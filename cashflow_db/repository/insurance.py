"""Payor behavior / insurance analytics repository contracts."""

from __future__ import annotations

import json
from typing import Any, Iterable

import psycopg

from cashflow_db.repository import client


def replace_payor_behavior_summary(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None,
    rows: Iterable[dict[str, Any]],
) -> int:
    if reconciliation_run_id:
        client.execute(
            conn,
            "DELETE FROM analytics.payor_behavior_summary WHERE reconciliation_run_id = %s::uuid",
            (reconciliation_run_id,),
        )
    else:
        client.execute(conn, "DELETE FROM analytics.payor_behavior_summary")
    n = 0
    sql = """
        INSERT INTO analytics.payor_behavior_summary (
            reconciliation_run_id, payor_key, payor_raw, check_count,
            median_cash_velocity_days, p75_cash_velocity_days,
            median_eob_to_deposit_days, deposit_weekday_profile,
            payload
        ) VALUES (
            %s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb
        )
    """
    for r in rows:
        payload = {k: v for k, v in r.items() if k not in {
            "payor_key", "payor_raw", "check_count",
            "median_cash_velocity_days", "p75_cash_velocity_days",
            "median_eob_to_deposit_days", "deposit_weekday_profile",
        }}
        client.execute(
            conn,
            sql,
            (
                reconciliation_run_id,
                r.get("payor_key") or r.get("insurance_key") or r.get("payor"),
                r.get("payor_raw") or r.get("payor"),
                r.get("check_count"),
                r.get("median_cash_velocity_days") or r.get("cash_velocity_days"),
                r.get("p75_cash_velocity_days"),
                r.get("median_eob_to_deposit_days") or r.get("eob_to_deposit_days"),
                json.dumps(r.get("deposit_weekday_profile") or {}),
                json.dumps(payload, default=str),
            ),
        )
        n += 1
    return n


def _date_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def replace_checks_timeline(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None,
    rows: Iterable[dict[str, Any]],
) -> int:
    if reconciliation_run_id:
        client.execute(
            conn,
            "DELETE FROM analytics.checks_timeline WHERE reconciliation_run_id = %s::uuid",
            (reconciliation_run_id,),
        )
    else:
        client.execute(conn, "DELETE FROM analytics.checks_timeline")
    n = 0
    sql = """
        INSERT INTO analytics.checks_timeline (
            reconciliation_run_id, check_eft_num, payor_raw, eob_date,
            deposit_date, paid_amount, payload
        ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
    """
    for r in rows:
        client.execute(
            conn,
            sql,
            (
                reconciliation_run_id,
                r.get("check_eft_num"),
                r.get("payor_raw") or r.get("payor"),
                _date_or_none(r.get("eob_date")),
                _date_or_none(r.get("deposit_date")),
                r.get("paid_amount"),
                json.dumps(r, default=str),
            ),
        )
        n += 1
    return n


def get_payor_behavior_summary(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if reconciliation_run_id:
        rows = client.fetchall(
            conn,
            """
            SELECT * FROM analytics.payor_behavior_summary
            WHERE reconciliation_run_id = %s::uuid
            ORDER BY payor_key
            """,
            (reconciliation_run_id,),
        )
    else:
        rows = client.fetchall(
            conn,
            """
            SELECT DISTINCT ON (payor_key) *
            FROM analytics.payor_behavior_summary
            ORDER BY payor_key, created_at DESC
            """,
        )
    # Flatten payload for forecast consumers expecting CSV columns
    out: list[dict[str, Any]] = []
    for r in rows:
        flat = dict(r)
        payload = flat.pop("payload", None) or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                flat.setdefault(k, v)
        out.append(flat)
    return out


def get_checks_timeline(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if reconciliation_run_id:
        return client.fetchall(
            conn,
            """
            SELECT * FROM analytics.checks_timeline
            WHERE reconciliation_run_id = %s::uuid
            ORDER BY deposit_date NULLS LAST, eob_date NULLS LAST
            """,
            (reconciliation_run_id,),
        )
    return client.fetchall(
        conn,
        """
        SELECT * FROM analytics.checks_timeline
        ORDER BY created_at DESC
        LIMIT 50000
        """,
    )


def get_plans_of_care(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT
            poc.poc_id,
            poc.date_of_plan_of_care,
            poc.frequency,
            poc.duration,
            poc.plan_text,
            p.webpt_patient_id AS patient_id,
            ph.patient_name,
            pc.webpt_case_id AS case_id
        FROM docs.plan_of_care_detail poc
        JOIN docs.document d ON d.document_id = poc.document_id
        LEFT JOIN core.patient p ON p.patient_id = d.patient_id
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.patient_case pc ON pc.case_pk = d.case_pk
        ORDER BY poc.date_of_plan_of_care NULLS LAST
        """,
    )
