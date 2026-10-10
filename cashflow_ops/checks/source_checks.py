"""Hard-gate and alert checks against acquired source artifacts."""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cashflow_ops import state
from cashflow_ops.adapters import revflow as revflow_adapter
from cashflow_ops.adapters import snowflake as snowflake_adapter
from cashflow_ops.adapters import waystar as waystar_adapter
from cashflow_ops.config import (
    CASE_PIPELINE_DIR,
    MAIL_CHECKS_CSV,
    SCHEDULE_DROP_ALERT_PCT,
    TRACKER_XLSX,
    WEBPT_OUTPUT,
)

# The nightly pull finishes before the load; an older file means the pull failed.
WAYSTAR_STALE_HOURS = 20


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


def _count_schedule_appointments() -> tuple[int | None, str | None]:
    """Return (appointment count, error). Count is None when the DB probe fails."""
    try:
        from cashflow_db.repository import connection, visits as visit_repo

        with connection() as conn:
            return visit_repo.count_schedule_appointments(conn), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def schedule_source_gate(
    *,
    db_rows: int | None,
    csv_rows: int,
    skipped: bool,
    db_error: str | None = None,
) -> tuple[list[str], list[dict[str, Any]], int]:
    """Schedule SoT is Postgres. CSV is optional leftover after prune."""
    if skipped:
        return [], [], db_rows or csv_rows or 0
    if db_rows is not None and db_rows > 0:
        alerts: list[dict[str, Any]] = []
        if csv_rows <= 0:
            alerts.append(
                {
                    "severity": "info",
                    "alert_key": "schedule_csv_pruned",
                    "message": (
                        f"Schedule CSV absent on disk; warehouse has {db_rows} "
                        "schedule_appointment rows"
                    ),
                }
            )
        return [], alerts, db_rows
    if db_error:
        if csv_rows > 0:
            return [], [
                {
                    "severity": "warning",
                    "alert_key": "schedule_db_probe_failed",
                    "message": f"Schedule DB check failed ({db_error}); CSV has {csv_rows} rows",
                }
            ], csv_rows
        return [f"Schedule empty (DB check failed: {db_error})"], [], 0
    if csv_rows > 0:
        return [], [
            {
                "severity": "warning",
                "alert_key": "schedule_csv_only",
                "message": f"Schedule not loaded to DB; CSV has {csv_rows} rows",
            }
        ], csv_rows
    return ["Schedule export empty or missing"], [], 0


def _latest_payments_csv() -> Path | None:
    matches = sorted(WEBPT_OUTPUT.glob("patient_payments*.csv"))
    if matches:
        return matches[-1]
    return None


