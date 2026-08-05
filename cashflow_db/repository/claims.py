"""Claims, denials, and audit finding repository contracts."""

from __future__ import annotations

from typing import Any

import psycopg

from cashflow_db.repository import client


def get_denial_records(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT
            d.*,
            c.claim_number,
            p.webpt_patient_id
        FROM billing.denial_record d
        LEFT JOIN billing.claim c ON c.claim_id = d.claim_id
        LEFT JOIN core.patient p ON p.patient_id = c.patient_id
        ORDER BY d.denial_date NULLS LAST
        """,
    )


def get_audit_findings(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT
            af.*,
            p.webpt_patient_id
        FROM billing.audit_finding af
        LEFT JOIN core.patient p ON p.patient_id = af.patient_id
        ORDER BY af.finding_kind, af.rule_id
        """,
    )


def get_claim_lines(conn: psycopg.Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    lim = f"LIMIT {int(limit)}" if limit else ""
    return client.fetchall(
        conn,
        f"""
        SELECT cl.*, c.claim_number, c.patient_id, c.status_current
        FROM billing.claim_line cl
        JOIN billing.claim c ON c.claim_id = cl.claim_id
        ORDER BY cl.cpt_code
        {lim}
        """,
    )


def count_denial_records(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.denial_record")
    return int(row["n"]) if row else 0


def count_audit_findings(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.audit_finding")
    return int(row["n"]) if row else 0
