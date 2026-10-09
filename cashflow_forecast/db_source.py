"""Load forecast inputs / write outputs via cashflow_db.repository (no CSV SoT)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from cashflow_db.repository import claims as claims_repo
from cashflow_db.repository import connection
from cashflow_db.repository import eligibility as elig_repo
from cashflow_db.repository import forecast as forecast_repo
from cashflow_db.repository import insurance as ins_repo
from cashflow_db.repository import payments as pay_repo
from cashflow_db.repository import reconciliation as recon_repo
from cashflow_db.repository import tracker as tracker_repo
from cashflow_db.repository import visits as visit_repo

log = logging.getLogger(__name__)


def _rows_to_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def load_reconciliation_lines_df(
    *,
    run_id: str | None = None,
    as_of: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = recon_repo.get_lines(conn, run_id=run_id, as_of=as_of)
    df = _rows_to_df(rows)
    if not df.empty and "date_of_service" in df.columns:
        df["date_of_service"] = pd.to_datetime(df["date_of_service"], errors="coerce").dt.date
    if not df.empty and "eob_date" in df.columns:
        df["eob_date"] = pd.to_datetime(df["eob_date"], errors="coerce").dt.date
    return df


def load_reconciliation_visits_df(
    *,
    run_id: str | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = recon_repo.get_visit_aggs(conn, run_id=run_id)
    return _rows_to_df(rows)


def load_payments_unified_df(
    *,
    as_of: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = pay_repo.get_eob_payments_unified(conn, as_of=as_of)
    return _rows_to_df(rows)


def load_payor_behavior_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = ins_repo.get_payor_behavior_summary(conn)
    return _rows_to_df(rows)


def load_checks_timeline_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = ins_repo.get_checks_timeline(conn)
    return _rows_to_df(rows)


def load_plans_of_care_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = ins_repo.get_plans_of_care(conn)
    return _rows_to_df(rows)


def load_eligibility_ins_lookup(
    *,
    database_url: str | None = None,
) -> dict[tuple[str, date], str]:
    """Map (emr_patient_id, DOS) → Eligibility Sheet insurance_name."""
    with connection(database_url) as conn:
        rows = elig_repo.list_visit_insurance_names(conn)
    lookup: dict[tuple[str, date], str] = {}
    for row in rows:
        pid = str(row.get("emr_patient_id") or "").strip()
        dos = row.get("dos")
        ins = str(row.get("insurance_name") or "").strip()
        if not pid or not ins or dos is None:
            continue
        if hasattr(dos, "date") and not isinstance(dos, date):
            dos = dos.date()
        if not isinstance(dos, date):
            continue
        lookup[(pid, dos)] = ins
    return lookup


def load_clinical_ar_lines_df(
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    database_url: str | None = None,
    exclude_reconciliation_run_id: str | None = None,
) -> pd.DataFrame:
    """Service lines as AR-like rows for Jan–May style pending volume."""
    with connection(database_url) as conn:
        rows = visit_repo.get_service_lines_for_reconcile(
            conn,
            service_from=service_from,
            service_to=service_to,
            exclude_reconciliation_run_id=exclude_reconciliation_run_id,
        )
    df = _rows_to_df(rows)
    if df.empty:
        return df
    # Align column names with extracted AR loaders where possible
    rename = {"webpt_patient_id": "webpt_patient_id", "date_of_service": "date_of_service"}
    df = df.rename(columns=rename)
    df["status"] = "pending"
    return df


def load_patients_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        ph = visit_repo.get_patients_enriched(conn)
    df = _rows_to_df(ph)
    # Forward PoC / extract loaders key on patient_id (= WebPT EMR id)
    if not df.empty and "patient_id" not in df.columns and "webpt_patient_id" in df.columns:
        df = df.copy()
        df["patient_id"] = df["webpt_patient_id"]
    return df

def load_denials_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = claims_repo.get_denial_records(conn)
    return _rows_to_df(rows)


def load_audit_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = claims_repo.get_audit_findings(conn)
    return _rows_to_df(rows)


def load_tracker_actuals_df(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    """Daily actual cash from billing.transaction_tracker_row (not bank_deposit / RevFlow)."""
    with connection(database_url) as conn:
        rows = tracker_repo.get_actual_cash_daily(
            conn, date_from=date_from, date_to=date_to
        )
    df = _rows_to_df(rows)
    if df.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count", "deposit_date"])
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    if "line_count" in df.columns:
        df["line_count"] = (
            pd.to_numeric(df["line_count"], errors="coerce").fillna(0).astype(int)
        )
    else:
        df["line_count"] = 0
    df["deposit_date"] = pd.to_datetime(df["period"], errors="coerce").dt.date
    return df


def load_tracker_payer_daily_df(*, database_url: str | None = None) -> pd.DataFrame:
    """Tracker dollars by day × payor (for Layer 2B eligible_orgs)."""
    with connection(database_url) as conn:
        rows = pay_repo.get_tracker_payer_daily(conn)
    return _rows_to_df(rows)


def load_deposits_df(
    *,
    as_of: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = pay_repo.get_bank_deposits(conn)
    df = _rows_to_df(rows)
    if not df.empty:
        if "bank_posting_date" in df.columns:
            df["deposit_date"] = df["bank_posting_date"]
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        if as_of is not None and "deposit_date" in df.columns:
            posted = pd.to_datetime(df["deposit_date"], errors="coerce").dt.date
            df = df[posted.notna() & (posted <= as_of)].copy()
    return df


def load_sheet_lump_inputs(
    *,
    as_of: date | None = None,
    database_url: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tracker lines + checks_deposits sheet for cadence attribution (not scoring)."""
    with connection(database_url) as conn:
        tracker_rows = tracker_repo.list_rows_for_sheet_attrib(conn, date_to=as_of)
        sheet_rows = pay_repo.list_checks_deposits_sheet(conn)
    return tracker_rows, sheet_rows


