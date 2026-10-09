"""Visit / schedule / clinical note repository contracts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from cashflow_db.repository import client

RECON_MIN_DOS = date(2026, 1, 1)
CAIRO_TZ = ZoneInfo("Africa/Cairo")
ELIGIBILITY_MIN_DOS_SQL = "wi.dos >= DATE '2026-01-01'"
ELIGIBILITY_MAX_DOS_SQL = (
    "wi.dos <= ((CURRENT_TIMESTAMP AT TIME ZONE 'Africa/Cairo')::date - 1)"
)


def recon_window(
    service_from: date | None = None,
    service_to: date | None = None,
) -> tuple[date, date]:
    """Inclusive recon DOS window: 2026-01-01 .. yesterday (Cairo) unless set."""
    today = datetime.now(CAIRO_TZ).date()
    start = service_from or RECON_MIN_DOS
    end = service_to or (today - timedelta(days=1))
    return start, end


def _pt_city_cpt_sql(
    *,
    emr_expr: str,
    dos_expr: str,
    visit_id_expr: str | None = None,
) -> str:
    if visit_id_expr:
        lines = f"""EXISTS (
            SELECT 1 FROM core.visit_service_line sl
            WHERE sl.visit_id = {visit_id_expr}
              AND sl.source_system = 'pt_city'
              AND COALESCE(btrim(sl.cpt_code), '') <> ''
        )"""
    else:
        lines = f"""EXISTS (
            SELECT 1 FROM core.visit vx
            JOIN core.patient px ON px.patient_id = vx.patient_id
            JOIN core.visit_service_line sl ON sl.visit_id = vx.visit_id
            WHERE btrim(px.webpt_patient_id) = btrim({emr_expr})
              AND vx.service_date = {dos_expr}
              AND sl.source_system = 'pt_city'
              AND COALESCE(btrim(sl.cpt_code), '') <> ''
        )"""
    charge = f"""EXISTS (
        SELECT 1 FROM analytics.pt_city_charge_ins ci
        WHERE btrim(ci.emr_id) = btrim({emr_expr})
          AND ci.date_of_service = {dos_expr}
    )"""
    return f"({lines} OR {charge})"


def _kpi_sql(*, emr_expr: str, dos_expr: str) -> str:
    return f"""EXISTS (
        SELECT 1 FROM analytics.snowflake_visit_kpi sf
        WHERE btrim(sf.emr_id) = btrim({emr_expr})
          AND sf.date_of_service = {dos_expr}
    )"""


def _note_sql(
    *,
    emr_expr: str,
    dos_expr: str,
    visit_id_expr: str | None = None,
) -> str:
    if visit_id_expr:
        return f"""EXISTS (
            SELECT 1 FROM core.clinical_note cn
            WHERE cn.visit_id = {visit_id_expr}
        )"""
    return f"""EXISTS (
        SELECT 1 FROM core.visit vx
        JOIN core.patient px ON px.patient_id = vx.patient_id
        JOIN core.clinical_note cn ON cn.visit_id = vx.visit_id
        WHERE btrim(px.webpt_patient_id) = btrim({emr_expr})
          AND vx.service_date = {dos_expr}
    )"""


def billed_sql(
    *,
    emr_expr: str,
    dos_expr: str,
    visit_id_expr: str | None = None,
) -> str:
    """Snowflake billed: KPI row or pt_city CPT / charge_ins."""
    cpt = _pt_city_cpt_sql(
        emr_expr=emr_expr, dos_expr=dos_expr, visit_id_expr=visit_id_expr
    )
    return f"({_kpi_sql(emr_expr=emr_expr, dos_expr=dos_expr)} OR {cpt})"


def keep_visit_sql(
    *,
    emr_expr: str,
    dos_expr: str,
    visit_id_expr: str | None = None,
) -> str:
    """Billed visit: pt_city CPT, or a Snowflake KPI row.

    KPI-only visits stay in the spine when the daily-note scrape is down.
    A missing note must not drop a visit the billing KPI already counted.
    """
    if visit_id_expr:
        cpt = _pt_city_cpt_sql(
            emr_expr=emr_expr, dos_expr=dos_expr, visit_id_expr=visit_id_expr
        )
        kpi = _kpi_sql(emr_expr=emr_expr, dos_expr=dos_expr)
        return f"({cpt} OR {kpi})"
    return f"""(
        EXISTS (
            SELECT 1 FROM analytics.elig_keep_visit kv
            WHERE kv.emr_id = {emr_expr}
              AND kv.dos = {dos_expr}
        )
    )"""


KEEP_CLINICAL_VISIT_SQL = keep_visit_sql(
    emr_expr="p.webpt_patient_id",
    dos_expr="v.service_date",
    visit_id_expr="v.visit_id",
)
KEEP_WORK_ITEM_SQL = keep_visit_sql(
    emr_expr="wi.emr_patient_id",
    dos_expr="wi.dos",
)


def get_clinical_visits(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = [KEEP_CLINICAL_VISIT_SQL]
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
    exclude_reconciliation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """WebPT billed lines grain for matching (replaces extracted CPT+notes CSV).

    ``exclude_reconciliation_run_id`` drops lines already on that recon run
    for the same patient, date of service, and CPT. The forecast still runs
    its own dedupe afterwards, so this only skips rows that dedupe would drop.
    """
    clauses = [KEEP_CLINICAL_VISIT_SQL]
    params: list[Any] = []
    if service_from:
        clauses.append("v.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("v.service_date <= %s")
        params.append(service_to)
    if exclude_reconciliation_run_id:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM billing.reconciliation_line rl
                WHERE rl.reconciliation_run_id = %s::uuid
                  AND rl.date_of_service = v.service_date
                  AND rl.webpt_patient_id IS NOT NULL
                  AND btrim(rl.webpt_patient_id) <> ''
                  AND btrim(rl.webpt_patient_id) = btrim(p.webpt_patient_id)
                  AND rl.cpt_code IS NOT NULL
                  AND sl.cpt_code IS NOT NULL
                  AND btrim(rl.cpt_code) = btrim(sl.cpt_code)
            )
            """
        )
        params.append(exclude_reconciliation_run_id)
    return client.fetchall(
        conn,
        f"""
        SELECT
            sl.service_line_id,
            sl.cpt_code,
            sl.modifiers AS modifier,
            sl.units,
            sl.source_system,
            NULL::numeric AS billed_amount,
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
        LEFT JOIN LATERAL (
            SELECT cn.external_daily_note_id, cn.note_file, cn.insurance_name_raw
            FROM core.clinical_note cn
            WHERE cn.visit_id = v.visit_id
            ORDER BY cn.note_date DESC NULLS LAST, cn.version_no DESC
            LIMIT 1
        ) cn ON true
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


def refresh_elig_keep_visit(conn: psycopg.Connection) -> int:
    """Rebuild billed-visit membership for the eligibility DOS window."""
    start, end = recon_window()
    client.execute(conn, "DELETE FROM analytics.elig_keep_visit")
    client.execute(
        conn,
        """
        INSERT INTO analytics.elig_keep_visit (emr_id, dos)
        SELECT emr_id, date_of_service
        FROM analytics.pt_city_charge_ins
        WHERE date_of_service >= %s
          AND date_of_service <= %s
          AND COALESCE(btrim(emr_id), '') <> ''
        UNION
        SELECT emr_id, date_of_service
        FROM analytics.snowflake_visit_kpi
        WHERE date_of_service >= %s
          AND date_of_service <= %s
          AND COALESCE(btrim(emr_id), '') <> ''
        """,
        (start, end, start, end),
    )
    row = client.fetchone(
        conn, "SELECT COUNT(*)::int AS n FROM analytics.elig_keep_visit"
    )
    client.execute(conn, "DELETE FROM analytics.elig_skip_visit")
    client.execute(
        conn,
        """
        INSERT INTO analytics.elig_skip_visit (emr_id, dos)
        SELECT p.webpt_patient_id, v.service_date
        FROM core.visit v
        JOIN core.patient p ON p.patient_id = v.patient_id
        WHERE v.status IN ('cancelled', 'no_show')
          AND COALESCE(btrim(p.webpt_patient_id), '') <> ''
          AND v.service_date >= %s
          AND v.service_date <= %s
        UNION
        SELECT sf.emr_id, sf.date_of_service
        FROM analytics.snowflake_visit_kpi sf
        WHERE sf.date_of_service >= %s
          AND sf.date_of_service <= %s
          AND COALESCE(btrim(sf.emr_id), '') <> ''
          AND regexp_replace(
                lower(btrim(COALESCE(sf.status, ''))),
                '[\\s/-]+',
                '_',
                'g'
              ) IN (
                'cancelled',
                'canceled',
                'no_show',
                'noshow',
                'cancelled_no_show',
                'canceled_no_show'
              )
        UNION
        SELECT p.webpt_patient_id, sa.service_date
        FROM core.schedule_appointment sa
        JOIN core.patient p ON p.patient_id = sa.patient_id
        WHERE sa.status IN ('cancelled', 'no_show')
          AND COALESCE(btrim(p.webpt_patient_id), '') <> ''
          AND sa.service_date >= %s
          AND sa.service_date <= %s
          AND (
              sa.is_selected_clinical
              OR NOT EXISTS (
                  SELECT 1 FROM core.schedule_appointment ok
                  WHERE ok.patient_id = sa.patient_id
                    AND ok.service_date = sa.service_date
                    AND ok.status IN (
                        'completed', 'unchecked_out', 'confirmed', 'scheduled'
                    )
              )
          )
        """,
        (start, end, start, end, start, end),
    )
    return int(row["n"]) if row else 0


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
