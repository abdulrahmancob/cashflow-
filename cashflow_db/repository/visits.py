"""Visit / schedule / clinical note repository contracts."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from cashflow_db.repository import client


def get_clinical_visits(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if service_from:
        clauses.append("v.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("v.service_date <= %s")
        params.append(service_to)
    lim = f"LIMIT {int(limit)}" if limit else ""
    return client.fetchall(
        conn,
        f"""
        SELECT
            v.visit_id,
            v.service_date,
            v.status,
            v.visit_type,
            v.insurance_name_raw,
            p.webpt_patient_id,
            ph.patient_name,
            ph.dob,
            pc.webpt_case_id AS case_id,
            pc.case_label,
            f.webpt_facility_id AS facility_id,
            f.name AS facility_name
        FROM core.visit v
        JOIN core.patient p ON p.patient_id = v.patient_id
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        WHERE {' AND '.join(clauses)}
        ORDER BY v.service_date, p.webpt_patient_id
        {lim}
        """,
        params,
    )


def get_service_lines_for_reconcile(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
) -> list[dict[str, Any]]:
    """WebPT billed lines grain for matching (replaces extracted CPT+notes CSV)."""
    clauses = ["1=1"]
    params: list[Any] = []
    if service_from:
        clauses.append("v.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("v.service_date <= %s")
        params.append(service_to)
    return client.fetchall(
        conn,
        f"""
        SELECT
            sl.service_line_id,
            sl.cpt_code,
            sl.modifiers AS modifier,
            sl.units,
            sl.billed_amount,
            v.visit_id,
            v.service_date AS date_of_service,
            p.webpt_patient_id,
            ph.patient_name,
            ph.dob,
            pc.webpt_case_id AS case_id,
            cov.raw_insurance_name AS ins_name,
            cov.copay AS expected_copay,
            cov.deductible AS expected_deductible,
            f.name AS facility_name,
            f.webpt_facility_id AS facility_id,
            cn.external_daily_note_id AS daily_note_id,
            cn.note_file,
            cn.insurance_name_raw AS insurance_note
        FROM core.visit_service_line sl
        JOIN core.visit v ON v.visit_id = sl.visit_id
        JOIN core.patient p ON p.patient_id = v.patient_id
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
        LEFT JOIN core.patient_coverage cov ON cov.coverage_id = v.coverage_id
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        LEFT JOIN core.clinical_note cn ON cn.visit_id = v.visit_id
        WHERE {' AND '.join(clauses)}
        ORDER BY v.service_date, p.webpt_patient_id, sl.cpt_code
        """,
        params,
    )


def get_schedule_appointments(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if service_from:
        clauses.append("sa.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("sa.service_date <= %s")
        params.append(service_to)
    return client.fetchall(
        conn,
        f"""
        SELECT sa.*, pc.webpt_case_id AS case_id, p.webpt_patient_id
        FROM core.schedule_appointment sa
        JOIN core.patient_case pc ON pc.case_pk = sa.case_pk
        JOIN core.patient p ON p.patient_id = sa.patient_id
        WHERE {' AND '.join(clauses)}
        ORDER BY sa.appointment_at
        """,
        params,
    )


def count_clinical_notes(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM core.clinical_note")
    return int(row["n"]) if row else 0


def count_service_lines(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM core.visit_service_line")
    return int(row["n"]) if row else 0


def count_schedule_appointments(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM core.schedule_appointment")
    return int(row["n"]) if row else 0


def get_patients_enriched(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT p.webpt_patient_id, ph.patient_name, ph.dob,
               pc.webpt_case_id AS case_id, f.name AS facility_name,
               cov.raw_insurance_name AS ins_name, cov.copay, cov.deductible,
               a.visits_authorized AS auth_ins_visits
        FROM core.patient p
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.patient_case pc ON pc.patient_id = p.patient_id
        LEFT JOIN ref.facility f ON f.facility_id = pc.facility_id
        LEFT JOIN core.patient_coverage cov ON cov.case_pk = pc.case_pk
        LEFT JOIN LATERAL (
            SELECT visits_authorized FROM core.authorization auth
            WHERE auth.case_pk = pc.case_pk
            ORDER BY auth.auth_id DESC LIMIT 1
        ) a ON true
        WHERE p.webpt_patient_id IS NOT NULL
        """,
    )
