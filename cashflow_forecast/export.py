"""Write forecast output CSVs and kpi_summary.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_df(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == "object":
            # stringify dates
            out[col] = out[col].map(lambda x: x.isoformat() if hasattr(x, "isoformat") else x)
    out.to_csv(path, index=False)


def export_all(output_dir: Path | str, artifacts: dict[str, Any]) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    mapping = {
        "payer_sla": "payer_sla.csv",
        "outcome_stages": "outcome_stages.csv",
        "risk_flags": "risk_flags.csv",
        "audit_denial_matches": "audit_denial_matches.csv",
        "actual_cash_daily": "actual_cash_daily.csv",
        "actual_cash_weekly": "actual_cash_weekly.csv",
        "actual_cash_monthly": "actual_cash_monthly.csv",
        "projected_cash_daily": "projected_cash_daily.csv",
        "projected_cash_weekly": "projected_cash_weekly.csv",
        "projected_cash_monthly": "projected_cash_monthly.csv",
        # Dimensional (full)
        "actual_cash_daily_by_insurance": "actual_cash_daily_by_insurance.csv",
        "actual_cash_weekly_by_insurance": "actual_cash_weekly_by_insurance.csv",
        "actual_cash_monthly_by_insurance": "actual_cash_monthly_by_insurance.csv",
        "actual_cash_daily_by_facility": "actual_cash_daily_by_facility.csv",
        "actual_cash_weekly_by_facility": "actual_cash_weekly_by_facility.csv",
        "actual_cash_monthly_by_facility": "actual_cash_monthly_by_facility.csv",
        "projected_cash_daily_by_insurance": "projected_cash_daily_by_insurance.csv",
        "projected_cash_weekly_by_insurance": "projected_cash_weekly_by_insurance.csv",
        "projected_cash_monthly_by_insurance": "projected_cash_monthly_by_insurance.csv",
        "projected_cash_daily_by_facility": "projected_cash_daily_by_facility.csv",
        "projected_cash_weekly_by_facility": "projected_cash_weekly_by_facility.csv",
        "projected_cash_monthly_by_facility": "projected_cash_monthly_by_facility.csv",
        "projected_cash_monthly_by_facility_insurance": "projected_cash_monthly_by_facility_insurance.csv",
        # May–Aug filtered deliverable views
        "projected_cash_daily_may_aug": "projected_cash_daily_may_aug.csv",
        "projected_cash_weekly_may_aug": "projected_cash_weekly_may_aug.csv",
        "projected_cash_monthly_may_aug": "projected_cash_monthly_may_aug.csv",
        "projected_cash_daily_by_insurance_may_aug": "projected_cash_daily_by_insurance_may_aug.csv",
        "projected_cash_weekly_by_insurance_may_aug": "projected_cash_weekly_by_insurance_may_aug.csv",
        "projected_cash_monthly_by_insurance_may_aug": "projected_cash_monthly_by_insurance_may_aug.csv",
        "projected_cash_daily_by_facility_may_aug": "projected_cash_daily_by_facility_may_aug.csv",
        "projected_cash_weekly_by_facility_may_aug": "projected_cash_weekly_by_facility_may_aug.csv",
        "projected_cash_monthly_by_facility_may_aug": "projected_cash_monthly_by_facility_may_aug.csv",
        "projected_cash_monthly_by_facility_insurance_may_aug": "projected_cash_monthly_by_facility_insurance_may_aug.csv",
        "actual_cash_daily_may_aug": "actual_cash_daily_may_aug.csv",
        "actual_cash_weekly_may_aug": "actual_cash_weekly_may_aug.csv",
        "actual_cash_monthly_may_aug": "actual_cash_monthly_may_aug.csv",
        "forward_visits_august": "forward_visits_august.csv",
        "overdue_by_insurance": "overdue_by_insurance.csv",
        "denied_by_insurance": "denied_by_insurance.csv",
        "risk_by_insurance": "risk_by_insurance.csv",
        "outcome_stage_counts": "outcome_stage_counts.csv",
        "payer_plan_payment_models": "payer_plan_payment_models.csv",
        "deposit_capacity": "deposit_capacity.csv",
        "slot_capacity_audit": "slot_capacity_audit.csv",
        "reschedule_audit": "reschedule_audit.csv",
        "land_accuracy": "land_accuracy.csv",
        "land_accuracy_focus": "land_accuracy_focus.csv",
    }

    for key, filename in mapping.items():
        df = artifacts.get(key)
        if isinstance(df, pd.DataFrame):
            _write_df(output_dir / filename, df)

    kpi = artifacts.get("kpi_summary")
    if isinstance(kpi, dict):
        (output_dir / "kpi_summary.json").write_text(
            json.dumps(kpi, indent=2, default=str), encoding="utf-8"
        )

    return output_dir
