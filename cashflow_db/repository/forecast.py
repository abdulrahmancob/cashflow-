"""Forecast run / prediction / feature repository contracts."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Iterable

import psycopg

from cashflow_db.repository import client


def _json_safe(value: Any) -> Any:
    """Convert NaN/Inf/NA to None so json.dumps emits valid JSONB."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    # pandas / numpy scalars
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    return value


def _date_or_none(value: Any) -> Any:
    value = _json_safe(value)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _empty_str_none(value: Any) -> Any:
    value = _json_safe(value)
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


def create_forecast_run(
    conn: psycopg.Connection,
    *,
    algorithm_version: str,
    as_of_date: date,
    params: dict[str, Any] | None = None,
    source_etl_run_ids: list[str] | None = None,
    reconciliation_run_id: str | None = None,
    rules_version: str | None = None,
    status: str = "running",
) -> str:
    merged = dict(params or {})
    merged["source_etl_run_ids"] = source_etl_run_ids or []
    merged["rules_version"] = rules_version
    row = client.fetchone(
        conn,
        """
        INSERT INTO analytics.forecast_run (
            algorithm_version, params, as_of_date, status,
            reconciliation_run_id, rules_version, source_etl_run_ids
        )
        VALUES (%s, %s::jsonb, %s, %s, %s::uuid, %s, %s::jsonb)
        RETURNING forecast_run_id
        """,
        (
            algorithm_version,
            json.dumps(merged, default=str),
            as_of_date,
            status,
            reconciliation_run_id,
            rules_version,
            json.dumps(source_etl_run_ids or []),
        ),
    )
    assert row
    return str(row["forecast_run_id"])


def finish_forecast_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str = "success",
) -> None:
    client.execute(
        conn,
        """
        UPDATE analytics.forecast_run
        SET status = %s
        WHERE forecast_run_id = %s::uuid
        """,
        (status, run_id),
    )


_ALLOWED_STAGES = frozenset(
    {"paid", "on_track", "overdue", "rejected", "denied", "zero_pay"}
)


def insert_predictions(
    conn: psycopg.Connection,
    run_id: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    n = 0
    sql = """
        INSERT INTO analytics.forecast_prediction (
            forecast_run_id, visit_id, outcome_stage, expected_amount,
            expected_pay_date, overdue_days, denied_amount, denial_category,
            sla_lag_days, forecast_shift_days, risk_flags, risk_score,
            webpt_patient_id, case_id, cpt_code, date_of_service, payload
        ) VALUES (
            %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
            %s, %s, %s, %s, %s::jsonb
        )
    """
    for r in rows:
        risk = r.get("risk_flags")
        if not isinstance(risk, (dict, list)):
            risk = {"raw": risk} if risk else {}
        stage = r.get("outcome_stage")
        stage_s = str(stage).strip().lower() if stage is not None else None
        if stage_s and stage_s not in _ALLOWED_STAGES:
            # Preserve original stage in payload; column stays null if unknown
            r = dict(r)
            r.setdefault("outcome_stage_raw", stage)
            stage_s = None
        payload = {k: v for k, v in r.items() if k not in {
            "visit_id", "outcome_stage", "expected_amount", "expected_pay_date",
            "overdue_days", "denied_amount", "denial_category", "sla_lag_days",
            "forecast_shift_days", "risk_flags", "risk_score",
            "webpt_patient_id", "case_id", "cpt_code", "date_of_service",
        }}
        visit_id = r.get("visit_id")
        if visit_id is not None and str(visit_id).strip() in {"", "nan", "None", "NaT"}:
            visit_id = None
        risk_score = _json_safe(r.get("risk_score"))
        client.execute(
            conn,
            sql,
            (
                run_id,
                visit_id,
                stage_s,
                _json_safe(r.get("expected_amount")),
                _date_or_none(r.get("expected_pay_date")),
                _json_safe(r.get("overdue_days")),
                _json_safe(r.get("denied_amount")),
                r.get("denial_category"),
                _json_safe(r.get("sla_lag_days")),
                _json_safe(r.get("forecast_shift_days")),
                json.dumps(_json_safe(risk), default=str),
                risk_score,
                _empty_str_none(r.get("webpt_patient_id")),
                _empty_str_none(r.get("case_id")),
                _empty_str_none(r.get("cpt_code")),
                _date_or_none(r.get("date_of_service")),
                json.dumps(_json_safe(payload), default=str),
            ),
        )
        n += 1
    return n


def replace_feature_table(
    conn: psycopg.Connection,
    run_id: str,
    feature_kind: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    client.execute(
        conn,
        """
        DELETE FROM analytics.forecast_feature
        WHERE forecast_run_id = %s::uuid AND feature_kind = %s
        """,
        (run_id, feature_kind),
    )
    n = 0
    for r in rows:
        client.execute(
            conn,
            """
            INSERT INTO analytics.forecast_feature (
                forecast_run_id, feature_kind, feature_key, payload
            ) VALUES (%s::uuid, %s, %s, %s::jsonb)
            """,
            (
                run_id,
                feature_kind,
                str(r.get("feature_key") or r.get("insurance") or r.get("payer") or n),
                json.dumps(_json_safe(r), default=str),
            ),
        )
        n += 1
    return n


def get_outcome_stages_latest(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM mart.v_outcome_stages_latest
        """,
    )


def get_predictions_for_run(
    conn: psycopg.Connection,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    if not run_id:
        row = client.fetchone(
            conn,
            """
            SELECT forecast_run_id
            FROM analytics.forecast_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
            """,
        )
        if not row:
            return []
        run_id = str(row["forecast_run_id"])
    return client.fetchall(
        conn,
        """
        SELECT * FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
        """,
        (run_id,),
    )


def get_features(
    conn: psycopg.Connection,
    feature_kind: str,
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    if run_id:
        rows = client.fetchall(
            conn,
            """
            SELECT * FROM analytics.forecast_feature
            WHERE forecast_run_id = %s::uuid AND feature_kind = %s
            """,
            (run_id, feature_kind),
        )
    else:
        rows = client.fetchall(
            conn,
            """
            SELECT ff.*
            FROM analytics.forecast_feature ff
            JOIN analytics.forecast_run fr ON fr.forecast_run_id = ff.forecast_run_id
            WHERE fr.status = 'success' AND ff.feature_kind = %s
              AND fr.created_at = (
                  SELECT MAX(created_at) FROM analytics.forecast_run WHERE status = 'success'
              )
            """,
            (feature_kind,),
        )
    out = []
    for r in rows:
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        flat = dict(payload) if isinstance(payload, dict) else {"payload": payload}
        flat["feature_key"] = r.get("feature_key")
        flat["forecast_run_id"] = r.get("forecast_run_id")
        out.append(flat)
    return out


def get_actual_cash_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(conn, "SELECT * FROM mart.v_actual_cash_daily ORDER BY cash_date")


def get_projected_cash_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(conn, "SELECT * FROM mart.v_projected_cash_daily ORDER BY cash_date")


def get_snowflake_kpi(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    lim = f"LIMIT {int(limit)}" if limit else ""
    return client.fetchall(
        conn,
        f"SELECT * FROM analytics.snowflake_visit_kpi ORDER BY date_of_service {lim}",
    )
