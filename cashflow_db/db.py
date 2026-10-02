"""PostgreSQL connection helpers and schema migration runner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL, SQL_DIR

MIGRATIONS = [
    "001_schemas.sql",
    "002_ref.sql",
    "003_etl_gov.sql",
    "004_core.sql",
    "005_billing.sql",
    "006_docs.sql",
    "007_ops.sql",
    "008_analytics.sql",
    "009_finance.sql",
    "010_views.sql",
    "011_seed_ref.sql",
    "012_case_centric.sql",
    "013_operational_spine.sql",
    "014_pipeline_control.sql",
    "015_monitoring.sql",
    "016_auth.sql",
    "017_eligibility_ops.sql",
    "018_transaction_tracker.sql",
    "019_recon_pending_reason.sql",
    "020_mail_dedupe.sql",
    "021_fk_indexes.sql",
    "022_acquire_sla.sql",
    "023_sot_validation.sql",
    "024_checks_deposits.sql",
    "025_payer_cpt_guide.sql",
    "026_facility_names.sql",
    "027_icd_audit.sql",
    "028_note_esign.sql",
    "029_clinic_clusters.sql",
    "030_submission_role.sql",
    "031_demographic_audit.sql",
    "032_waystar_claims.sql",
    "033_waystar_map_patient.sql",
    "034_waystar_ambiguous_emr.sql",
    "035_pt_city_charges.sql",
    "036_perf_indexes.sql",
    "037_elig_manual_overrides.sql",
    "038_collector_role.sql",
    "039_pr_queue_flags.sql",
    "040_pr_queue_sort_indexes.sql",
    "041_pr_queue_row.sql",
    "042_pr_second_submission.sql",
    "043_drop_role_key_check.sql",
    "044_ops_admin_role.sql",
    "045_elig_sheet_ledger.sql",
    "046_second_submission_role.sql",
    "047_pr_tfl_rules.sql",
    "048_pt_city_charge_ins.sql",
    "049_pr_queue_row_source.sql",
    "050_pr_queue_flag_note.sql",
    "051_emr_name.sql",
    "052_recon_check_breakdown.sql",
    "053_work_analytics.sql",
    "054_sub_admin_role.sql",
    "055_portal_activity.sql",
    "056_collection_lookups.sql",
    "057_analytics_viewer_role.sql",
    "058_list_perf_indexes.sql",
    "059_elig_keep_visit.sql",
    "060_collection_queue_member.sql",
    "061_user_away.sql",
    "062_eob_line_oa23_amount.sql",
    "063_medical_audit_role.sql",
    "064_collection_status_buckets.sql",
    "060_cash_event_match.sql",
    "065_chat_log.sql",
    "066_activity_desk_idle.sql",
    "067_ss_eligible_visit.sql",
    "068_desk_role.sql",
    "069_paid_patient_responsibility.sql",
    "070_desk_permission.sql",
    "071_collection_dead_bucket.sql",
    "072_billing_collect_visit.sql",
    "073_collection_exit_indexes.sql",
]


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(url or DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    conn.execute(sql)


def migrate(url: str | None = None) -> list[str]:
    applied: list[str] = []
    with connect(url) as conn:
        for name in MIGRATIONS:
            path = SQL_DIR / name
            if not path.exists():
                raise FileNotFoundError(path)
            run_sql_file(conn, path)
            applied.append(name)
    return applied


def start_etl_run(
    conn: psycopg.Connection,
    source_system: str,
    source_uri: str | None = None,
    notes: str | None = None,
) -> str:
    row = conn.execute(
        """
        INSERT INTO etl.etl_run (source_system, source_uri, status, notes)
        VALUES (%s, %s, 'running', %s)
        RETURNING etl_run_id
        """,
        (source_system, source_uri, notes),
    ).fetchone()
    return str(row["etl_run_id"])


def finish_etl_run(
    conn: psycopg.Connection,
    etl_run_id: str,
    *,
    status: str = "success",
    row_count: int | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE etl.etl_run
        SET finished_at = now(),
            status = %s,
            row_count = %s,
            notes = COALESCE(%s, notes)
        WHERE etl_run_id = %s::uuid
        """,
        (status, row_count, notes, etl_run_id),
    )
