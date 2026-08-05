"""Classify outcome stages only — never mix with risk flags."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from cashflow_forecast.config import OVERDUE_BUFFER_DAYS, RESUBMISSION_LAG_DAYS
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.insurance_behavior_sla import (
    DepositSchedule,
    get_deposit_schedule,
    get_eob_to_deposit_days,
    snap_to_deposit_weekdays,
    snap_weekend_by_historical_weekday,
)
from cashflow_forecast.payer_plan import resolve_payer_plan
from cashflow_forecast.payer_sla import build_lag_cache, get_lag_days
from cashflow_forecast.utils import normalize_name_key

log = logging.getLogger(__name__)


def _resolve_spill_probs_for_ins(
    ins_name: str,
    insurance_revflow: str,
    grain_probs: dict[str, dict[int, float]],
    global_probs: dict[int, float],
) -> tuple[dict[int, float], str]:
    key = resolve_payer_plan(ins_name, insurance_revflow=insurance_revflow)
    for h in key.hierarchy:
        if h in grain_probs:
            return grain_probs[h], "historical"
    if global_probs and sum(global_probs.values()) > 1e-12:
        return global_probs, "global"
    return {d: 0.2 for d in range(5)}, "uniform"


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


def _lookup_schedule_for_line(
    deposit_schedule_lookup: dict[str, DepositSchedule] | None,
    ins_name: str,
    insurance_revflow: str,
) -> DepositSchedule | None:
    # Prefer RevFlow payor cadence (actual deposit behavior) over WebPT label.
    if insurance_revflow:
        schedule = get_deposit_schedule(deposit_schedule_lookup, insurance_revflow)
        if schedule is not None:
            return schedule
    return get_deposit_schedule(deposit_schedule_lookup, ins_name)


def classify_outcomes(
    lines: pd.DataFrame,
    *,
    sla_lookup: dict[str, int],
    fee_estimator: FeeEstimator,
    rejections: pd.DataFrame | None = None,
    denials: pd.DataFrame | None = None,
    as_of: date,
    deposit_schedule_lookup: dict[str, DepositSchedule] | None = None,
    weekday_probs_by_grain: dict[str, dict[int, float]] | None = None,
    global_weekday_probs: dict[int, float] | None = None,
    eob_to_deposit_lookup: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Assign outcome_stage per reconciliation line. Risk flags are separate."""
    rej_keys = _build_rejection_index(rejections if rejections is not None else pd.DataFrame())
    den_idx = _build_denial_index(denials if denials is not None else pd.DataFrame())
    schedule_cache: dict[tuple[str, str], DepositSchedule | None] = {}
    spill_cache: dict[tuple[str, str], tuple[dict[int, float], str]] = {}
    eob_dep_cache: dict[tuple[str, str], int | None] = {}
    ins_values = [str(v) for v in lines["ins_name"].fillna("").tolist()] if "ins_name" in lines.columns else []
    lag_cache = build_lag_cache(sla_lookup, ins_values)
    fee_cache: dict[tuple[str, str], float] = {}
    grain_probs = weekday_probs_by_grain or {}
    global_probs = global_weekday_probs or {d: 0.2 for d in range(5)}

    rows: list[dict] = []
    for row in lines.itertuples(index=False):
        status = str(getattr(row, "status", "") or "").lower()
        dos = getattr(row, "date_of_service", None)
        ins = str(getattr(row, "ins_name", "") or "")
        revflow = str(getattr(row, "insurance_revflow", "") or "")
        if revflow.lower() == "nan":
            revflow = ""
        cpt = str(getattr(row, "cpt_code", "") or "")
        paid_amt = float(getattr(row, "paid_amount", 0) or 0)
        patient_name = str(getattr(row, "patient_name", "") or "")
        name_key = getattr(row, "name_key", None) or normalize_name_key(patient_name)

        lag = lag_cache.get(ins.strip().lower())
        if lag is None:
            lag = get_lag_days(sla_lookup, ins)
            lag_cache[ins.strip().lower()] = lag
        expected_pay = (dos + timedelta(days=lag)) if dos else None
        try:
            units = float(getattr(row, "units", 1) or 1) or 1.0
        except (TypeError, ValueError):
            units = 1.0
        precomputed = getattr(row, "precomputed_expected", None)
        if precomputed is not None and precomputed != "" and not (
            isinstance(precomputed, float) and pd.isna(precomputed)
        ):
            try:
                expected_amount = float(precomputed)
            except (TypeError, ValueError):
                fee_key = (cpt, ins.lower())
                if fee_key not in fee_cache:
                    fee_cache[fee_key] = fee_estimator.estimate(cpt, ins)
                expected_amount = fee_cache[fee_key] * units
        else:
            fee_key = (cpt, ins.lower())
            if fee_key not in fee_cache:
                fee_cache[fee_key] = fee_estimator.estimate(cpt, ins)
            expected_amount = fee_cache[fee_key] * units
        source = str(getattr(row, "source", "") or "reconciliation")

        outcome = "on_track"
        denied_amount = 0.0
        denial_category = ""
        forecast_shift_days = 0

        if status == "paid":
            outcome = "paid"
            expected_amount = paid_amt
        elif status == "denied":
            outcome = "denied"
            expected_amount = 0.0
            denied_amount = 0.0
            denial_category = "sf_visit_override"
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
        deposit_snap_days = 0
        if expected_pay or getattr(row, "eob_date", None) is not None:
            cache_key = (ins.lower(), revflow.lower())
            eob_raw = getattr(row, "eob_date", None)
            eob_date = None
            if eob_raw is not None and not (isinstance(eob_raw, float) and pd.isna(eob_raw)):
                if isinstance(eob_raw, date) and not isinstance(eob_raw, pd.Timestamp):
                    eob_date = eob_raw
                else:
                    ts = pd.to_datetime(eob_raw, errors="coerce")
                    if not pd.isna(ts):
                        eob_date = ts.date()

            # Prefer EOB→deposit when EOB is known (batch payers like Healthfirst).
            raw_forecast = None
            if eob_date is not None:
                if cache_key not in eob_dep_cache:
                    eob_dep_cache[cache_key] = get_eob_to_deposit_days(
                        eob_to_deposit_lookup, ins, revflow
                    )
                eob_dep_lag = eob_dep_cache[cache_key]
                if eob_dep_lag is not None:
                    raw_forecast = eob_date + timedelta(days=eob_dep_lag + forecast_shift_days)
            if raw_forecast is None and expected_pay is not None:
                raw_forecast = expected_pay + timedelta(days=forecast_shift_days)

            if raw_forecast is not None:
                if cache_key not in schedule_cache:
                    schedule_cache[cache_key] = _lookup_schedule_for_line(
                        deposit_schedule_lookup, ins, revflow
                    )
                schedule = schedule_cache[cache_key]
                if schedule is not None and schedule.snaps:
                    forecast_date = snap_to_deposit_weekdays(
                        raw_forecast, schedule.allowed_weekdays
                    )
                else:
                    # near_daily / no cadence: only re-assign weekend raw via Hist→Assign
                    if raw_forecast.weekday() < 5:
                        forecast_date = raw_forecast
                    else:
                        if cache_key not in spill_cache:
                            spill_cache[cache_key] = _resolve_spill_probs_for_ins(
                                ins, revflow, grain_probs, global_probs
                            )
                        probs, method = spill_cache[cache_key]
                        spill = snap_weekend_by_historical_weekday(
                            raw_forecast,
                            probs,
                            spill_method=method,
                        )
                        forecast_date = spill.forecast_date
                deposit_snap_days = (forecast_date - raw_forecast).days
                if forecast_date.weekday() >= 5:
                    log.warning(
                        "Weekend forecast generated after snap: date=%s ins=%s stage=%s",
                        forecast_date.isoformat(),
                        ins,
                        outcome,
                    )

        rows.append(
            {
                "webpt_patient_id": getattr(row, "webpt_patient_id", ""),
                "patient_name": patient_name,
                "name_key": name_key,
                "dob": getattr(row, "dob", ""),
                "facility_name": getattr(row, "facility_name", ""),
                "ins_name": ins,
                "insurance_revflow": getattr(row, "insurance_revflow", ""),
                "date_of_service": dos,
                "cpt_code": cpt,
                "modifier": getattr(row, "modifier", ""),
                "reconcile_status": status,
                "paid_amount": paid_amt,
                "allowed_amount": float(getattr(row, "allowed_amount", 0) or 0),
                "eob_date": getattr(row, "eob_date", None),
                "outcome_stage": outcome,
                "expected_amount": round(expected_amount, 2),
                "expected_pay_date": expected_pay,
                "forecast_date": forecast_date,
                "deposit_snap_days": deposit_snap_days,
                "overdue_days": overdue_days,
                "denied_amount": denied_amount,
                "denial_category": denial_category,
                "sla_lag_days": lag,
                "forecast_shift_days": forecast_shift_days,
                "units": units,
                "source": source,
            }
        )

    return pd.DataFrame.from_records(rows)
