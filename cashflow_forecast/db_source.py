"""Load forecast inputs / write outputs via cashflow_db.repository (no CSV SoT)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

from cashflow_db.repository import claims as claims_repo
from cashflow_db.repository import connection
from cashflow_db.repository import forecast as forecast_repo
from cashflow_db.repository import insurance as ins_repo
from cashflow_db.repository import payments as pay_repo
from cashflow_db.repository import reconciliation as recon_repo
from cashflow_db.repository import visits as visit_repo

log = logging.getLogger(__name__)


def _rows_to_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def load_reconciliation_lines_df(
    *,
    run_id: str | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = recon_repo.get_lines(conn, run_id=run_id)
    df = _rows_to_df(rows)
    if not df.empty and "date_of_service" in df.columns:
        df["date_of_service"] = pd.to_datetime(df["date_of_service"], errors="coerce").dt.date
    return df


def load_reconciliation_visits_df(
    *,
    run_id: str | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = recon_repo.get_visit_aggs(conn, run_id=run_id)
    return _rows_to_df(rows)


def load_payments_unified_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = pay_repo.get_eob_payments_unified(conn)
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


def load_clinical_ar_lines_df(
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    database_url: str | None = None,
) -> pd.DataFrame:
    """Service lines as AR-like rows for Jan–May style pending volume."""
    with connection(database_url) as conn:
        rows = visit_repo.get_service_lines_for_reconcile(
            conn, service_from=service_from, service_to=service_to
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


def load_deposits_df(*, database_url: str | None = None) -> pd.DataFrame:
    with connection(database_url) as conn:
        rows = pay_repo.get_bank_deposits(conn)
    df = _rows_to_df(rows)
    if not df.empty:
        if "bank_posting_date" in df.columns:
            df["deposit_date"] = df["bank_posting_date"]
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    return df


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
