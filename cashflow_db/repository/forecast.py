"""Forecast run / prediction / feature repository contracts."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Iterable

import psycopg

from cashflow_db.repository import client
from cashflow_db.repository.collection import still_pending_visit_sql
from cashflow_db.repository.insurance import usable_sql


def _prediction_ins_sql() -> str:
    """Prefer prediction ins_name; fall back to filled Eligibility Sheet name."""
    payload_ins = usable_sql("payload->>'ins_name'")
    sheet_ins = usable_sql("wi.insurance_name")
    return f"COALESCE({payload_ins}, {sheet_ins})"


def _prediction_elig_join() -> str:
    return """
        LEFT JOIN ops.eligibility_work_item wi
          ON wi.emr_patient_id = COALESCE(
                NULLIF(BTRIM(fp.webpt_patient_id), ''),
                NULLIF(BTRIM(fp.payload->>'webpt_patient_id'), '')
             )
         AND wi.dos = COALESCE(
                fp.date_of_service,
                CASE
                    WHEN fp.payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN substring(fp.payload->>'date_of_service' from 1 for 10)::date
                    ELSE NULL
                END
             )
    """


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


_INSERT_BATCH = 500


def _prediction_params(run_id: str, r: dict[str, Any]) -> tuple[Any, ...]:
    risk = r.get("risk_flags")
    if not isinstance(risk, (dict, list)):
        risk = {"raw": risk} if risk else {}
    stage = r.get("outcome_stage")
    stage_s = str(stage).strip().lower() if stage is not None else None
    row = r
    if stage_s and stage_s not in _ALLOWED_STAGES:
        row = dict(r)
        row.setdefault("outcome_stage_raw", stage)
        stage_s = None
    payload = {k: v for k, v in row.items() if k not in {
        "visit_id", "outcome_stage", "expected_amount", "expected_pay_date",
        "overdue_days", "denied_amount", "denial_category", "sla_lag_days",
        "forecast_shift_days", "risk_flags", "risk_score",
        "webpt_patient_id", "case_id", "cpt_code", "date_of_service",
    }}
    visit_id = row.get("visit_id")
    if visit_id is not None and str(visit_id).strip() in {"", "nan", "None", "NaT"}:
        visit_id = None
    return (
        run_id,
        visit_id,
        stage_s,
        _json_safe(row.get("expected_amount")),
        _date_or_none(row.get("expected_pay_date")),
        _json_safe(row.get("overdue_days")),
        _json_safe(row.get("denied_amount")),
        row.get("denial_category"),
        _json_safe(row.get("sla_lag_days")),
        _json_safe(row.get("forecast_shift_days")),
        json.dumps(_json_safe(risk), default=str),
        _json_safe(row.get("risk_score")),
        _empty_str_none(row.get("webpt_patient_id")),
        _empty_str_none(row.get("case_id")),
        _empty_str_none(row.get("cpt_code")),
        _date_or_none(row.get("date_of_service")),
        json.dumps(_json_safe(payload), default=str),
    )


def insert_predictions(
    conn: psycopg.Connection,
    run_id: str,
    rows: Iterable[dict[str, Any]],
) -> int:
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
    batch: list[tuple[Any, ...]] = []
    n = 0
    for r in rows:
        batch.append(_prediction_params(run_id, r))
        if len(batch) >= _INSERT_BATCH:
            client.executemany(conn, sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        client.executemany(conn, sql, batch)
        n += len(batch)
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
    sql = """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        ) VALUES (%s::uuid, %s, %s, %s::jsonb)
    """
    batch: list[tuple[Any, ...]] = []
    n = 0
    for r in rows:
        batch.append(
            (
                run_id,
                feature_kind,
                str(r.get("feature_key") or r.get("insurance") or r.get("payer") or n + len(batch)),
                json.dumps(_json_safe(r), default=str),
            )
        )
        if len(batch) >= _INSERT_BATCH:
            client.executemany(conn, sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        client.executemany(conn, sql, batch)
        n += len(batch)
    return n


def get_outcome_stages_latest(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM mart.v_outcome_stages_latest
        """,
    )


def get_prior_forecast_run_id(
    conn: psycopg.Connection,
    *,
    before_as_of: date,
) -> str | None:
    """Latest successful forecast_run strictly before ``before_as_of``."""
    row = client.fetchone(
        conn,
        """
        SELECT forecast_run_id
        FROM analytics.forecast_run
        WHERE status = 'success' AND as_of_date < %s
        ORDER BY as_of_date DESC, created_at DESC
        LIMIT 1
        """,
        (before_as_of,),
    )
    return str(row["forecast_run_id"]) if row else None


