"""Risk flags only — predictive signals, never an outcome stage."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from cashflow_forecast.config import SUBMISSION_WINDOW_DAYS
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.utils import normalize_name_key


def _severity_score(severity: str) -> int:
    s = (severity or "").lower()
    if s == "error":
        return 80
    if s == "warning":
        return 50
    return 30


def build_risk_flags(
    outcomes: pd.DataFrame,
    audit: pd.DataFrame,
    *,
    fee_estimator: FeeEstimator,
    as_of: date,
) -> pd.DataFrame:
    """Attach risk flags to visits. One row per (patient, DOS, flag)."""
    if outcomes.empty:
        return pd.DataFrame()

    # Audit index by patient_id+DOS and name_key+DOS
    audit_by_pid: dict[tuple[str, date], list[dict]] = {}
    audit_by_name: dict[tuple[str, date], list[dict]] = {}
    if audit is not None and not audit.empty:
        for _, row in audit.iterrows():
            dos = row.get("date_of_service")
            if not dos:
                continue
            info = {
                "flag": f"audit_{row.get('violation_type') or 'cpt'}",
                "rule_id": row.get("rule_id", ""),
                "severity": row.get("severity", ""),
                "risk_score": _severity_score(str(row.get("severity") or "")),
                "insurance_name": row.get("insurance_name", ""),
            }
            pid = str(row.get("patient_id") or "")
            nk = row.get("name_key") or ""
            if pid:
                audit_by_pid.setdefault((pid, dos), []).append(info)
            if nk:
                audit_by_name.setdefault((nk, dos), []).append(info)

    # Visit-level outcomes for unsubmitted check
    visit_keys = outcomes.drop_duplicates(
        subset=["webpt_patient_id", "date_of_service"], keep="first"
    )

    rows: list[dict] = []
    seen: set[tuple[str, date, str]] = set()

    for _, v in visit_keys.iterrows():
        pid = str(v.get("webpt_patient_id") or "")
        dos = v.get("date_of_service")
        nk = v.get("name_key") or ""
        if not dos:
            continue

        flags = audit_by_pid.get((pid, dos), []) or audit_by_name.get((nk, dos), [])
        for info in flags:
            key = (pid, dos, info["flag"])
            if key in seen:
                continue
            seen.add(key)
            amt = fee_estimator.estimate("", str(v.get("ins_name") or ""))
            # Prefer sum of CPT expected on that visit
            visit_lines = outcomes[
                (outcomes["webpt_patient_id"] == pid) & (outcomes["date_of_service"] == dos)
            ]
            if not visit_lines.empty:
                amt = float(visit_lines["expected_amount"].sum())
            rows.append(
                {
                    "webpt_patient_id": pid,
                    "patient_name": v.get("patient_name", ""),
                    "facility_name": v.get("facility_name", ""),
                    "ins_name": v.get("ins_name", ""),
                    "date_of_service": dos,
                    "outcome_stage": v.get("outcome_stage", ""),
                    "risk_flag": info["flag"],
                    "rule_id": info["rule_id"],
                    "severity": info["severity"],
                    "risk_score": info["risk_score"],
                    "exposure_amount": round(amt, 2),
                }
            )

        # Unsubmitted: pending-like outcomes with no rejection/denial and past submission window
        stage = str(v.get("outcome_stage") or "")
        if stage in ("on_track", "overdue") and dos:
            if as_of > dos + timedelta(days=SUBMISSION_WINDOW_DAYS):
                # Only if no audit already covering — still add unsubmitted as separate flag
                key = (pid, dos, "unsubmitted")
                if key not in seen:
                    # Heuristic: if still on_track/overdue long after DOS and never hit waystar
                    # We already excluded rejected/denied in outcome. Mark unsubmitted for overdue only
                    # when past submission window AND overdue (stronger signal), or always past window.
                    if stage == "overdue" or as_of > dos + timedelta(days=SUBMISSION_WINDOW_DAYS + 7):
                        seen.add(key)
                        visit_lines = outcomes[
                            (outcomes["webpt_patient_id"] == pid)
                            & (outcomes["date_of_service"] == dos)
                        ]
                        amt = float(visit_lines["expected_amount"].sum()) if not visit_lines.empty else 0.0
                        rows.append(
                            {
                                "webpt_patient_id": pid,
                                "patient_name": v.get("patient_name", ""),
                                "facility_name": v.get("facility_name", ""),
                                "ins_name": v.get("ins_name", ""),
                                "date_of_service": dos,
                                "outcome_stage": stage,
                                "risk_flag": "unsubmitted",
                                "rule_id": "submission_window",
                                "severity": "warning",
                                "risk_score": 40,
                                "exposure_amount": round(amt, 2),
                            }
                        )

    return pd.DataFrame(rows)