def _count_eob_checks() -> tuple[int | None, str | None]:
    """Return (eob_check count, error). Count is None when the DB probe fails."""
    try:
        from cashflow_db.repository import connection, payments as pay_repo

        with connection() as conn:
            return pay_repo.count_eob_checks(conn), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def revflow_exports_gate(
    *,
    export_files: int,
    skipped: bool,
    eob_check_rows: int | None,
    db_error: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Hard-fail only when disk is empty AND the warehouse has no EOBs."""
    if export_files > 0 or skipped:
        return [], []
    if eob_check_rows is not None and eob_check_rows > 0:
        return [], [
            {
                "severity": "info",
                "alert_key": "revflow_exports_pruned",
                "message": (
                    f"RevFlow exports empty on disk; warehouse has {eob_check_rows} eob_check rows"
                ),
            }
        ]
    if db_error:
        return [
            f"RevFlow exports directory has 0 CSV files (DB check failed: {db_error})"
        ], []
    return ["RevFlow exports directory has 0 CSV files"], []


def waystar_claims_gate(
    *,
    recent_present: bool,
    skipped: bool,
    age_hours: float | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if skipped:
        return [], []
    if not recent_present:
        return ["Waystar recent claims file missing"], []
    if age_hours is not None and age_hours > WAYSTAR_STALE_HOURS:
        # A failed scrape leaves yesterday's file in place and the load reruns it.
        return [], [
            {
                "severity": "warning",
                "alert_key": "waystar_claims_stale",
                "message": (
                    f"Waystar claims file is {age_hours:.0f}h old — the last pull failed, "
                    "so new remits are not loaded"
                ),
                "payload": {"age_hours": round(age_hours, 1)},
            }
        ]
    return [], []


def pt_city_source_gate(
    *,
    visit_present: bool,
    patient_present: bool,
    charge_csvs: int,
    skipped: bool,
    db_visits: int | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if skipped:
        return [], []
    alerts: list[dict[str, Any]] = []
    crit: list[str] = []
    if visit_present or (db_visits is not None and db_visits > 0):
        if not visit_present and db_visits:
            alerts.append(
                {
                    "severity": "info",
                    "alert_key": "pt_city_visit_csv_missing",
                    "message": f"PT_CITY visit CSV absent on disk; warehouse has {db_visits} visits",
                }
            )
    else:
        crit.append("PT_CITY visit extract missing")
    if not patient_present:
        crit.append("PT_CITY patient account↔EMR CSV missing")
    if charge_csvs <= 0:
        alerts.append(
            {
                "severity": "warning",
                "alert_key": "pt_city_charges_missing",
                "message": "PT_CITY charges CSV directory is empty",
            }
        )
    return crit, alerts


def _count_core_visits() -> tuple[int | None, str | None]:
    try:
        from cashflow_db.repository import connection, visits as visit_repo

        with connection() as conn:
            return visit_repo.count_schedule_appointments(conn), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


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

    # WebPT schedule is no longer a nightly source. Keep metrics if leftovers exist.
    sched = None
    matches = sorted(WEBPT_OUTPUT.glob("schedule_visits_*.csv"))
    if matches:
        sched = matches[-1]
    csv_rows = _count_csv_rows(sched) if sched else 0
    db_n, sched_db_error = _count_schedule_appointments()
    crit, extra_alerts, schedule_rows = schedule_source_gate(
        db_rows=db_n,
        csv_rows=csv_rows,
        skipped=True,
        db_error=sched_db_error,
    )
    alerts.extend(extra_alerts)
    metrics["schedule_path"] = str(sched) if sched else None
    metrics["schedule_rows"] = schedule_rows
    metrics["schedule_csv_rows"] = csv_rows
    if db_n is not None:
        metrics["schedule_appointment_rows"] = db_n

    prior = state.get_prior_snapshot(as_of)
    if prior and schedule_rows > 0:
        prior_vol = (prior.get("volumes") or {}).get("schedule_rows")
        if prior_vol and int(prior_vol) > 0:
            drop = 1.0 - (schedule_rows / float(prior_vol))
            metrics["schedule_drop_pct"] = round(drop, 4)
            if drop >= SCHEDULE_DROP_ALERT_PCT:
                alerts.append(
                    {
                        "severity": "info",
                        "alert_key": "schedule_volume_drop",
                        "message": (
                            f"Schedule dropped {drop:.1%} vs prior "
                            f"({prior_vol} → {schedule_rows})"
                        ),
                        "payload": {"prior": prior_vol, "current": schedule_rows},
                    }
                )

    # RevFlow: empty exports on disk is OK after prune if warehouse already has EOBs.
    rf_count = revflow_adapter.count_exports()
    metrics["revflow_export_files"] = rf_count
    skipped_rf = bool(acquire_outputs.get("revflow_skipped"))
    eob_n, rf_db_error = _count_eob_checks()
    if eob_n is not None:
        metrics["eob_check_rows"] = eob_n
    crit, extra_alerts = revflow_exports_gate(
        export_files=rf_count,
        skipped=skipped_rf,
        eob_check_rows=eob_n,
        db_error=rf_db_error,
    )
    critical.extend(crit)
    alerts.extend(extra_alerts)

    pay_csv = _latest_payments_csv()
    pay_rows = _count_csv_rows(pay_csv) if pay_csv else 0
    metrics["patient_payments_rows"] = pay_rows
    metrics["patient_payments_path"] = str(pay_csv) if pay_csv else None
    if pay_rows <= 0:
        alerts.append(
            {
                "severity": "info",
                "alert_key": "payments_missing",
                "message": "Patient payments CSV missing or empty (not required for Waystar nightly)",
            }
        )

    sqlite_path = CASE_PIPELINE_DIR / "case_units.sqlite"
    metrics["case_sqlite_present"] = sqlite_path.is_file()
    cases_dir = CASE_PIPELINE_DIR / "cases"
    case_dirs = 0
    if cases_dir.is_dir():
        case_dirs = sum(1 for _ in cases_dir.rglob("manifests"))
    metrics["case_manifest_dirs"] = case_dirs

    ws = waystar_adapter.count_waystar_outputs()
    metrics["waystar_csv_files"] = ws.get("csv_files")
    metrics["waystar_recent_claims"] = ws.get("recent_claims")
    recent_path = waystar_adapter.recent_claims_path()
    recent_ok = bool(recent_path) or bool(ws.get("recent_claims"))
    age_hours = (time.time() - recent_path.stat().st_mtime) / 3600 if recent_path else None
    if age_hours is not None:
        metrics["waystar_claims_age_hours"] = round(age_hours, 1)
    crit, extra_alerts = waystar_claims_gate(
        recent_present=recent_ok,
        skipped=False,
        age_hours=age_hours,
    )
    critical.extend(crit)
    alerts.extend(extra_alerts)

    pt = snowflake_adapter.pt_city_artifact_counts()
    metrics.update({f"pt_city_{k}": v for k, v in pt.items()})
    db_visits, _ = _count_core_visits()
    if db_visits is not None:
        metrics["core_visit_proxy_rows"] = db_visits
    crit, extra_alerts = pt_city_source_gate(
        visit_present=bool(pt.get("visit_csv")),
        patient_present=bool(pt.get("patient_csv")),
        charge_csvs=int(pt.get("charge_csvs") or 0),
        skipped=False,
        db_visits=db_visits,
    )
    critical.extend(crit)
    alerts.extend(extra_alerts)

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
