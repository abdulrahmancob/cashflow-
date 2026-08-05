"""Operational spine: reconciliation runs and lines."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable
from uuid import UUID

import psycopg

from cashflow_db.repository import client


def create_reconciliation_run(
    conn: psycopg.Connection,
    *,
    source_etl_run_ids: list[str] | None = None,
    rules_version: str | None = None,
    notes: str | None = None,
    status: str = "running",
) -> str:
    row = client.fetchone(
        conn,
        """
        INSERT INTO billing.reconciliation_run (
            status, source_etl_run_ids, rules_version, notes
        )
        VALUES (%s, %s::jsonb, %s, %s)
        RETURNING reconciliation_run_id
        """,
        (
            status,
            json.dumps(source_etl_run_ids or []),
            rules_version,
            notes,
        ),
    )
    assert row
    return str(row["reconciliation_run_id"])


def finish_reconciliation_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str = "success",
    row_count: int | None = None,
    notes: str | None = None,
) -> None:
    client.execute(
        conn,
        """
        UPDATE billing.reconciliation_run
        SET finished_at = now(),
            status = %s,
            row_count = %s,
            notes = COALESCE(%s, notes)
        WHERE reconciliation_run_id = %s::uuid
        """,
        (status, row_count, notes, run_id),
    )


def latest_reconciliation_run_id(conn: psycopg.Connection) -> str | None:
    row = client.fetchone(
        conn,
        """
        SELECT reconciliation_run_id
        FROM billing.reconciliation_run
        WHERE status = 'success'
        ORDER BY created_at DESC
        LIMIT 1
        """,
    )
    return str(row["reconciliation_run_id"]) if row else None


def replace_reconciliation_lines(
    conn: psycopg.Connection,
    run_id: str,
    lines: Iterable[dict[str, Any]],
) -> int:
    """Replace all lines for a run (idempotent per run_id)."""
    client.execute(
        conn,
        "DELETE FROM billing.reconciliation_line WHERE reconciliation_run_id = %s::uuid",
        (run_id,),
    )
    n = 0
    sql = """
        INSERT INTO billing.reconciliation_line (
            reconciliation_run_id, webpt_patient_id, patient_name, dob,
            facility_id, facility_name, case_id, ins_name, insurance_note,
            insurance_revflow, date_of_service, cpt_code, modifier, status,
            paid_amount, allowed_amount, adjustment_amount, deductible_amount,
            eob_date, check_eft_num, carcs, expected_copay, expected_deductible,
            match_level, confidence, insurance_mismatch, daily_note_id, note_file,
            visit_id, service_line_id, eob_line_id, patient_id, case_pk
        ) VALUES (
            %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid
        )
    """
    for line in lines:
        client.execute(
            conn,
            sql,
            (
                run_id,
                line.get("webpt_patient_id"),
                line.get("patient_name"),
                line.get("dob"),
                line.get("facility_id"),
                line.get("facility_name"),
                line.get("case_id"),
                line.get("ins_name"),
                line.get("insurance_note"),
                line.get("insurance_revflow"),
                line.get("date_of_service"),
                line.get("cpt_code"),
                line.get("modifier"),
                line.get("status"),
                line.get("paid_amount"),
                line.get("allowed_amount"),
                line.get("adjustment_amount"),
                line.get("deductible_amount"),
                line.get("eob_date"),
                line.get("check_eft_num"),
                line.get("carcs"),
                line.get("expected_copay"),
                line.get("expected_deductible"),
                line.get("match_level"),
                line.get("confidence"),
                line.get("insurance_mismatch"),
                line.get("daily_note_id"),
                line.get("note_file"),
                _uuid_or_none(line.get("visit_id")),
                _uuid_or_none(line.get("service_line_id")),
                _uuid_or_none(line.get("eob_line_id")),
                _uuid_or_none(line.get("patient_id")),
                _uuid_or_none(line.get("case_pk")),
            ),
        )
        n += 1
    return n


def replace_visit_aggs(
    conn: psycopg.Connection,
    run_id: str,
    visits: Iterable[dict[str, Any]],
) -> int:
    client.execute(
        conn,
        "DELETE FROM billing.reconciliation_visit_agg WHERE reconciliation_run_id = %s::uuid",
        (run_id,),
    )
    n = 0
    sql = """
        INSERT INTO billing.reconciliation_visit_agg (
            reconciliation_run_id, facility_id, case_id, webpt_patient_id,
            patient_name, dob, facility_name, date_of_service,
            total_billed_cpts, total_paid, matched_paid, bonus_paid, unmatched_paid,
            visit_paid_total, unmatched_cpts, paid_lines, pending_lines, visit_status,
            primary_check_number, primary_check_date, primary_check_amount,
            secondary_check_number, secondary_check_date, secondary_check_amount
        ) VALUES (
            %s::uuid, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
    """
    for v in visits:
        client.execute(
            conn,
            sql,
            (
                run_id,
                v.get("facility_id"),
                v.get("case_id"),
                v.get("webpt_patient_id"),
                v.get("patient_name"),
                v.get("dob"),
                v.get("facility_name"),
                v.get("date_of_service"),
                v.get("total_billed_cpts"),
                v.get("total_paid"),
                v.get("matched_paid"),
                v.get("bonus_paid"),
                v.get("unmatched_paid"),
                v.get("visit_paid_total"),
                v.get("unmatched_cpts"),
                v.get("paid_lines"),
                v.get("pending_lines"),
                v.get("visit_status"),
                v.get("primary_check_number"),
                v.get("primary_check_date"),
                v.get("primary_check_amount"),
                v.get("secondary_check_number"),
                v.get("secondary_check_date"),
                v.get("secondary_check_amount"),
            ),
        )
        n += 1
    return n


def get_lines(
    conn: psycopg.Connection,
    *,
    run_id: str | None = None,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Return reconciliation lines for a run (latest success if run_id omitted)."""
    rid = run_id or latest_reconciliation_run_id(conn)
    if not rid:
        return []
    clauses = ["reconciliation_run_id = %s::uuid"]
    params: list[Any] = [rid]
    if as_of:
        clauses.append("date_of_service <= %s")
        params.append(as_of)
    return client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.reconciliation_line
        WHERE {' AND '.join(clauses)}
        ORDER BY date_of_service, webpt_patient_id, cpt_code
        """,
        params,
    )


def get_visit_aggs(
    conn: psycopg.Connection,
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    rid = run_id or latest_reconciliation_run_id(conn)
    if not rid:
        return []
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.reconciliation_visit_agg
        WHERE reconciliation_run_id = %s::uuid
        ORDER BY date_of_service, webpt_patient_id
        """,
        (rid,),
    )


def latest_etl_run_ids(
    conn: psycopg.Connection,
    source_systems: list[str] | None = None,
) -> list[str]:
    if source_systems:
        rows = client.fetchall(
            conn,
            """
            SELECT DISTINCT ON (source_system) etl_run_id::text
            FROM etl.etl_run
            WHERE status = 'success' AND source_system = ANY(%s)
            ORDER BY source_system, finished_at DESC NULLS LAST
            """,
            (source_systems,),
        )
    else:
        rows = client.fetchall(
            conn,
            """
            SELECT etl_run_id::text
            FROM etl.etl_run
            WHERE status = 'success'
            ORDER BY finished_at DESC NULLS LAST
            LIMIT 20
            """,
        )
    return [r["etl_run_id"] for r in rows]


def _uuid_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return str(value)
    return str(value)
