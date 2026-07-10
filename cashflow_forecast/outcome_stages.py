"""Classify outcome stages only — never mix with risk flags."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from cashflow_forecast.config import OVERDUE_BUFFER_DAYS, RESUBMISSION_LAG_DAYS
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.payer_sla import get_lag_days
from cashflow_forecast.utils import normalize_name_key


OUTCOME_STAGES = ("paid", "on_track", "overdue", "rejected", "denied", "zero_pay")


def _build_rejection_index(rejections: pd.DataFrame) -> set[tuple[str, date]]:
    keys: set[tuple[str, date]] = set()
    if rejections is None or rejections.empty:
        return keys
    for _, row in rejections.iterrows():
        nk = row.get("name_key") or ""
        dos = row.get("service_date")
        if nk and dos:
            keys.add((nk, dos))
    return keys


def _build_denial_index(denials: pd.DataFrame) -> dict[tuple[str, date], dict]:
    idx: dict[tuple[str, date], dict] = {}
    if denials is None or denials.empty:
        return idx
    for _, row in denials.iterrows():
        nk = row.get("name_key") or ""
        dos = row.get("service_date")
        if not nk or not dos:
            continue
        key = (nk, dos)
        # Keep highest denied amount if duplicates
        prev = idx.get(key)
        amt = float(row.get("denied_amount") or 0)
        if prev is None or amt > prev["denied_amount"]:
            idx[key] = {
                "denied_amount": amt,
                "stage": str(row.get("stage") or ""),
                "denial_category": str(row.get("denial_category") or ""),
                "payer": str(row.get("payer") or ""),
            }
    return idx


def classify_outcomes(
    lines: pd.DataFrame,
    *,
    sla_lookup: dict[str, int],
    fee_estimator: FeeEstimator,
    rejections: pd.DataFrame | None = None,
    denials: pd.DataFrame | None = None,
    as_of: date,
) -> pd.DataFrame:
    """Assign outcome_stage per reconciliation line. Risk flags are separate."""
    rej_keys = _build_rejection_index(rejections if rejections is not None else pd.DataFrame())
    den_idx = _build_denial_index(denials if denials is not None else pd.DataFrame())

    rows: list[dict] = []
    for _, row in lines.iterrows():
        status = str(row.get("status") or "").lower()
        dos = row.get("date_of_service")
        ins = str(row.get("ins_name") or "")
        cpt = str(row.get("cpt_code") or "")
        paid_amt = float(row.get("paid_amount") or 0)
        name_key = row.get("name_key") or normalize_name_key(str(row.get("patient_name") or ""))

        lag = get_lag_days(sla_lookup, ins)
        expected_pay = (dos + timedelta(days=lag)) if dos else None
        expected_amount = fee_estimator.estimate(cpt, ins)

        outcome = "on_track"
        denied_amount = 0.0
        denial_category = ""
        forecast_shift_days = 0

        if status == "paid" and paid_amt > 0:
            outcome = "paid"
            expected_amount = paid_amt
        elif status == "zero_pay":
            outcome = "zero_pay"
            expected_amount = 0.0
        elif status == "patient_responsibility":
            outcome = "zero_pay"
            expected_amount = paid_amt
        elif dos and (name_key, dos) in rej_keys:
            outcome = "rejected"
            forecast_shift_days = RESUBMISSION_LAG_DAYS
        elif dos and (name_key, dos) in den_idx:
            outcome = "denied"
            info = den_idx[(name_key, dos)]
            denied_amount = info["denied_amount"]
            denial_category = info["denial_category"]
            expected_amount = denied_amount or expected_amount
            if "resubmit" in info["stage"].lower():
                forecast_shift_days = RESUBMISSION_LAG_DAYS
        elif status in ("pending", "secondary_pending", "") or paid_amt <= 0:
            if expected_pay and as_of > expected_pay + timedelta(days=OVERDUE_BUFFER_DAYS):
                outcome = "overdue"
            else:
                outcome = "on_track"
        else:
            outcome = "on_track"

        overdue_days = 0
        if outcome == "overdue" and expected_pay:
            overdue_days = (as_of - expected_pay).days

        forecast_date = None
        if expected_pay:
            forecast_date = expected_pay + timedelta(days=forecast_shift_days)

        rows.append(
            {
                "webpt_patient_id": row.get("webpt_patient_id", ""),
                "patient_name": row.get("patient_name", ""),
                "name_key": name_key,
                "dob": row.get("dob", ""),
                "facility_name": row.get("facility_name", ""),
                "ins_name": ins,
                "insurance_revflow": row.get("insurance_revflow", ""),
                "date_of_service": dos,
                "cpt_code": cpt,
                "modifier": row.get("modifier", ""),
                "reconcile_status": status,
                "paid_amount": paid_amt,
                "allowed_amount": float(row.get("allowed_amount") or 0),
                "eob_date": row.get("eob_date"),
                "outcome_stage": outcome,
                "expected_amount": round(expected_amount, 2),
                "expected_pay_date": expected_pay,
                "forecast_date": forecast_date,
                "overdue_days": overdue_days,
                "denied_amount": denied_amount,
                "denial_category": denial_category,
                "sla_lag_days": lag,
                "forecast_shift_days": forecast_shift_days,
            }
        )

    return pd.DataFrame(rows)
