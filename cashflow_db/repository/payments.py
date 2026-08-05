"""Patient payments, EOB, and bank deposit repository contracts."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from cashflow_db.repository import client


def get_patient_payments(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if service_from:
        clauses.append("pp.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("pp.service_date <= %s")
        params.append(service_to)
    lim = f"LIMIT {int(limit)}" if limit else ""
    return client.fetchall(
        conn,
        f"""
        SELECT
            pp.*,
            p.webpt_patient_id,
            pc.webpt_case_id AS case_id
        FROM billing.patient_payment pp
        JOIN core.patient p ON p.patient_id = pp.patient_id
        LEFT JOIN core.patient_case pc ON pc.case_pk = pp.case_pk
        WHERE {' AND '.join(clauses)}
        ORDER BY pp.service_date NULLS LAST
        {lim}
        """,
        params,
    )


def get_eob_payments_unified(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Flatten eob_line ⋈ eob_check into payments_unified-shaped rows."""
    return client.fetchall(
        conn,
        """
        SELECT
            el.revflow_patient_id,
            p.webpt_patient_id,
            ph.patient_name AS first_name,
            NULL::text AS last_name,
            p.name_key,
            f.name AS facility_name,
            el.date_of_service,
            el.cpt_code,
            el.modifiers AS modifier,
            el.units,
            el.billed_amount,
            el.allowed_amount,
            el.paid_amount,
            el.adjustment_amount,
            el.deductible_amount,
            el.carcs,
            ec.payor_raw AS payor,
            ec.check_eft_num,
            ec.eob_date,
            ec.source_file,
            ec.eob_key,
            ec.company_id,
            el.eob_line_id,
            ec.eob_check_id
        FROM billing.eob_line el
        JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
        LEFT JOIN core.patient p ON p.patient_id = el.patient_id
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.visit v ON v.patient_id = el.patient_id AND v.service_date = el.date_of_service
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        ORDER BY ec.eob_date, el.date_of_service
        """,
    )


def get_bank_deposits(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.bank_deposit
        ORDER BY bank_posting_date NULLS LAST, check_date_recognized NULLS LAST
        """,
    )


def get_tracked_eft_refs(conn: psycopg.Connection) -> dict[str, date | None]:
    """Map EFT/check refs → deposit posting date (tracker replacement)."""
    rows = client.fetchall(
        conn,
        """
        SELECT eft_1, eft_2, eft_last4, bank_posting_date, check_date_recognized
        FROM billing.bank_deposit
        """,
    )
    out: dict[str, date | None] = {}
    for r in rows:
        d = r.get("bank_posting_date") or r.get("check_date_recognized")
        for key in (r.get("eft_1"), r.get("eft_2"), r.get("eft_last4")):
            if key:
                out[str(key).strip()] = d
    return out


def count_patient_payments(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.patient_payment")
    return int(row["n"]) if row else 0


def count_eob_checks(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.eob_check")
    return int(row["n"]) if row else 0


def count_bank_deposits(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.bank_deposit")
    return int(row["n"]) if row else 0