def get_forecast_recon_run_id(
    conn: psycopg.Connection,
    *,
    as_of: date,
) -> str | None:
    """Recon run stamped on the latest success forecast_run for this as_of (pack-input)."""
    row = client.fetchone(
        conn,
        """
        SELECT reconciliation_run_id
        FROM analytics.forecast_run
        WHERE status = 'success'
          AND as_of_date = %s
          AND reconciliation_run_id IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (as_of,),
    )
    if not row or not row.get("reconciliation_run_id"):
        return None
    return str(row["reconciliation_run_id"])


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
    return client.fetchall(conn, "SELECT * FROM mart.v_actual_cash_daily ORDER BY period")


def get_projected_cash_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(conn, "SELECT * FROM mart.v_projected_cash_daily ORDER BY period")


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


def list_projected_history_daily(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    settled: date | None = None,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Closest prior forecast $ vs Tracker, plus day-ahead pending and latest-run tail.

    Past days (``period <= settled``): among successful runs with
    ``as_of_date < period``, pick the projection with the smallest
    ``|forecast − tracker|``. Ties break to the newest ``as_of_date``.
    Pending days (``settled < period < as_of``): newest run with
    ``as_of_date < period`` — do not rank against missing Tracker ($0).
    On/after ``as_of``: ``projected_cash_daily`` from the current success run.
    """
    sql = """
        WITH src AS (
            SELECT
                CASE
                    WHEN ff.payload->>'period' ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN substring(ff.payload->>'period' from 1 for 10)::date
                    WHEN ff.feature_key ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN substring(ff.feature_key from 1 for 10)::date
                    ELSE NULL
                END AS period,
                COALESCE((ff.payload->>'amount')::numeric, 0) AS amount,
                fr.as_of_date,
                fr.forecast_run_id,
                fr.created_at
            FROM analytics.forecast_feature ff
            JOIN analytics.forecast_run fr ON fr.forecast_run_id = ff.forecast_run_id
            WHERE fr.status = 'success'
              AND ff.feature_kind = 'projected_cash_daily'
        ),
        actual AS (
            SELECT
                txn_date AS period,
                COALESCE(SUM(amount), 0) AS amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL
              AND txn_date IS NOT NULL
            GROUP BY 1
        ),
        latest AS (
            SELECT forecast_run_id
            FROM analytics.forecast_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
        ),
        past AS (
            SELECT DISTINCT ON (s.period)
                s.period,
                ROUND(s.amount, 2) AS amount,
                s.as_of_date AS forecast_as_of
            FROM src s
            LEFT JOIN actual a ON a.period = s.period
            WHERE s.period IS NOT NULL
              AND s.as_of_date < s.period
              AND (%s::date IS NULL OR s.period <= %s::date)
            ORDER BY s.period,
                     ABS(s.amount - COALESCE(a.amount, 0)) ASC,
                     s.as_of_date DESC,
                     s.created_at DESC
        ),
        pending AS (
            SELECT DISTINCT ON (s.period)
                s.period,
                ROUND(s.amount, 2) AS amount,
                s.as_of_date AS forecast_as_of
            FROM src s
            WHERE s.period IS NOT NULL
              AND s.as_of_date < s.period
              AND %s::date IS NOT NULL
              AND %s::date IS NOT NULL
              AND s.period > %s::date
              AND s.period < %s::date
            ORDER BY s.period,
                     s.as_of_date DESC,
                     s.created_at DESC
        ),
        future AS (
            SELECT
                s.period,
                ROUND(s.amount, 2) AS amount,
                s.as_of_date AS forecast_as_of
            FROM src s
            JOIN latest l ON l.forecast_run_id = s.forecast_run_id
            WHERE s.period IS NOT NULL
              AND %s::date IS NOT NULL
              AND %s::date IS NOT NULL
              AND s.period >= %s::date
        )
        SELECT
            to_char(u.period, 'YYYY-MM-DD') AS period,
            u.amount,
            to_char(u.forecast_as_of, 'YYYY-MM-DD') AS forecast_as_of
        FROM (
            SELECT period, amount, forecast_as_of FROM past
            UNION ALL
            SELECT period, amount, forecast_as_of FROM pending
            UNION ALL
            SELECT period, amount, forecast_as_of FROM future
        ) u
        WHERE (%s::date IS NULL OR u.period >= %s::date)
          AND (%s::date IS NULL OR u.period <= %s::date)
        ORDER BY u.period
    """
    today = as_of or date.today()
    params = [
        settled,
        settled,
        settled,
        today,
        settled,
        today,
        settled,
        today,
        today,
        d0,
        d0,
        d1,
        d1,
    ]
    rows = client.fetchall(conn, sql, params)
    return [
        {
            "period": str(row.get("period") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
            "forecast_as_of": str(row.get("forecast_as_of") or "") or None,
        }
        for row in rows
        if row.get("period")
    ]


def list_projected_history(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    settled: date | None = None,
    as_of: date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Stitched daily expected plus monthly rollup (company-wide)."""
    daily = list_projected_history_daily(
        conn, d0=d0, d1=d1, settled=settled, as_of=as_of
    )
    by_month: dict[str, float] = {}
    as_of_month: dict[str, str] = {}
    for row in daily:
        ym = str(row["period"])[:7]
        if len(ym) < 7:
            continue
        by_month[ym] = by_month.get(ym, 0.0) + float(row["amount"] or 0)
        fa = row.get("forecast_as_of")
        if fa and (ym not in as_of_month or str(fa) > as_of_month[ym]):
            as_of_month[ym] = str(fa)
    monthly = [
        {
            "period": ym,
            "amount": round(amt, 2),
            "forecast_as_of": as_of_month.get(ym),
        }
        for ym, amt in sorted(by_month.items())
    ]
    return {"daily": daily, "monthly": monthly}


def _latest_success_run_id(conn: psycopg.Connection) -> str | None:
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
    return str(row["forecast_run_id"]) if row else None


def get_prediction_filter_options(
    conn: psycopg.Connection,
    *,
    run_id: str | None = None,
) -> dict[str, list[str]]:
    """DISTINCT facility/insurer names for Mission Control filters (no full row fetch)."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return {"facilities": [], "insurers": []}
    fac_rows = client.fetchall(
        conn,
        """
        SELECT DISTINCT NULLIF(BTRIM(payload->>'facility_name'), '') AS v
        FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
          AND NULLIF(BTRIM(payload->>'facility_name'), '') IS NOT NULL
        ORDER BY 1
        """,
        (run_id,),
    )
    ins_rows = client.fetchall(
        conn,
        """
        SELECT DISTINCT NULLIF(BTRIM(payload->>'ins_name'), '') AS v
        FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
          AND NULLIF(BTRIM(payload->>'ins_name'), '') IS NOT NULL
        ORDER BY 1
        """,
        (run_id,),
    )
    return {
        "facilities": [r["v"] for r in fac_rows if r.get("v")],
        "insurers": [r["v"] for r in ins_rows if r.get("v")],
    }


def backfill_mission_control_features(
    conn: psycopg.Connection,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Write monthly / by-facility / by-insurance features for an existing run.

    Monthly is rolled up from projected_cash_daily already on the run.
    Facility/insurance come from prediction payload (on_track/overdue only).
    Postgres does the GROUP BY — callers must not load predictions into pandas.
    """
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return {
            "forecast_run_id": "",
            "monthly": 0,
            "by_facility": 0,
            "by_insurance": 0,
        }

    client.execute(
        conn,
        """
        DELETE FROM analytics.forecast_feature
        WHERE forecast_run_id = %s::uuid
          AND feature_kind IN (
            'projected_cash_monthly',
            'projected_cash_monthly_by_facility',
            'projected_cash_monthly_by_insurance',
            'outcome_stage_counts',
            'overdue_by_insurance',
            'risk_totals_by_insurance'
          )
        """,
        (run_id,),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'projected_cash_monthly',
            month_key,
            jsonb_build_object(
                'period', month_key,
                'amount', ROUND(SUM(amt), 2),
                'line_count', SUM(n)
            )
        FROM (
            SELECT
                CASE
                    WHEN payload->>'period' ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN to_char(substring(payload->>'period' from 1 for 10)::date, 'YYYY-MM')
                    WHEN payload->>'period' ~ '^\\d{4}-\\d{2}$'
                        THEN payload->>'period'
                    ELSE NULL
                END AS month_key,
                COALESCE((payload->>'amount')::numeric, 0) AS amt,
                COALESCE((payload->>'line_count')::numeric, 1) AS n
            FROM analytics.forecast_feature
            WHERE forecast_run_id = %s::uuid
              AND feature_kind = 'projected_cash_daily'
        ) daily
        WHERE month_key IS NOT NULL
        GROUP BY month_key
        """,
        (run_id, run_id),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'projected_cash_monthly_by_facility',
            month_key || '|' || COALESCE(fac, ''),
            jsonb_build_object(
                'period', month_key,
                'facility_name', COALESCE(fac, ''),
                'amount', ROUND(SUM(amt), 2),
                'line_count', COUNT(*)
            )
        FROM (
            SELECT
                to_char(
                    COALESCE(
                        CASE
                            WHEN payload->>'forecast_date' ~ '^\\d{4}-\\d{2}-\\d{2}'
                                THEN substring(payload->>'forecast_date' from 1 for 10)::date
                            ELSE NULL
                        END,
                        expected_pay_date
                    ),
                    'YYYY-MM'
                ) AS month_key,
                NULLIF(BTRIM(payload->>'facility_name'), '') AS fac,
                COALESCE(expected_amount, 0)::numeric AS amt
            FROM analytics.forecast_prediction
            WHERE forecast_run_id = %s::uuid
              AND outcome_stage IN ('on_track', 'overdue')
              AND COALESCE(expected_amount, 0) > 0
        ) src
        WHERE month_key IS NOT NULL
        GROUP BY month_key, fac
        """,
        (run_id, run_id),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'projected_cash_monthly_by_insurance',
            month_key || '|' || COALESCE(ins, ''),
            jsonb_build_object(
                'period', month_key,
                'ins_name', COALESCE(ins, ''),
                'amount', ROUND(SUM(amt), 2),
                'line_count', COUNT(*)
            )
        FROM (
            SELECT
                to_char(
                    COALESCE(
                        CASE
                            WHEN payload->>'forecast_date' ~ '^\\d{4}-\\d{2}-\\d{2}'
                                THEN substring(payload->>'forecast_date' from 1 for 10)::date
                            ELSE NULL
                        END,
                        expected_pay_date
                    ),
                    'YYYY-MM'
                ) AS month_key,
                NULLIF(BTRIM(payload->>'ins_name'), '') AS ins,
                COALESCE(expected_amount, 0)::numeric AS amt
            FROM analytics.forecast_prediction
            WHERE forecast_run_id = %s::uuid
              AND outcome_stage IN ('on_track', 'overdue')
              AND COALESCE(expected_amount, 0) > 0
        ) src
        WHERE month_key IS NOT NULL
        GROUP BY month_key, ins
        """,
        (run_id, run_id),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'outcome_stage_counts',
            COALESCE(outcome_stage, ''),
            jsonb_build_object(
                'outcome_stage', COALESCE(outcome_stage, ''),
                'amount', ROUND(SUM(COALESCE(expected_amount, 0)), 2),
                'line_count', COUNT(*),
                'share_pct', ROUND(
                    100.0 * SUM(COALESCE(expected_amount, 0))
                    / NULLIF(SUM(SUM(COALESCE(expected_amount, 0))) OVER (), 0),
                    1
                )
            )
        FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
        GROUP BY outcome_stage
        """,
        (run_id, run_id),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'overdue_by_insurance',
            COALESCE(ins, ''),
            jsonb_build_object(
                'ins_name', COALESCE(ins, ''),
                'expected_payment', ROUND(amt, 2),
                'avg_overdue_days', days,
                'line_count', n,
                'share_pct', ROUND(100.0 * amt / NULLIF(SUM(amt) OVER (), 0), 1)
            )
        FROM (
            SELECT
                NULLIF(BTRIM(payload->>'ins_name'), '') AS ins,
                SUM(COALESCE(expected_amount, 0)) AS amt,
                ROUND(AVG(overdue_days)::numeric, 1) AS days,
                COUNT(*)::int AS n
            FROM analytics.forecast_prediction
            WHERE forecast_run_id = %s::uuid
              AND outcome_stage = 'overdue'
            GROUP BY 1
        ) g
        """,
        (run_id, run_id),
    )

    client.execute(
        conn,
        """
        INSERT INTO analytics.forecast_feature (
            forecast_run_id, feature_kind, feature_key, payload
        )
        SELECT
            %s::uuid,
            'risk_totals_by_insurance',
            COALESCE(ins, ''),
            jsonb_build_object(
                'ins_name', COALESCE(ins, ''),
                'exposure_amount', ROUND(amt, 2),
                'visit_count', visits,
                'share_pct', ROUND(100.0 * amt / NULLIF(SUM(amt) OVER (), 0), 1)
            )
        FROM (
            SELECT
                NULLIF(BTRIM(payload->>'ins_name'), '') AS ins,
                SUM(COALESCE((payload->>'exposure_amount')::numeric, 0)) AS amt,
                COUNT(DISTINCT NULLIF(BTRIM(payload->>'webpt_patient_id'), ''))::int AS visits
            FROM analytics.forecast_feature
            WHERE forecast_run_id = %s::uuid
              AND feature_kind = 'risk_flags'
            GROUP BY 1
        ) g
        """,
        (run_id, run_id),
    )

    def _count_kind(kind: str) -> int:
        row = client.fetchone(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM analytics.forecast_feature
            WHERE forecast_run_id = %s::uuid AND feature_kind = %s
            """,
            (run_id, kind),
        )
        return int(row["n"]) if row else 0

    return {
        "forecast_run_id": run_id,
        "monthly": _count_kind("projected_cash_monthly"),
        "by_facility": _count_kind("projected_cash_monthly_by_facility"),
        "by_insurance": _count_kind("projected_cash_monthly_by_insurance"),
        "stages": _count_kind("outcome_stage_counts"),
        "overdue_ins": _count_kind("overdue_by_insurance"),
        "risk_ins": _count_kind("risk_totals_by_insurance"),
    }


_EMPTY_AR_STAGES = {
    "on_track_amount": 0.0,
    "on_track_count": 0,
    "overdue_amount": 0.0,
    "overdue_count": 0,
}


_LAND_DATE_SQL = """
        COALESCE(
            CASE
                WHEN payload->>'original_forecast_date' ~ '^\\d{4}-\\d{2}-\\d{2}'
                    THEN substring(payload->>'original_forecast_date' from 1 for 10)::date
                ELSE NULL
            END,
            expected_pay_date
        )
    """

_PACKED_LAND_SQL = """
        COALESCE(
            CASE
                WHEN payload->>'forecast_date' ~ '^\\d{4}-\\d{2}-\\d{2}'
                    THEN substring(payload->>'forecast_date' from 1 for 10)::date
                ELSE NULL
            END,
            expected_pay_date
        )
    """


def _append_name_filters(
    sql: str,
    params: list[Any],
    *,
    facilities: list[str] | None,
    insurers: list[str] | None,
) -> tuple[str, list[Any]]:
    if facilities:
        sql += " AND NULLIF(BTRIM(payload->>'facility_name'), '') = ANY(%s)"
        params.append(list(facilities))
    if insurers:
        sql += " AND NULLIF(BTRIM(payload->>'ins_name'), '') = ANY(%s)"
        params.append(list(insurers))
    return sql, params


def sum_ar_stages_in_range(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """On-track by DOS, overdue by pre-pack land date — Postgres SUM, no row fetch.

    Packed ``forecast_date`` is ignored so a past month still shows AR that was
    expected to land then (or visits still inside SLA).

    When ``d0``/``d1`` are omitted, sums all open on-track + overdue rows
    (clinic/insurance filters still apply).
    """
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return dict(_EMPTY_AR_STAGES)

    params: list[Any] = [run_id]
    if d0 is not None or d1 is not None:
        on_track = ["outcome_stage = 'on_track'"]
        overdue = [f"outcome_stage = 'overdue'"]
        if d0 is not None:
            on_track.append("date_of_service >= %s")
            params.append(d0)
            overdue.append(f"{_LAND_DATE_SQL} >= %s")
            params.append(d0)
        if d1 is not None:
            on_track.append("date_of_service <= %s")
            params.append(d1)
            overdue.append(f"{_LAND_DATE_SQL} <= %s")
            params.append(d1)
        stage_sql = f"(({' AND '.join(on_track)}) OR ({' AND '.join(overdue)}))"
    else:
        stage_sql = "outcome_stage IN ('on_track', 'overdue')"

    sql = f"""
        SELECT outcome_stage,
               COALESCE(SUM(expected_amount), 0) AS amount,
               COUNT(*)::int AS n
        FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
          AND {stage_sql}
    """
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    sql += " GROUP BY outcome_stage"

    rows = client.fetchall(conn, sql, params)
    out = dict(_EMPTY_AR_STAGES)
    for row in rows:
        stage = str(row.get("outcome_stage") or "")
        amt = round(float(row.get("amount") or 0), 2)
        n = int(row.get("n") or 0)
        if stage == "on_track":
            out["on_track_amount"] = amt
            out["on_track_count"] = n
        elif stage == "overdue":
            out["overdue_amount"] = amt
            out["overdue_count"] = n
    return out


def sum_projected_monthly(
    conn: psycopg.Connection,
    *,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
    months: list[str] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Projected (on_track + overdue) cash by land month, AND-ing clinic/insurance."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []

    sql = f"""
        SELECT month_key AS period,
               ROUND(SUM(amt), 2) AS amount,
               COUNT(*)::int AS line_count
        FROM (
            SELECT
                to_char(land_date, 'YYYY-MM') AS month_key,
                COALESCE(expected_amount, 0)::numeric AS amt
            FROM (
                SELECT
                    {_PACKED_LAND_SQL} AS land_date,
                    expected_amount,
                    payload
                FROM analytics.forecast_prediction
                WHERE forecast_run_id = %s::uuid
                  AND outcome_stage IN ('on_track', 'overdue')
                  AND COALESCE(expected_amount, 0) > 0
    """
    params: list[Any] = [run_id]
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    sql += """
            ) src
            WHERE land_date IS NOT NULL
    """
    if d0 is not None:
        sql += " AND land_date >= %s"
        params.append(d0)
    if d1 is not None:
        sql += " AND land_date <= %s"
        params.append(d1)
    sql += """
        ) rolled
        WHERE month_key IS NOT NULL
    """
    if months:
        sql += " AND month_key = ANY(%s)"
        params.append(list(months))
    sql += " GROUP BY month_key ORDER BY month_key"

    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "period": str(row.get("period") or ""),
                "amount": round(float(row.get("amount") or 0), 2),
                "line_count": int(row.get("line_count") or 0),
            }
        )
    return out


def sum_projected_cash(
    conn: psycopg.Connection,
    *,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
    months: list[str] | None = None,
    run_id: str | None = None,
) -> float:
    rows = sum_projected_monthly(
        conn,
        facilities=facilities,
        insurers=insurers,
        d0=d0,
        d1=d1,
        months=months,
        run_id=run_id,
    )
    return round(sum(float(r["amount"]) for r in rows), 2)


def _with_share(rows: list[dict[str, Any]], col: str) -> list[dict[str, Any]]:
    total = sum(float(r.get(col) or 0) for r in rows)
    for row in rows:
        amt = float(row.get(col) or 0)
        row["share_pct"] = round(100.0 * amt / total, 1) if total else 0.0
    return rows


def _prediction_stage_date_sql(
    *,
    d0: date | None,
    d1: date | None,
    params: list[Any],
) -> tuple[str, list[Any]]:
    """Date window for mixed stages: on_track by DOS, overdue by land, others by DOS."""
    if d0 is None and d1 is None:
        return "TRUE", params
    on_track = ["outcome_stage = 'on_track'"]
    overdue = ["outcome_stage = 'overdue'"]
    other = ["outcome_stage NOT IN ('on_track', 'overdue')"]
    if d0 is not None:
        on_track.append("date_of_service >= %s")
        params.append(d0)
        overdue.append(f"{_LAND_DATE_SQL} >= %s")
        params.append(d0)
        other.append("date_of_service >= %s")
        params.append(d0)
    if d1 is not None:
        on_track.append("date_of_service <= %s")
        params.append(d1)
        overdue.append(f"{_LAND_DATE_SQL} <= %s")
        params.append(d1)
        other.append("date_of_service <= %s")
        params.append(d1)
    clause = (
        f"(({' AND '.join(on_track)}) OR ({' AND '.join(overdue)}) "
        f"OR ({' AND '.join(other)}))"
    )
    return clause, params


def summarize_outcome_stages(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """GROUP BY outcome_stage — Postgres aggregation, no row fetch."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    date_sql, params = _prediction_stage_date_sql(d0=d0, d1=d1, params=params)
    sql = f"""
        SELECT outcome_stage,
               COALESCE(SUM(expected_amount), 0) AS amount,
               COUNT(*)::int AS line_count
        FROM analytics.forecast_prediction
        WHERE forecast_run_id = %s::uuid
          AND {date_sql}
    """
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    sql += " GROUP BY outcome_stage ORDER BY line_count DESC"
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "outcome_stage": str(row.get("outcome_stage") or ""),
                "amount": round(float(row.get("amount") or 0), 2),
                "line_count": int(row.get("line_count") or 0),
            }
        )
    return _with_share(out, "amount")


def summarize_overdue_by_insurance(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    run_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Overdue $ / avg days by insurer — Postgres GROUP BY."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    sql = f"""
        SELECT {_prediction_ins_sql()} AS ins_name,
               ROUND(SUM(COALESCE(expected_amount, 0))::numeric, 2) AS expected_payment,
               ROUND(AVG(overdue_days)::numeric, 1) AS avg_overdue_days,
               COUNT(*)::int AS line_count
        FROM analytics.forecast_prediction fp
        {_prediction_elig_join()}
        WHERE fp.forecast_run_id = %s::uuid
          AND fp.outcome_stage = 'overdue'
    """
    if d0 is not None:
        sql += f" AND {_LAND_DATE_SQL} >= %s"
        params.append(d0)
    if d1 is not None:
        sql += f" AND {_LAND_DATE_SQL} <= %s"
        params.append(d1)
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    sql += (
        f" AND {_prediction_ins_sql()} IS NOT NULL"
        " GROUP BY 1 ORDER BY expected_payment DESC"
    )
    if limit is not None and int(limit) > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "ins_name": str(row.get("ins_name") or ""),
                "expected_payment": round(float(row.get("expected_payment") or 0), 2),
                "avg_overdue_days": round(float(row.get("avg_overdue_days") or 0), 1),
                "line_count": int(row.get("line_count") or 0),
            }
        )
    return _with_share(out, "expected_payment")


def list_overdue_claims(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    q: str | None = None,
    run_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Overdue forecast lines past Insurance-behavior land date."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    sql = f"""
        SELECT
            COALESCE(
                NULLIF(BTRIM(fp.payload->>'patient_name'), ''),
                NULLIF(BTRIM(wi.patient_name), ''),
                ''
            ) AS patient_name,
            COALESCE(
                NULLIF(BTRIM(fp.webpt_patient_id), ''),
                NULLIF(BTRIM(fp.payload->>'webpt_patient_id'), ''),
                NULLIF(BTRIM(wi.emr_patient_id), ''),
                ''
            ) AS emr_patient_id,
            COALESCE(
                fp.date_of_service,
                CASE
                    WHEN fp.payload->>'date_of_service' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                        THEN substring(fp.payload->>'date_of_service' from 1 for 10)::date
                    ELSE NULL
                END
            ) AS dos,
            NULLIF(BTRIM(fp.payload->>'facility_name'), '') AS facility_name,
            {_prediction_ins_sql()} AS ins_name,
            NULLIF(BTRIM(COALESCE(fp.cpt_code, fp.payload->>'cpt_code')), '') AS cpt_code,
            ROUND(COALESCE(fp.expected_amount, 0)::numeric, 2) AS expected_amount,
            {_LAND_DATE_SQL} AS expected_land_date,
            fp.overdue_days,
            fp.sla_lag_days
        FROM analytics.forecast_prediction fp
        {_prediction_elig_join()}
        WHERE fp.forecast_run_id = %s::uuid
          AND fp.outcome_stage = 'overdue'
    """
    sql += " AND " + still_pending_visit_sql(
        emr_expr="""COALESCE(
                NULLIF(BTRIM(fp.webpt_patient_id), ''),
                NULLIF(BTRIM(fp.payload->>'webpt_patient_id'), ''),
                NULLIF(BTRIM(wi.emr_patient_id), ''),
                ''
            )""",
        dos_expr="""COALESCE(
                fp.date_of_service,
                CASE
                    WHEN fp.payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN substring(fp.payload->>'date_of_service' from 1 for 10)::date
                    ELSE NULL
                END
            )""",
        status_expr="wi.source_visit_status",
    )
    if d0 is not None:
        sql += f" AND {_LAND_DATE_SQL} >= %s"
        params.append(d0)
    if d1 is not None:
        sql += f" AND {_LAND_DATE_SQL} <= %s"
        params.append(d1)
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    needle = str(q or "").strip()
    if needle:
        like = f"%{needle}%"
        sql += """
          AND (
            COALESCE(fp.payload->>'patient_name', wi.patient_name, '') ILIKE %s
            OR COALESCE(fp.webpt_patient_id, fp.payload->>'webpt_patient_id', wi.emr_patient_id, '') ILIKE %s
            OR COALESCE(fp.payload->>'ins_name', wi.insurance_name, '') ILIKE %s
          )
        """
        params.extend([like, like, like])
    sql += """
        ORDER BY COALESCE(fp.overdue_days, 0) DESC, COALESCE(fp.expected_amount, 0) DESC
        LIMIT %s
    """
    params.append(max(1, min(int(limit or 500), 2000)))
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        land = row.get("expected_land_date")
        dos = row.get("dos")
        out.append(
            {
                "patient_name": str(row.get("patient_name") or ""),
                "emr_patient_id": str(row.get("emr_patient_id") or ""),
                "dos": dos.isoformat() if hasattr(dos, "isoformat") else (str(dos) if dos else ""),
                "facility_name": str(row.get("facility_name") or ""),
                "ins_name": str(row.get("ins_name") or ""),
                "cpt_code": str(row.get("cpt_code") or ""),
                "expected_amount": round(float(row.get("expected_amount") or 0), 2),
                "expected_land_date": land.isoformat()
                if hasattr(land, "isoformat")
                else (str(land) if land else ""),
                "overdue_days": int(row.get("overdue_days") or 0),
                "sla_lag_days": (
                    round(float(row.get("sla_lag_days")), 1)
                    if row.get("sla_lag_days") is not None
                    else None
                ),
            }
        )
    return out


def _risk_feature_filters(
    sql: str,
    params: list[Any],
    *,
    facilities: list[str] | None,
    insurers: list[str] | None,
    d0: date | None,
    d1: date | None,
) -> tuple[str, list[Any]]:
    if facilities:
        sql += " AND NULLIF(BTRIM(payload->>'facility_name'), '') = ANY(%s)"
        params.append(list(facilities))
    if insurers:
        sql += " AND NULLIF(BTRIM(payload->>'ins_name'), '') = ANY(%s)"
        params.append(list(insurers))
    if d0 is not None:
        sql += """
          AND CASE
                WHEN payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                    THEN substring(payload->>'date_of_service' from 1 for 10)::date
                ELSE NULL
              END >= %s
        """
        params.append(d0)
    if d1 is not None:
        sql += """
          AND CASE
                WHEN payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                    THEN substring(payload->>'date_of_service' from 1 for 10)::date
                ELSE NULL
              END <= %s
        """
        params.append(d1)
    return sql, params


def summarize_risk_by_insurance(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    run_id: str | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """At-risk $ by insurer from risk_flags feature rows — no prediction scan."""
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    payload_ins = usable_sql("ff.payload->>'ins_name'")
    sheet_ins = usable_sql("wi.insurance_name")
    sql = f"""
        SELECT COALESCE({payload_ins}, {sheet_ins}) AS ins_name,
               ROUND(SUM(COALESCE((ff.payload->>'exposure_amount')::numeric, 0)), 2)
                   AS exposure_amount,
               COUNT(DISTINCT NULLIF(BTRIM(ff.payload->>'webpt_patient_id'), ''))::int
                   AS visit_count
        FROM analytics.forecast_feature ff
        LEFT JOIN ops.eligibility_work_item wi
          ON wi.emr_patient_id = NULLIF(BTRIM(ff.payload->>'webpt_patient_id'), '')
         AND wi.dos = CASE
                WHEN ff.payload->>'date_of_service' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                    THEN substring(ff.payload->>'date_of_service' from 1 for 10)::date
                ELSE NULL
              END
        WHERE ff.forecast_run_id = %s::uuid
          AND ff.feature_kind = 'risk_flags'
    """
    sql, params = _risk_feature_filters(
        sql, params, facilities=facilities, insurers=insurers, d0=d0, d1=d1
    )
    sql += (
        f" AND COALESCE({payload_ins}, {sheet_ins}) IS NOT NULL"
        " GROUP BY 1 ORDER BY exposure_amount DESC LIMIT %s"
    )
    params.append(int(limit) if limit > 0 else 40)
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "ins_name": str(row.get("ins_name") or ""),
                "exposure_amount": round(float(row.get("exposure_amount") or 0), 2),
                "visit_count": int(row.get("visit_count") or 0),
            }
        )
    return _with_share(out, "exposure_amount")


def summarize_risk_by_flag(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    sql = """
        SELECT COALESCE(NULLIF(BTRIM(payload->>'risk_flag'), ''), '') AS risk_flag,
               ROUND(SUM(COALESCE((payload->>'exposure_amount')::numeric, 0)), 2)
                   AS exposure_amount
        FROM analytics.forecast_feature
        WHERE forecast_run_id = %s::uuid
          AND feature_kind = 'risk_flags'
    """
    sql, params = _risk_feature_filters(
        sql, params, facilities=facilities, insurers=insurers, d0=d0, d1=d1
    )
    sql += " GROUP BY 1 ORDER BY exposure_amount DESC"
    rows = client.fetchall(conn, sql, params)
    return [
        {
            "risk_flag": str(row.get("risk_flag") or ""),
            "exposure_amount": round(float(row.get("exposure_amount") or 0), 2),
        }
        for row in rows
        if row.get("risk_flag")
    ]


def sum_projected_by_name(
    conn: psycopg.Connection,
    *,
    name_field: str,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
    months: list[str] | None = None,
    run_id: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Projected cash grouped by facility_name or ins_name (AND filters)."""
    col = "facility_name" if name_field == "facility_name" else "ins_name"
    if not run_id:
        run_id = _latest_success_run_id(conn)
    if not run_id:
        return []
    params: list[Any] = [run_id]
    sql = f"""
        SELECT COALESCE(NULLIF(BTRIM(payload->>'{col}'), ''), '') AS name,
               ROUND(SUM(amt), 2) AS amount
        FROM (
            SELECT
                payload,
                COALESCE(expected_amount, 0)::numeric AS amt,
                {_PACKED_LAND_SQL} AS land_date
            FROM analytics.forecast_prediction
            WHERE forecast_run_id = %s::uuid
              AND outcome_stage IN ('on_track', 'overdue')
              AND COALESCE(expected_amount, 0) > 0
    """
    sql, params = _append_name_filters(
        sql, params, facilities=facilities, insurers=insurers
    )
    sql += """
        ) src
        WHERE land_date IS NOT NULL
    """
    if d0 is not None:
        sql += " AND land_date >= %s"
        params.append(d0)
    if d1 is not None:
        sql += " AND land_date <= %s"
        params.append(d1)
    if months:
        sql += " AND to_char(land_date, 'YYYY-MM') = ANY(%s)"
        params.append(list(months))
    sql += """
        GROUP BY 1
        ORDER BY amount DESC
        LIMIT %s
    """
    params.append(int(limit) if limit > 0 else 25)
    rows = client.fetchall(conn, sql, params)
    key = "facility_name" if col == "facility_name" else "ins_name"
    return [
        {key: str(row.get("name") or ""), "amount": round(float(row.get("amount") or 0), 2)}
        for row in rows
    ]