def enrich_deposit_events_with_sheet(
    events: list[Any],
    *,
    as_of: date,
    database_url: str | None = None,
) -> list[Any]:
    """Replace unmatched Deposit lumps with sheet-payer events for cadence/FFD only."""
    from cashflow_forecast.sheet_lump_attrib import (
        apply_attributed_to_events,
        attribute_sheet_to_lumps,
    )

    tracker_rows, sheet_rows = load_sheet_lump_inputs(
        as_of=as_of, database_url=database_url
    )
    if not tracker_rows or not sheet_rows:
        return list(events)
    attributed = attribute_sheet_to_lumps(tracker_rows, sheet_rows, as_of=as_of)
    if not attributed:
        return list(events)
    return apply_attributed_to_events(events, attributed, as_of=as_of)


def enrich_deposit_events_with_nonins(
    events: list[Any],
    *,
    as_of: date,
    database_url: str | None = None,
) -> list[Any]:
    """Add separate MERCH / BOA cadence grains. Scoring actuals unchanged."""
    from cashflow_forecast.nonins_streams import (
        apply_nonins_to_events,
        daily_nonins_totals,
    )

    with connection(database_url) as conn:
        tracker_rows = tracker_repo.list_rows_for_sheet_attrib(conn, date_to=as_of)
    if not tracker_rows:
        return list(events)
    daily = daily_nonins_totals(tracker_rows, as_of=as_of)
    if not daily:
        return list(events)
    return apply_nonins_to_events(events, daily, as_of=as_of)


