"""Hard-gate and alert checks against acquired source artifacts."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cashflow_ops import state
from cashflow_ops.adapters import revflow as revflow_adapter
from cashflow_ops.adapters import waystar as waystar_adapter
from cashflow_ops.config import (
    CASE_PIPELINE_DIR,
    MAIL_CHECKS_CSV,
    PAYMENTS_DROP_ALERT_PCT,
    SCHEDULE_DROP_ALERT_PCT,
    SCHEDULE_DROP_STOP_PCT,
    TRACKER_XLSX,
    WEBPT_OUTPUT,
)


@dataclass
class CheckResult:
    ok: bool
    critical_failures: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _count_csv_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


def _latest_schedule_csv() -> Path | None:
    matches = sorted(WEBPT_OUTPUT.glob("schedule_visits_*.csv"))
    if matches:
        return matches[-1]
    return None


def _latest_payments_csv() -> Path | None:
    matches = sorted(WEBPT_OUTPUT.glob("patient_payments*.csv"))
    if matches:
        return matches[-1]
    return None


def run_all_checks(*, as_of: date, acquire_outputs: dict[str, Any] | None = None) -> CheckResult:
    metrics: dict[str, Any] = {}
    alerts: list[dict[str, Any]] = []
    critical: list[str] = []
    acquire_outputs = acquire_outputs or {}

    # Tracker (Postgres SoT)
    try:
        from cashflow_db.repository import connection, tracker as tracker_repo

        with connection() as conn:
            tracker_n = tracker_repo.count_active_rows(conn)
        metrics["tracker_active_rows"] = tracker_n
        if tracker_n <= 0:
            critical.append(
                "Transaction Tracker empty: billing.transaction_tracker_row has no active rows"
            )
        else:
            metrics["tracker_present"] = True
    except Exception as exc:  # noqa: BLE001
        critical.append(f"Transaction Tracker DB check failed: {exc}")
        # Legacy file presence is informative only
        metrics["tracker_xlsx_present"] = TRACKER_XLSX.is_file()

    # Schedule
    sched = _latest_schedule_csv()
    schedule_rows = _count_csv_rows(sched) if sched else 0
    metrics["schedule_path"] = str(sched) if sched else None
    metrics["schedule_rows"] = schedule_rows
    if schedule_rows <= 0 and not acquire_outputs.get("webpt_skipped"):
        critical.append("Schedule export empty or missing")

    prior = state.get_prior_snapshot(as_of)
    if prior and schedule_rows > 0:
        prior_vol = (prior.get("volumes") or {}).get("schedule_rows")
        if prior_vol and int(prior_vol) > 0:
            drop = 1.0 - (schedule_rows / float(prior_vol))
            metrics["schedule_drop_pct"] = round(drop, 4)
            if drop >= SCHEDULE_DROP_STOP_PCT:
                critical.append(
                    f"Schedule dropped {drop:.1%} vs prior snapshot ({prior_vol} → {schedule_rows})"
                )
            elif drop >= SCHEDULE_DROP_ALERT_PCT:
                alerts.append(
                    {
                        "severity": "warning",
                        "alert_key": "schedule_volume_drop",
                        "message": (
                            f"Schedule dropped {drop:.1%} vs prior "
                            f"({prior_vol} → {schedule_rows})"
                        ),
                        "payload": {"prior": prior_vol, "current": schedule_rows},
                    }
                )

    # RevFlow
    rf_count = revflow_adapter.count_exports()
    metrics["revflow_export_files"] = rf_count
    if rf_count <= 0 and not acquire_outputs.get("revflow_skipped"):
        critical.append("RevFlow exports directory has 0 CSV files")

    # Patient payments
    pay_csv = _latest_payments_csv()
    pay_rows = _count_csv_rows(pay_csv) if pay_csv else 0
    metrics["patient_payments_rows"] = pay_rows
    metrics["patient_payments_path"] = str(pay_csv) if pay_csv else None
    if prior and pay_rows > 0:
        prior_pay = (prior.get("volumes") or {}).get("patient_payments_rows")
        if prior_pay and int(prior_pay) > 0:
            drop = 1.0 - (pay_rows / float(prior_pay))
            if drop >= PAYMENTS_DROP_ALERT_PCT:
                alerts.append(
                    {
                        "severity": "warning",
                        "alert_key": "payments_volume_drop",
                        "message": (
                            f"Patient payments dropped {drop:.1%} "
                            f"({prior_pay} → {pay_rows})"
                        ),
                        "payload": {"prior": prior_pay, "current": pay_rows},
                    }
                )
    elif pay_rows <= 0:
        alerts.append(
            {
                "severity": "warning",
                "alert_key": "payments_missing",
                "message": "Patient payments CSV missing or empty",
            }
        )

    # Case pipeline queue / OCR
    sqlite_path = CASE_PIPELINE_DIR / "case_units.sqlite"
    metrics["case_sqlite_present"] = sqlite_path.is_file()
    cases_dir = CASE_PIPELINE_DIR / "cases"
    case_dirs = 0
    if cases_dir.is_dir():
        case_dirs = sum(1 for _ in cases_dir.rglob("manifests"))
    metrics["case_manifest_dirs"] = case_dirs

    # Waystar
    ws = waystar_adapter.count_waystar_outputs()
    metrics["waystar_csv_files"] = ws["csv_files"]
    if ws["csv_files"] <= 0 and not acquire_outputs.get("waystar_skipped"):
        alerts.append(
            {
                "severity": "warning",
                "alert_key": "waystar_empty",
                "message": "Waystar output has 0 CSV files (explicit skip not set)",
            }
        )

    # Mail
    metrics["mail_checks_present"] = MAIL_CHECKS_CSV.is_file()
    if not MAIL_CHECKS_CSV.is_file():
        alerts.append(
            {
                "severity": "info",
                "alert_key": "mail_checks_missing",
                "message": f"Mail checks CSV not found: {MAIL_CHECKS_CSV}",
            }
        )

    return CheckResult(
        ok=len(critical) == 0,
        critical_failures=critical,
        alerts=alerts,
        metrics=metrics,
    )