def load_scoring_actuals_df(
    *,
    as_of: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    """Daily actuals for scoring: tracker wins on days it has rows."""
    with connection(database_url) as conn:
        rows = pay_repo.get_scoring_actuals(conn)
    df = _rows_to_df(rows)
    if df.empty:
        return df
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df["deposit_date"] = pd.to_datetime(df["period"], errors="coerce").dt.date
    if as_of is not None:
        df = df[df["deposit_date"].notna() & (df["deposit_date"] <= as_of)].copy()
    return df


def load_tracker_payer_daily_df(*, database_url: str | None = None) -> pd.DataFrame:
    """Tracker dollars by day × payor (for Layer 2B eligible_orgs)."""
    with connection(database_url) as conn:
        rows = pay_repo.get_tracker_payer_daily(conn)
    return _rows_to_df(rows)


def write_forecast_run(
    *,
    algorithm_version: str,
    as_of_date: date,
    outcome_df: pd.DataFrame,
    feature_tables: dict[str, pd.DataFrame],
    reconciliation_run_id: str | None = None,
    rules_version: str | None = None,
    params: dict[str, Any] | None = None,
    database_url: str | None = None,
    created_at: datetime | None = None,
) -> str:
    with connection(database_url) as conn:
        etl_ids = recon_repo.latest_etl_run_ids(conn)
        if reconciliation_run_id is None:
            reconciliation_run_id = recon_repo.latest_reconciliation_run_id(conn)
        run_id = forecast_repo.create_forecast_run(
            conn,
            algorithm_version=algorithm_version,
            as_of_date=as_of_date,
            params=params,
            source_etl_run_ids=etl_ids,
            reconciliation_run_id=reconciliation_run_id,
            rules_version=rules_version,
            status="running",
            created_at=created_at,
        )
        try:
            pred_rows: list[dict[str, Any]] = []
            if not outcome_df.empty:
                for rec in outcome_df.to_dict(orient="records"):
                    pred_rows.append(rec)
            forecast_repo.insert_predictions(conn, run_id, pred_rows)
            for kind, frame in feature_tables.items():
                if frame is None or frame.empty:
                    continue
                forecast_repo.replace_feature_table(
                    conn, run_id, kind, frame.to_dict(orient="records")
                )
            forecast_repo.finish_forecast_run(conn, run_id, status="success")
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            try:
                forecast_repo.finish_forecast_run(conn, run_id, status="failed")
            except Exception:  # noqa: BLE001
                log.exception("Could not mark forecast_run %s failed", run_id)
            raise
    log.info("Wrote forecast_run %s (%d predictions)", run_id, len(pred_rows))
    return run_id


def load_outcome_stages_latest_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        # Prefer prediction payload if mart empty
        rows = forecast_repo.get_predictions_for_run(conn)
        if not rows:
            rows = forecast_repo.get_outcome_stages_latest(conn)
    df = _rows_to_df(rows)
    if df.empty or "payload" not in df.columns:
        return df
    # Flatten payload so Mission Control can see forecast_date / facility_name / etc.
    payloads = [p if isinstance(p, dict) else {} for p in df["payload"].tolist()]
    flat = pd.json_normalize(payloads)
    if flat.empty:
        return df
    flat.index = df.index
    for col in flat.columns:
        if col not in df.columns:
            df[col] = flat[col]
    return df


def load_feature_df(
    feature_kind: str,
    *,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = forecast_repo.get_features(conn, feature_kind)
    return _rows_to_df(rows)


def load_cash_series_from_marts(
    *,
    database_url: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    with connection(database_url) as conn:
        actual = _rows_to_df(forecast_repo.get_actual_cash_daily(conn))
        projected = _rows_to_df(forecast_repo.get_projected_cash_daily(conn))
    return actual, projected


def load_prediction_filter_options(
    *,
    run_id: str | None = None,
    database_url: str | None = None,
) -> dict[str, list[str]]:
    with connection(database_url) as conn:
        return forecast_repo.get_prediction_filter_options(conn, run_id=run_id)


def backfill_mission_control_features(
    run_id: str | None = None,
    *,
    database_url: str | None = None,
) -> dict[str, Any]:
    with connection(database_url) as conn:
        stats = forecast_repo.backfill_mission_control_features(conn, run_id=run_id)
    log.info("Backfilled Mission Control features %s", stats)
    return stats
