"""Eligibility work-queue repository (ops schema)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg

from cashflow_db.repository import client
from cashflow_db.repository.collection import (
    collection_status_bucket,
    fold_label,
    still_pending_visit_sql,
)
from cashflow_db.repository.visits import (
    ELIGIBILITY_MAX_DOS_SQL,
    ELIGIBILITY_MIN_DOS_SQL,
    KEEP_WORK_ITEM_SQL,
)
from cashflow_db.repository.pr_tfl import (
    TFL_DAYS_LEFT_EXPR,
    TFL_JOIN_SQL,
    TFL_SELECT_SQL,
    WORKLOAD_TFL_FILTERS,
    fold_insurance_name,
)
from cashflow_db.repository.pr_workload_lookups import (
    COLLECTED,
    COLLECTED_PR2_SQL,
    MEDICARE_MEDICAID_SQL,
    NO_SECONDARY_PAYER,
    NO_SECONDARY_PAYER_SQL,
    SECOND_INSURANCE_NAMES,
    WORKLOAD_IN_QUEUE_SQL,
    WORKLOAD_MOVED_SQL,
    WORKLOAD_PROVIDERS,
    WORKLOAD_STATUSES,
)
from cashflow_db.util import parse_date

SYSTEM_SUBMITTER = "System"

DIRECT_FIELDS = frozenset({"eligibility_status", "reference_number", "notes"})
MONEY_FIELDS = frozenset(
    {
        "paid_amount",
        "client_payment",
        "insurance_payment",
        "updated_payment",
        "coinsurance_payment",
        "reduction",
        "charged_amount",
        "adjusted",
        "insurance_check_amount",
        "secondary_check_amount",
        "updated_check_amount",
        "fourth_check_amount",
    }
)
DATE_OVERRIDE_FIELDS = frozenset(
    {
        "dos",
        "dob",
        "check_date",
        "tracker_date",
        "insurance_check_date",
        "secondary_check_date",
        "updated_check_date",
        "fourth_check_date",
        "posting_date_1",
        "posting_date_2",
        "posting_date_3",
        "corrected_date",
        "work_date",
    }
)
SHEET_TEXT_FIELDS = frozenset(
    {
        "details",
        "collector_1",
        "collector_2",
        "collector_3",
        "visit_status_sheet",
        "corrected",
        "sf_visit_id",
        "insurance_id",
        "secondary_insurance",
        "secondary_insurance_id",
        "insurance_check_number",
        "secondary_check_number",
        "updated_check_number",
        "fourth_check_number",
        "work_status",
        "denial_reason",
        "root_cause",
        "actions_taken",
        "collection_status",
    }
)
OVERRIDE_FIELDS = frozenset(
    {
        "patient_name",
        "emr_patient_id",
        "dos",
        "insurance_name",
        "facility_name",
        "paid_amount",
        "check_number",
        "check_date",
        "tracker_date",
        "source_visit_status",
    }
    | MONEY_FIELDS
    | DATE_OVERRIDE_FIELDS
    | SHEET_TEXT_FIELDS
)
EDITABLE_FIELDS = DIRECT_FIELDS | OVERRIDE_FIELDS
LOCK_TTL_MINUTES = 5
OVERDUE_DAYS = 7
LEDGER_SOURCES = frozenset({"manual", "recon", "snowflake"})
PAID_ALIAS_FIELDS = frozenset({"paid_amount", "insurance_payment"})
CHECK_ALIAS_FIELDS = frozenset({"check_number", "insurance_check_number"})
CHECK_DATE_ALIAS_FIELDS = frozenset({"check_date", "insurance_check_date"})

SHEET_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("emr_patient_id", "EMR ID"),
    ("patient_name", "Patient Name"),
    ("insurance_name", "Insurance Name"),
    ("dos", "DOS"),
    ("insurance_payment", "Insurance Payment"),
    ("source_visit_status", "Status"),
    ("updated_payment", "Updated Payment"),
    ("coinsurance_payment", "Co-Insurance Payment"),
    ("rtm", "RTM"),
    ("reduction", "Reduction"),
    ("details", "Details"),
    ("total_amount", "Total  Amount"),
    ("insurance_check_number", "Insurance Payemnt Check#"),
    ("insurance_check_date", "Insurance Payemnt Check Date"),
    ("insurance_check_amount", "Insurance Payemnt Check Amount"),
    ("secondary_check_number", "Secondary Check#"),
    ("secondary_check_date", "Secondary Check Date"),
    ("secondary_check_amount", "Secondary Check Amount"),
    ("collector_1", "Collector 1"),
    ("posting_date_1", "1st Posting Date"),
    ("collector_2", "Collector 2"),
    ("posting_date_2", "2nd Posting Date"),
    ("collector_3", "Collector 3"),
    ("posting_date_3", "Third Posting Date"),
    ("facility_name", "Facility"),
    ("tracker_date", "Tracker Date"),
    ("added_amount", "Added"),
    ("deducted_amount", "Deducted"),
    ("notes", "Notes"),
    ("assigned_to_code", "Collector"),
)

COLLECTION_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("emr_patient_id", "EMR ID"),
    ("account_number", "Account #"),
    ("patient_name", "Patient Name"),
    ("insurance_name", "Insurance Name"),
    ("facility_name", "Facility"),
    ("dos", "DOS"),
    ("client_payment", "Client Payment"),
    ("insurance_payment", "Insurance Payment"),
    ("source_visit_status", "Status"),
    ("work_date", "Work Date"),
    ("assigned_to_code", "Assignee"),
    ("denial_reason", "Denial Reason"),
    ("root_cause", "RootCause"),
    ("actions_taken", "Actions taken"),
    ("collection_status", "Collection Status"),
)

WORK_DATE_TRIGGER_FIELDS = frozenset(
    {"denial_reason", "root_cause", "actions_taken", "collection_status"}
)

EFFECTIVE_VISIT_STATUS_SQL = """COALESCE(
    NULLIF(btrim(wi.manual_overrides->>'source_visit_status'), ''),
    wi.source_visit_status
)"""

PAID_OR_DEDUCT_SQL = f"""(
    lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) IN ('paid', 'deduct')
    OR EXISTS (
        SELECT 1 FROM analytics.snowflake_visit_kpi sf
        WHERE sf.emr_id = wi.emr_patient_id
          AND sf.date_of_service = wi.dos
          AND lower(btrim(COALESCE(sf.status, ''))) IN ('paid', 'deduct')
    )
    OR EXISTS (
        SELECT 1 FROM billing.reconciliation_visit_agg rv
        WHERE rv.reconciliation_run_id = (
            SELECT reconciliation_run_id
            FROM billing.reconciliation_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
        )
          AND rv.webpt_patient_id = wi.emr_patient_id
          AND rv.date_of_service = wi.dos
          AND lower(btrim(COALESCE(rv.visit_status, ''))) IN ('paid', 'deduct')
    )
)"""

WAYSTAR_ZERO_REMIT_SQL = """(
    EXISTS (
        SELECT 1
        FROM billing.waystar_webpt_map m
        JOIN billing.waystar_claim c
          ON c.claim_key = m.waystar_claim_key
         AND c.from_date = wi.dos
        WHERE m.webpt_patient_id = wi.emr_patient_id
          AND COALESCE(c.total_remit_amount, 0) = 0
    )
    AND NOT EXISTS (
        SELECT 1
        FROM billing.waystar_webpt_map m
        JOIN billing.waystar_claim c
          ON c.claim_key = m.waystar_claim_key
         AND c.from_date = wi.dos
        WHERE m.webpt_patient_id = wi.emr_patient_id
          AND COALESCE(c.total_remit_amount, 0) <> 0
    )
)"""

WAYSTAR_HAS_DETAILS_SQL = """EXISTS (
    SELECT 1
    FROM billing.waystar_webpt_map m
    JOIN billing.eob_line el
      ON el.revflow_patient_id = m.waystar_claim_key
     AND el.date_of_service = wi.dos
    WHERE m.webpt_patient_id = wi.emr_patient_id
      AND NULLIF(btrim(el.carcs), '') IS NOT NULL
)"""

# A real EFT/check number. ZEROPAY notices and non-numeric tokens are not checks.
# Percents are doubled because this fragment is executed through psycopg.
_WAYSTAR_REAL_CHECK_SQL = """EXISTS (
    SELECT 1
    FROM unnest(COALESCE(c.remit_numbers, ARRAY[]::text[])) AS num
    WHERE NULLIF(btrim(num), '') IS NOT NULL
      AND upper(btrim(num)) NOT LIKE 'ZEROPAY%%'
      AND btrim(num) ~ '[0-9]'
)"""

# Mapped Waystar claim on the same EMR + DOS with money and a real check.
WAYSTAR_PAID_CHECK_SQL = f"""EXISTS (
    SELECT 1
    FROM billing.waystar_webpt_map m
    JOIN billing.waystar_claim c
      ON c.claim_key = m.waystar_claim_key
     AND c.from_date = wi.dos
    WHERE m.webpt_patient_id = wi.emr_patient_id
      AND COALESCE(c.total_remit_amount, 0) > 0
      AND {_WAYSTAR_REAL_CHECK_SQL}
)"""

WAYSTAR_COLLECTION_EXIT_REASON = "Exited collection after Waystar payment and check"

WAYSTAR_PAST_SLA_SQL = """EXISTS (
    SELECT 1
    FROM analytics.forecast_prediction fp
    WHERE fp.forecast_run_id = (
        SELECT forecast_run_id
        FROM analytics.forecast_run
        WHERE status = 'success'
        ORDER BY created_at DESC
        LIMIT 1
    )
      AND fp.expected_pay_date + 3 < CURRENT_DATE
      AND (
        (
          fp.webpt_patient_id = wi.emr_patient_id
          AND fp.date_of_service = wi.dos
        )
        OR (
          fp.webpt_patient_id IS NULL
          AND NULLIF(BTRIM(fp.payload->>'webpt_patient_id'), '') = wi.emr_patient_id
          AND COALESCE(
                fp.date_of_service,
                CASE
                    WHEN fp.payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                        THEN substring(fp.payload->>'date_of_service' from 1 for 10)::date
                    ELSE NULL
                END
              ) = wi.dos
        )
      )
)"""

DENIED_VISIT_SQL = f"""(
    (
        lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = 'denied'
        OR (
            {WAYSTAR_ZERO_REMIT_SQL}
            AND {WAYSTAR_HAS_DETAILS_SQL}
        )
    )
    AND NOT {PAID_OR_DEDUCT_SQL}
)"""

COLLECTION_VISIT_SQL = f"""(
    lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = 'collection'
    OR (
        lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) NOT IN ('paid', 'deduct')
        AND EXISTS (
            SELECT 1 FROM analytics.snowflake_visit_kpi sf
            WHERE sf.emr_id = wi.emr_patient_id
              AND sf.date_of_service = wi.dos
              AND lower(btrim(COALESCE(sf.status, ''))) = 'collection'
        )
    )
)"""

STILL_PENDING_VISIT_SQL = still_pending_visit_sql(
    emr_expr="wi.emr_patient_id",
    dos_expr="wi.dos",
    status_expr=EFFECTIVE_VISIT_STATUS_SQL,
)

ACCOUNT_NUMBER_SQL = """(
    SELECT CASE
        WHEN left(upper(NULLIF(btrim(p.revflow_patient_id), '')), 3) = 'PV4'
            THEN btrim(p.revflow_patient_id)
        WHEN regexp_replace(COALESCE(p.revflow_patient_id, ''), '[^0-9]', '', 'g') <> ''
            THEN 'PV4' || COALESCE(
                NULLIF(
                    ltrim(
                        regexp_replace(COALESCE(p.revflow_patient_id, ''), '[^0-9]', '', 'g'),
                        '0'
                    ),
                    ''
                ),
                regexp_replace(COALESCE(p.revflow_patient_id, ''), '[^0-9]', '', 'g')
            )
        ELSE NULLIF(btrim(p.webpt_patient_id), '')
    END
    FROM core.patient p
    WHERE p.webpt_patient_id = wi.emr_patient_id
    LIMIT 1
)"""

_ACCOUNT_SEARCH_SQL = """EXISTS (
        SELECT 1 FROM core.patient p
        WHERE p.webpt_patient_id = wi.emr_patient_id
          AND (
            COALESCE(p.revflow_patient_id, '') ILIKE %s
            OR regexp_replace(COALESCE(p.revflow_patient_id, ''), '[^0-9]', '', 'g') LIKE %s
          )
    )"""

COLLECTION_QUEUE_MEMBER_SQL = """EXISTS (
    SELECT 1 FROM analytics.collection_queue_member m
    WHERE m.work_item_id = wi.work_item_id
      AND m.bucket = %s
)"""

_COLLECTION_STATUS_TEXT_SQL = """COALESCE(
    NULLIF(btrim(wi.manual_overrides->>'collection_status'), ''),
    (
        SELECT COALESCE(
            NULLIF(btrim(sf.payload->>'COLLECTION_STATUS'), ''),
            NULLIF(btrim(sf.payload->>'collection_status'), '')
        )
        FROM analytics.snowflake_visit_kpi sf
        WHERE sf.emr_id = wi.emr_patient_id
          AND sf.date_of_service = wi.dos
        LIMIT 1
    ),
    NULLIF(btrim(wi.context->>'collection_status'), '')
)"""

EFFECTIVE_COLLECTION_FOLD_SQL = f"""regexp_replace(
    lower(btrim(COALESCE({_COLLECTION_STATUS_TEXT_SQL}, ''))),
    '[^a-z0-9]', '', 'g'
)"""

ROUTED_COLLECTION_SQL = f"""{EFFECTIVE_COLLECTION_FOLD_SQL} IN (
    'arbitration', 'actiontaken', 'pending', 'submittedwithoutauth'
)"""

ACTION_WORK_DATE_SQL = """COALESCE(
    CASE
        WHEN NULLIF(btrim(wi.manual_overrides->>'work_date'), '') ~ '^\\d{4}-\\d{2}-\\d{2}'
            THEN substring(wi.manual_overrides->>'work_date' from 1 for 10)::date
        ELSE NULL
    END,
    CASE
        WHEN NULLIF(btrim(wi.context->>'work_date'), '') ~ '^\\d{4}-\\d{2}-\\d{2}'
            THEN substring(wi.context->>'work_date' from 1 for 10)::date
        ELSE NULL
    END
)"""

ACTION_IS_FOLLOW_UP_SQL = f"""COALESCE(({ACTION_WORK_DATE_SQL}) <= CURRENT_DATE - 30, FALSE)"""

_SKIPPED_STATUS_SQL = """regexp_replace(lower(btrim(COALESCE({col}, ''))), '[\\s/-]+', '_', 'g')
        IN ('cancelled', 'canceled', 'no_show', 'noshow', 'cancelled_no_show', 'canceled_no_show')"""

SKIPPED_VISIT_SQL = f"""(
    {_SKIPPED_STATUS_SQL.format(col="wi.source_visit_status")}
    OR EXISTS (
        SELECT 1 FROM analytics.elig_skip_visit sk
        WHERE sk.emr_id = wi.emr_patient_id
          AND sk.dos = wi.dos
    )
)"""

# Eligibility visits past the insurance SLA. A Waystar claim is not required.
# Denied (including zero-remit with CARCs) and collection visits stay off this tab.
OVERDUE_PENDING_SQL = f"""(
    {WAYSTAR_PAST_SLA_SQL}
    AND NOT {SKIPPED_VISIT_SQL}
    AND NOT {DENIED_VISIT_SQL}
    AND NOT {COLLECTION_VISIT_SQL}
    AND NOT {PAID_OR_DEDUCT_SQL}
    AND NOT ({ROUTED_COLLECTION_SQL})
)"""

# Waystar denied checks, aggregated once per Second Submission query.
# Same rule as DENIED_VISIT_SQL: a visit is zero-remit only when every claim
# remits 0, and it has CARC detail.
_SS_WAYSTAR_ZERO_SQL = """
SELECT m.webpt_patient_id AS emr, c.from_date AS dos
FROM billing.waystar_webpt_map m
JOIN billing.waystar_claim c
  ON c.claim_key = m.waystar_claim_key
WHERE NULLIF(btrim(m.webpt_patient_id), '') IS NOT NULL
  AND c.from_date IS NOT NULL
GROUP BY 1, 2
HAVING bool_or(COALESCE(c.total_remit_amount, 0) = 0)
   AND NOT bool_or(COALESCE(c.total_remit_amount, 0) <> 0)
"""

_SS_WAYSTAR_DETAILS_SQL = """
SELECT DISTINCT m.webpt_patient_id AS emr, el.date_of_service AS dos
FROM billing.waystar_webpt_map m
JOIN billing.eob_line el
  ON el.revflow_patient_id = m.waystar_claim_key
WHERE NULLIF(btrim(m.webpt_patient_id), '') IS NOT NULL
  AND el.date_of_service IS NOT NULL
  AND NULLIF(btrim(el.carcs), '') IS NOT NULL
"""

_SS_DENIED_SQL = f"""(
    (
        lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = 'denied'
        OR (
            EXISTS (
                SELECT 1 FROM ss_waystar_zero z
                WHERE z.emr = wi.emr_patient_id AND z.dos = wi.dos
            )
            AND EXISTS (
                SELECT 1 FROM ss_waystar_details d
                WHERE d.emr = wi.emr_patient_id AND d.dos = wi.dos
            )
        )
    )
    AND NOT {PAID_OR_DEDUCT_SQL}
)"""

# Visits that currently appear on the Eligibility sheet.
# Stored in ops.ss_eligible_visit by refresh_ss_eligible_visits().
ELIGIBLE_VISIT_SQL = f"""
SELECT wi.emr_patient_id, wi.dos
FROM ops.eligibility_work_item wi
WHERE {ELIGIBILITY_MIN_DOS_SQL}
  AND {ELIGIBILITY_MAX_DOS_SQL}
  AND {KEEP_WORK_ITEM_SQL}
  AND NOT {SKIPPED_VISIT_SQL}
  AND NOT {_SS_DENIED_SQL}
  AND NOT {COLLECTION_VISIT_SQL}
"""

# Second Submission shows a row only when that visit is on the Eligibility sheet,
# unless someone added it with Add visit (source = 'manual').
SS_ON_ELIGIBILITY_SQL = """
(
    base.source = 'manual'
    OR EXISTS (
        SELECT 1
        FROM ops.ss_eligible_visit vis
        WHERE vis.emr_patient_id = base.emr_patient_id
          AND vis.dos = base.dos
          AND NULLIF(btrim(base.emr_patient_id), '') IS NOT NULL
    )
    OR (
        NULLIF(btrim(base.emr_patient_id), '') IS NULL
        AND EXISTS (
            SELECT 1
            FROM core.patient p
            JOIN ops.ss_eligible_visit vis
              ON vis.emr_patient_id = p.webpt_patient_id
             AND vis.dos = base.dos
            WHERE p.revflow_patient_id = base.revflow_patient_id
              AND NULLIF(btrim(p.webpt_patient_id), '') IS NOT NULL
        )
    )
)
"""


def refresh_ss_eligible_visits(conn: psycopg.Connection) -> int:
    """Rebuild stored Eligibility visits for Second Submission, off the page load."""
    client.execute(conn, "DROP TABLE IF EXISTS ss_waystar_zero")
    client.execute(conn, "DROP TABLE IF EXISTS ss_waystar_details")
    client.execute(
        conn,
        "CREATE TEMP TABLE ss_waystar_zero ON COMMIT DROP AS " + _SS_WAYSTAR_ZERO_SQL,
    )
    client.execute(conn, "CREATE INDEX ON ss_waystar_zero (emr, dos)")
    client.execute(conn, "ANALYZE ss_waystar_zero")
    client.execute(
        conn,
        "CREATE TEMP TABLE ss_waystar_details ON COMMIT DROP AS " + _SS_WAYSTAR_DETAILS_SQL,
    )
    client.execute(conn, "CREATE INDEX ON ss_waystar_details (emr, dos)")
    client.execute(conn, "ANALYZE ss_waystar_details")
    client.execute(conn, "DELETE FROM ops.ss_eligible_visit")
    client.execute(
        conn,
        f"""
        INSERT INTO ops.ss_eligible_visit (emr_patient_id, dos)
        SELECT DISTINCT btrim(emr_patient_id), dos
        FROM (
            {ELIGIBLE_VISIT_SQL}
        ) vis
        WHERE NULLIF(btrim(emr_patient_id), '') IS NOT NULL
          AND dos IS NOT NULL
        """,
    )
    row = client.fetchone(conn, "SELECT count(*)::int AS n FROM ops.ss_eligible_visit")
    client.execute(conn, "ANALYZE ops.ss_eligible_visit")
    return int(row["n"]) if row else 0


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def _as_date_text(value: Any) -> str | None:
    parsed = parse_date(value) if value not in (None, "") else None
    if parsed is not None:
        return parsed.isoformat()
    text = _as_text(value)
    return text[:10] if text and len(text) >= 10 else text


def _payload_map(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {str(k).strip().upper(): v for k, v in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(k).strip().upper(): v for k, v in parsed.items()}
    return {}


def _payload_get(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key.upper())
        if value is not None and str(value).strip() != "":
            return value
    return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        num = _as_number(value)
        if num is not None:
            return num
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _as_text(value)
        if text:
            return text
    return None


def _money_sum(*values: Any) -> float:
    total = 0.0
    for value in values:
        num = _as_number(value)
        if num is not None:
            total += num
    return round(total, 2)


def ledger_column_name(column_name: str) -> str:
    if column_name in PAID_ALIAS_FIELDS:
        return "insurance_payment"
    if column_name in CHECK_ALIAS_FIELDS:
        return "insurance_check_number"
    if column_name in CHECK_DATE_ALIAS_FIELDS:
        return "insurance_check_date"
    return column_name


def _export_columns(queue: str | None = None) -> tuple[tuple[str, str], ...]:
    return COLLECTION_EXPORT_COLUMNS if _is_collection_queue(queue) else SHEET_EXPORT_COLUMNS


def _is_collection_queue(queue: str | None) -> bool:
    return str(queue or "sheet").strip().lower() == "collection"


def _is_pr3_queue(queue: str | None) -> bool:
    return str(queue or "").strip().lower() in {"pr3", "patient_responsibility"}


def _collection_bucket(bucket: str | None) -> str:
    raw = str(bucket or "denied").strip().lower()
    if raw in {
        "overdue",
        "collection",
        "arbitration",
        "action",
        "follow_up",
        "at_risk",
        "paid_patient_responsibility",
    }:
        return raw
    return "denied"


def sheet_export_headers(queue: str | None = None) -> list[str]:
    return [label for _key, label in _export_columns(queue)]


def sheet_export_row(row: dict[str, Any], queue: str | None = None) -> list[Any]:
    out: list[Any] = []
    for key, _label in _export_columns(queue):
        value = row.get(key)
        if key == "collector_1" and not _as_text(value):
            value = row.get("assigned_to_code") or row.get("assigned_to_name")
        if key == "assigned_to_code" and not _as_text(value):
            value = row.get("assigned_to_name")
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        out.append(value)
    return out


def _emr_dos_keys(rows: list[dict[str, Any]]) -> tuple[list[str], list[date]]:
    emrs: list[str] = []
    doses: list[date] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        parsed = row.get("dos")
        if isinstance(parsed, datetime):
            parsed = parsed.date()
        elif not isinstance(parsed, date):
            parsed = parse_date(str(parsed or ""))
        if not emr or parsed is None:
            continue
        key = (emr, parsed.isoformat())
        if key in seen:
            continue
        seen.add(key)
        emrs.append(emr)
        doses.append(parsed)
    return emrs, doses


_FLOAT_DOT_ZERO = re.compile(r"\.0+$")


def compact_check_key(value: Any) -> str | None:
    """Normalize a check/EFT ref the same way overlay_tracker_dates matches SQL."""
    text = _as_text(value)
    if not text:
        return None
    text = _FLOAT_DOT_ZERO.sub("", text)
    compact = _NON_ALNUM.sub("", text.upper())
    if not compact:
        return None
    if compact.isdigit():
        return compact.lstrip("0") or "0"
    return compact


_RECON_OVERLAY_KEYS = (
    "recon_total_paid",
    "recon_check_number",
    "recon_check_date",
    "recon_check_amount",
    "recon_visit_status",
    "recon_pending_reason",
    "recon_secondary_check_number",
    "recon_secondary_check_date",
    "recon_secondary_check_amount",
    "recon_third_check_number",
    "recon_third_check_date",
    "recon_third_check_amount",
    "recon_fourth_check_number",
    "recon_fourth_check_date",
    "recon_fourth_check_amount",
    "recon_missing_tracker_checks",
    "recon_pr1_amount",
    "recon_denial_reason",
    "recon_oa23_amount",
    "recon_oa23_check",
    "recon_rtm_amount",
)

_SF_OVERLAY_KEYS = (
    "sf_status",
    "sf_client_payment",
    "sf_insurance_payment",
    "sf_updated_payment",
    "sf_coinsurance_payment",
    "sf_reduction",
    "sf_adjusted",
    "sf_charged_amount",
    "sf_details",
    "sf_insurance_check_number",
    "sf_insurance_check_date",
    "sf_insurance_check_amount",
    "sf_secondary_check_number",
    "sf_secondary_check_date",
    "sf_secondary_check_amount",
    "sf_collector_1",
    "sf_posting_date_1",
    "sf_collector_2",
    "sf_posting_date_2",
    "sf_collector_3",
    "sf_posting_date_3",
    "sf_visit_status_sheet",
    "sf_corrected",
    "sf_corrected_date",
    "sf_visit_id",
    "sf_insurance_id",
    "sf_secondary_insurance",
    "sf_secondary_insurance_id",
    "sf_updated_check_number",
    "sf_updated_check_date",
    "sf_updated_check_amount",
    "sf_fourth_check_number",
    "sf_fourth_check_date",
    "sf_fourth_check_amount",
    "sf_work_status",
    "sf_work_date",
    "sf_denial_reason",
    "sf_root_cause",
    "sf_actions_taken",
    "sf_collection_status",
)
_SF_PROTECTED_KEYS = (
    "sf_insurance_payment",
    "sf_insurance_check_number",
    "sf_insurance_check_date",
    "sf_insurance_check_amount",
)
SF_CLOSED_STATUSES = frozenset({"denied", "collection", "paid", "deduct", "partial"})
SF_PAID_TOLERANCE = 0.01
SF_OVERLAY_TAKE_ALL = "take_all"
SF_OVERLAY_EXTRAS_ONLY = "extras_only"
SF_OVERLAY_SKIP = "skip"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def _dos_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else (text or None)


def _pick_recon_row(
    candidates: list[dict[str, Any]],
    facility_name: str | None,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    fac = (facility_name or "").strip().lower()
    if fac:
        for row in candidates:
            if str(row.get("facility_name") or "").strip().lower() == fac:
                return row
    return candidates[0]


def overlay_live_recon(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Copy live total_paid / check fields from the latest recon visit agg."""
    if not rows:
        return
    from cashflow_db.repository.reconciliation import latest_reconciliation_run_id

    run_id = latest_reconciliation_run_id(conn)
    if not run_id:
        return
    emrs, doses = _emr_dos_keys(rows)
    if not emrs:
        return
    recon_rows = client.fetchall(
        conn,
        """
        SELECT
            rv.webpt_patient_id,
            rv.date_of_service,
            rv.facility_name,
            rv.total_paid,
            rv.primary_check_number,
            rv.primary_check_date,
            rv.primary_check_amount,
            rv.visit_status,
            rv.pending_reason,
            rv.secondary_check_number,
            rv.secondary_check_date,
            rv.secondary_check_amount,
            rv.third_check_number,
            rv.third_check_date,
            rv.third_check_amount,
            rv.fourth_check_number,
            rv.fourth_check_date,
            rv.fourth_check_amount,
            rv.missing_tracker_checks
        FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
        JOIN billing.reconciliation_visit_agg rv
          ON rv.webpt_patient_id = k.emr
         AND rv.date_of_service = k.dos
        WHERE rv.reconciliation_run_id = %s::uuid
        """,
        (emrs, doses, run_id),
    )
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in recon_rows:
        emr = str(rec.get("webpt_patient_id") or "").strip()
        dos_key = _dos_key(rec.get("date_of_service"))
        if not emr or not dos_key:
            continue
        by_key.setdefault((emr, dos_key), []).append(rec)
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        if not emr or not dos_key:
            continue
        picked = _pick_recon_row(by_key.get((emr, dos_key), []), row.get("facility_name"))
        if not picked:
            continue
        row["recon_total_paid"] = picked.get("total_paid")
        row["recon_check_number"] = picked.get("primary_check_number")
        row["recon_check_date"] = picked.get("primary_check_date")
        row["recon_check_amount"] = picked.get("primary_check_amount")
        row["recon_visit_status"] = picked.get("visit_status")
        row["recon_pending_reason"] = picked.get("pending_reason")
        row["recon_secondary_check_number"] = picked.get("secondary_check_number")
        row["recon_secondary_check_date"] = picked.get("secondary_check_date")
        row["recon_secondary_check_amount"] = picked.get("secondary_check_amount")
        row["recon_third_check_number"] = picked.get("third_check_number")
        row["recon_third_check_date"] = picked.get("third_check_date")
        row["recon_third_check_amount"] = picked.get("third_check_amount")
        row["recon_fourth_check_number"] = picked.get("fourth_check_number")
        row["recon_fourth_check_date"] = picked.get("fourth_check_date")
        row["recon_fourth_check_amount"] = picked.get("fourth_check_amount")
        row["recon_missing_tracker_checks"] = picked.get("missing_tracker_checks")


def overlay_live_sf(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Copy Snowflake billing-sheet fields for the page's EMR+DOS keys."""
    if not rows:
        return
    emrs, doses = _emr_dos_keys(rows)
    if not emrs:
        return
    sf_rows = client.fetchall(
        conn,
        """
        SELECT
            sf.emr_id,
            sf.date_of_service,
            sf.status,
            sf.client_payment,
            sf.insurance_payment,
            sf.co_insurance_payment,
            sf.reductions,
            sf.adjusted,
            sf.charged_amount,
            sf.primary_check_number,
            sf.primary_check_date,
            sf.primary_check_amount,
            sf.secondary_check_number,
            sf.secondary_check_date,
            sf.secondary_check_amount,
            sf.sf_visit_id,
            sf.insurance,
            sf.payload
        FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
        JOIN analytics.snowflake_visit_kpi sf
          ON sf.emr_id = k.emr
         AND sf.date_of_service = k.dos
        """,
        (emrs, doses),
    )
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in sf_rows:
        emr = str(rec.get("emr_id") or "").strip()
        dos_key = _dos_key(rec.get("date_of_service"))
        if not emr or not dos_key:
            continue
        by_key[(emr, dos_key)] = rec
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        if not emr or not dos_key:
            continue
        picked = by_key.get((emr, dos_key))
        if not picked:
            continue
        payload = _payload_map(picked.get("payload"))
        row["sf_status"] = _first_text(
            picked.get("status"), _payload_get(payload, "STATUS")
        )
        row["sf_client_payment"] = _first_number(
            picked.get("client_payment"), _payload_get(payload, "CLIENT_PAYMENT")
        )
        row["sf_insurance_payment"] = _first_number(
            picked.get("insurance_payment"), _payload_get(payload, "INSURANCE_PAYMENT")
        )
        row["sf_updated_payment"] = _first_number(
            _payload_get(payload, "UPDATED_PAYMENT")
        )
        row["sf_coinsurance_payment"] = _first_number(
            picked.get("co_insurance_payment"),
            _payload_get(payload, "CO_INSURANCE_PAYMENT"),
        )
        row["sf_reduction"] = _first_number(
            picked.get("reductions"), _payload_get(payload, "REDUCTIONS", "REDUCTION")
        )
        row["sf_adjusted"] = _first_number(
            picked.get("adjusted"), _payload_get(payload, "ADJUSTED")
        )
        row["sf_charged_amount"] = _first_number(
            picked.get("charged_amount"), _payload_get(payload, "CHARGED_AMOUNT")
        )
        row["sf_details"] = _first_text(_payload_get(payload, "DETAILS"))
        row["sf_insurance_check_number"] = _first_text(
            picked.get("primary_check_number"),
            _payload_get(payload, "PRIMARY_CHECK_NUMBER", "INSURANCE_PAYEMNT_CHECK#"),
        )
        row["sf_insurance_check_date"] = _as_date_text(
            picked.get("primary_check_date")
            or _payload_get(payload, "PRIMARY_CHECK_DATE", "INSURANCE_PAYEMNT_CHECK_DATE")
        )
        row["sf_insurance_check_amount"] = _first_number(
            picked.get("primary_check_amount"),
            _payload_get(payload, "PRIMARY_CHECK_AMOUNT", "INSURANCE_PAYEMNT_CHECK_AMOUNT"),
        )
        row["sf_secondary_check_number"] = _first_text(
            picked.get("secondary_check_number"),
            _payload_get(payload, "SECONDARY_CHECK_NUMBER"),
        )
        row["sf_secondary_check_date"] = _as_date_text(
            picked.get("secondary_check_date")
            or _payload_get(payload, "SECONDARY_CHECK_DATE")
        )
        row["sf_secondary_check_amount"] = _first_number(
            picked.get("secondary_check_amount"),
            _payload_get(payload, "SECONDARY_CHECK_AMOUNT"),
        )
        row["sf_collector_1"] = _first_text(_payload_get(payload, "COLLECTOR_1"))
        row["sf_posting_date_1"] = _as_date_text(
            _payload_get(payload, "DATE_OF_FIRST_POSTING", "1ST_POSTING_DATE")
        )
        row["sf_collector_2"] = _first_text(_payload_get(payload, "COLLECTOR_2"))
        row["sf_posting_date_2"] = _as_date_text(
            _payload_get(payload, "DATE_OF_SECOND_POSTING", "2ND_POSTING_DATE")
        )
        row["sf_collector_3"] = _first_text(_payload_get(payload, "COLLECTOR_3"))
        row["sf_posting_date_3"] = _as_date_text(
            _payload_get(payload, "DATE_OF_THIRD_POSTING", "THIRD_POSTING_DATE")
        )
        row["sf_visit_status_sheet"] = _first_text(
            _payload_get(payload, "VISIT_STATUS")
        )
        row["sf_corrected"] = _first_text(_payload_get(payload, "CORRECTED"))
        row["sf_corrected_date"] = _as_date_text(_payload_get(payload, "CORRECTED_DATE"))
        row["sf_visit_id"] = _first_text(
            picked.get("sf_visit_id"), _payload_get(payload, "VISIT_ID")
        )
        row["sf_insurance_id"] = _first_text(_payload_get(payload, "INSURANCE_ID"))
        row["sf_secondary_insurance"] = _first_text(
            _payload_get(payload, "SECONDARY_INSURANCE")
        )
        row["sf_secondary_insurance_id"] = _first_text(
            _payload_get(payload, "SECONDARY_INSURANCE_ID")
        )
        row["sf_updated_check_number"] = _first_text(
            _payload_get(
                payload,
                "UPDATED_PAYMENT_CHECK#",
                "UPDATED_PAYMENT_CHECK_NUMBER",
                "UPDATED_CHECK_NUMBER",
                "THIRD_CHECK_NUMBER",
            )
        )
        row["sf_updated_check_date"] = _as_date_text(
            _payload_get(
                payload,
                "UPDATED_PAYMENT_CHECK_DATE",
                "UPDATED_CHECK_DATE",
                "THIRD_CHECK_DATE",
            )
        )
        row["sf_updated_check_amount"] = _first_number(
            _payload_get(
                payload,
                "UPDATED_PAYMENT_CHECK_AMOUNT",
                "UPDATED_CHECK_AMOUNT",
                "THIRD_CHECK_AMOUNT",
            )
        )
        row["sf_fourth_check_number"] = _first_text(
            _payload_get(payload, "FOURTH_CHECK_NUMBER", "4TH_CHECK#")
        )
        row["sf_fourth_check_date"] = _as_date_text(
            _payload_get(payload, "FOURTH_CHECK_DATE", "4TH_CHECK_DATE")
        )
        row["sf_fourth_check_amount"] = _first_number(
            _payload_get(payload, "FOURTH_CHECK_AMOUNT", "4TH_CHECK_AMOUNT")
        )
        row["sf_work_status"] = _first_text(_payload_get(payload, "WORK_STATUS"))
        row["sf_work_date"] = _as_date_text(_payload_get(payload, "WORK_DATE"))
        row["sf_denial_reason"] = _first_text(_payload_get(payload, "DENIAL_REASON"))
        row["sf_root_cause"] = _first_text(_payload_get(payload, "ROOTCAUSE", "ROOT_CAUSE"))
        row["sf_actions_taken"] = _first_text(_payload_get(payload, "ACTIONS_TAKEN"))
        row["sf_collection_status"] = _first_text(
            _payload_get(payload, "COLLECTION_STATUS")
        )


def overlay_ledger_totals(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Sum added (+) and deducted (abs of -) from the amount ledger."""
    if not rows:
        return
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        wid = str(row.get("work_item_id") or "").strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        ids.append(wid)
    if not ids:
        return
    totals = client.fetchall(
        conn,
        """
        SELECT
            work_item_id,
            COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS added_amount,
            COALESCE(SUM(ABS(amount)) FILTER (WHERE amount < 0), 0) AS deducted_amount
        FROM ops.eligibility_amount_ledger
        WHERE work_item_id = ANY(%s::uuid[])
        GROUP BY work_item_id
        """,
        (ids,),
    )
    by_id = {str(rec["work_item_id"]): rec for rec in totals}
    for row in rows:
        rec = by_id.get(str(row.get("work_item_id") or ""))
        if not rec:
            continue
        row["added_amount"] = _as_number(rec.get("added_amount"))
        row["deducted_amount"] = _as_number(rec.get("deducted_amount"))
        row["_ledger_present"] = True


_COMPACT_REF_SQL = """
CASE
  WHEN regexp_replace(upper(regexp_replace(coalesce(ref, ''), '\\.0+$', '')), '[^A-Z0-9]', '', 'g') ~ '^[0-9]+$'
  THEN COALESCE(
    NULLIF(ltrim(regexp_replace(upper(regexp_replace(coalesce(ref, ''), '\\.0+$', '')), '[^A-Z0-9]', '', 'g'), '0'), ''),
    '0'
  )
  ELSE regexp_replace(upper(regexp_replace(coalesce(ref, ''), '\\.0+$', '')), '[^A-Z0-9]', '', 'g')
END
"""


_TRACKER_DATE_INDEX_SQL = f"""
WITH src AS (
    SELECT txn_date, unnest(ARRAY[eft_1, eft_2, check_reference]) AS ref
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL
),
norm AS (
    SELECT txn_date, {_COMPACT_REF_SQL} AS compact
    FROM src
    WHERE ref IS NOT NULL AND btrim(ref) <> ''
)
SELECT compact, min(txn_date) AS txn_date
FROM norm
WHERE compact <> ''
GROUP BY compact
"""


def _tracker_date_index(recs: list[dict[str, Any]]) -> dict[str, str]:
    by_key: dict[str, str] = {}
    for rec in recs:
        compact = compact_check_key(rec.get("compact"))
        dos_key = _dos_key(rec.get("txn_date"))
        if compact and dos_key:
            by_key[compact] = dos_key
    return by_key


def load_export_tracker_dates(conn: psycopg.Connection) -> dict[str, str]:
    """One tracker scan for a whole export, instead of one scan per page."""
    return _tracker_date_index(client.fetchall(conn, _TRACKER_DATE_INDEX_SQL))


def overlay_tracker_dates(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    *,
    index: dict[str, str] | None = None,
) -> None:
    """Copy min(txn_date) from the tracker for each row's check number."""
    if not rows:
        return
    if index is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            key = compact_check_key(row.get("check_number"))
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
        if not keys:
            index = {}
        else:
            index = _tracker_date_index(
                client.fetchall(
                    conn,
                    f"""
                    WITH src AS (
                        SELECT txn_date, unnest(ARRAY[eft_1, eft_2, check_reference]) AS ref
                        FROM billing.transaction_tracker_row
                        WHERE deleted_at IS NULL
                    ),
                    norm AS (
                        SELECT txn_date, {_COMPACT_REF_SQL} AS compact
                        FROM src
                        WHERE ref IS NOT NULL AND btrim(ref) <> ''
                    )
                    SELECT compact, min(txn_date) AS txn_date
                    FROM norm
                    WHERE compact = ANY(%s::text[])
                    GROUP BY compact
                    """,
                    (keys,),
                )
            )
    for row in rows:
        key = compact_check_key(row.get("check_number"))
        row["tracker_date"] = index.get(key) if key else None


_EFT_TOTAL_INDEX_SQL = f"""
WITH tracker_src AS (
    SELECT eft_1 AS ref, amount
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL AND amount IS NOT NULL
    UNION ALL
    SELECT eft_2, amount
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL AND amount IS NOT NULL
    UNION ALL
    SELECT check_reference, amount
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL AND amount IS NOT NULL
),
tracker AS (
    SELECT {_COMPACT_REF_SQL} AS compact, max(amount)::numeric AS amount
    FROM tracker_src
    WHERE ref IS NOT NULL AND btrim(ref) <> ''
    GROUP BY 1
),
eob_src AS (
    SELECT check_eft_num AS ref, paid_amount_sum AS amount
    FROM billing.eob_check
    WHERE paid_amount_sum IS NOT NULL
),
eob AS (
    SELECT {_COMPACT_REF_SQL} AS compact, max(amount)::numeric AS amount
    FROM eob_src
    WHERE ref IS NOT NULL AND btrim(ref) <> ''
    GROUP BY 1
),
waystar_src AS (
    SELECT total_remit_amount, unnest(remit_numbers) AS ref
    FROM billing.waystar_claim
    WHERE COALESCE(total_remit_amount, 0) > 0
      AND COALESCE(array_length(remit_numbers, 1), 0) > 0
),
waystar AS (
    SELECT {_COMPACT_REF_SQL} AS compact,
           sum(total_remit_amount)::numeric AS amount
    FROM waystar_src
    WHERE ref IS NOT NULL AND btrim(ref) <> ''
    GROUP BY 1
),
keys AS (
    SELECT compact FROM tracker
    UNION
    SELECT compact FROM eob
    UNION
    SELECT compact FROM waystar
)
SELECT k.compact,
       COALESCE(t.amount, e.amount, y.amount) AS eft_total
FROM keys k
LEFT JOIN tracker t ON t.compact = k.compact
LEFT JOIN eob e ON e.compact = k.compact
LEFT JOIN waystar y ON y.compact = k.compact
"""


def _eft_total_index(recs: list[dict[str, Any]]) -> dict[str, float]:
    by_key: dict[str, float] = {}
    for rec in recs:
        compact = compact_check_key(rec.get("compact"))
        amount = _as_number(rec.get("eft_total"))
        if compact and amount is not None:
            by_key[compact] = amount
    return by_key


def load_export_eft_totals(conn: psycopg.Connection) -> dict[str, float]:
    """One tracker, EOB, and Waystar scan for a whole export."""
    return _eft_total_index(client.fetchall(conn, _EFT_TOTAL_INDEX_SQL))


def _apply_eft_totals(rows: list[dict[str, Any]], by_key: dict[str, float]) -> None:
    for row in rows:
        for num_key, amt_key in _CHECK_SLOT_KEYS:
            key = compact_check_key(row.get(num_key))
            if key and key in by_key:
                row[amt_key] = by_key[key]


def overlay_eft_totals(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    *,
    index: dict[str, float] | None = None,
) -> None:
    """Replace check-amount columns with the full EFT (tracker, then EOB, then Waystar)."""
    if not rows:
        return
    if index is not None:
        _apply_eft_totals(rows, index)
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for num_key, _amt_key in _CHECK_SLOT_KEYS:
            key = compact_check_key(row.get(num_key))
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    if not keys:
        return
    recs = client.fetchall(
        conn,
        f"""
        WITH wanted AS (
            SELECT unnest(%s::text[]) AS compact
        ),
        tracker_src AS (
            SELECT eft_1 AS ref, amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL AND amount IS NOT NULL
            UNION ALL
            SELECT eft_2, amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL AND amount IS NOT NULL
            UNION ALL
            SELECT check_reference, amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL AND amount IS NOT NULL
        ),
        tracker AS (
            SELECT {_COMPACT_REF_SQL} AS compact, max(amount)::numeric AS amount
            FROM tracker_src
            WHERE ref IS NOT NULL AND btrim(ref) <> ''
            GROUP BY 1
        ),
        eob_src AS (
            SELECT check_eft_num AS ref, paid_amount_sum AS amount
            FROM billing.eob_check
            WHERE paid_amount_sum IS NOT NULL
        ),
        eob AS (
            SELECT {_COMPACT_REF_SQL} AS compact, max(amount)::numeric AS amount
            FROM eob_src
            WHERE ref IS NOT NULL AND btrim(ref) <> ''
            GROUP BY 1
        ),
        waystar_src AS (
            SELECT total_remit_amount, unnest(remit_numbers) AS ref
            FROM billing.waystar_claim
            WHERE COALESCE(total_remit_amount, 0) > 0
              AND COALESCE(array_length(remit_numbers, 1), 0) > 0
        ),
        waystar AS (
            SELECT {_COMPACT_REF_SQL} AS compact,
                   sum(total_remit_amount)::numeric AS amount
            FROM waystar_src
            WHERE ref IS NOT NULL AND btrim(ref) <> ''
            GROUP BY 1
        )
        SELECT w.compact,
               COALESCE(t.amount, e.amount, y.amount) AS eft_total
        FROM wanted w
        LEFT JOIN tracker t ON t.compact = w.compact
        LEFT JOIN eob e ON e.compact = w.compact
        LEFT JOIN waystar y ON y.compact = w.compact
        """,
        (keys,),
    )
    _apply_eft_totals(rows, _eft_total_index(recs))


def overlay_pr1_reductions(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Fill recon_pr1_amount from EOB lines tagged PR-1."""
    if not rows:
        return
    emrs, doses = _emr_dos_keys(rows)
    if not emrs:
        return
    recs = client.fetchall(
        conn,
        f"""
        SELECT m.webpt_patient_id, el.date_of_service,
               SUM(COALESCE(el.deductible_amount, el.adjustment_amount))::numeric AS pr1_amount
        FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
        JOIN billing.waystar_webpt_map m ON m.webpt_patient_id = k.emr
        JOIN billing.eob_line el
          ON el.revflow_patient_id = m.waystar_claim_key
         AND el.date_of_service = k.dos
        WHERE el.pr_oa_codes ~* '{PR1_SQL_PATTERN}'
        GROUP BY 1, 2
        """,
        (emrs, doses),
    )
    by_key: dict[tuple[str, str], float] = {}
    for rec in recs:
        emr = str(rec.get("webpt_patient_id") or "").strip()
        dos_key = _dos_key(rec.get("date_of_service"))
        amount = _as_number(rec.get("pr1_amount"))
        if emr and dos_key and amount is not None:
            by_key[(emr, dos_key)] = amount
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        if emr and dos_key and (emr, dos_key) in by_key:
            row["recon_pr1_amount"] = by_key[(emr, dos_key)]


def overlay_rtm_amounts(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Sum paid amount on RTM CPT lines for each visit."""
    if not rows:
        return
    emrs, doses = _emr_dos_keys(rows)
    if not emrs:
        return
    recs = client.fetchall(
        conn,
        """
        SELECT m.webpt_patient_id, el.date_of_service,
               SUM(el.paid_amount)::numeric AS rtm_amount
        FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
        JOIN billing.waystar_webpt_map m ON m.webpt_patient_id = k.emr
        JOIN billing.eob_line el
          ON el.revflow_patient_id = m.waystar_claim_key
         AND el.date_of_service = k.dos
        WHERE el.cpt_code IN ('98975', '98977', '98979', '98980', '98985')
        GROUP BY 1, 2
        """,
        (emrs, doses),
    )
    by_key: dict[tuple[str, str], float] = {}
    for rec in recs:
        emr = str(rec.get("webpt_patient_id") or "").strip()
        dos_key = _dos_key(rec.get("date_of_service"))
        amount = _as_number(rec.get("rtm_amount"))
        if emr and dos_key and amount is not None:
            by_key[(emr, dos_key)] = amount
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        if emr and dos_key and (emr, dos_key) in by_key:
            row["recon_rtm_amount"] = by_key[(emr, dos_key)]


def _primary_check_for_oa23(row: dict[str, Any]) -> str | None:
    """EOB check the visit's primary payment came from."""
    number = row.get("recon_check_number")
    if not _as_text(number):
        ctx = row.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {}
        if isinstance(ctx, dict):
            number = ctx.get("primary_check_number")
    return compact_check_key(number)


def overlay_oa23_amounts(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Sum OA-23 on the visit's primary check. That slice is co-insurance."""
    if not rows:
        return
    emrs: list[str] = []
    doses: list[date] = []
    compacts: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        parsed = row.get("dos")
        if isinstance(parsed, datetime):
            parsed = parsed.date()
        elif not isinstance(parsed, date):
            parsed = parse_date(str(parsed or ""))
        compact = _primary_check_for_oa23(row)
        if not emr or parsed is None or not compact:
            continue
        key = (emr, parsed.isoformat(), compact)
        if key in seen:
            continue
        seen.add(key)
        emrs.append(emr)
        doses.append(parsed)
        compacts.append(compact)
    if not emrs:
        return
    recs = client.fetchall(
        conn,
        f"""
        WITH keys AS (
            SELECT * FROM unnest(%s::text[], %s::date[], %s::text[]) AS k(emr, dos, compact)
        ),
        lines AS (
            SELECT
                m.webpt_patient_id,
                el.date_of_service,
                ec.check_eft_num AS ref,
                el.oa23_amount
            FROM keys k
            JOIN billing.waystar_webpt_map m ON m.webpt_patient_id = k.emr
            JOIN billing.eob_line el
              ON el.revflow_patient_id = m.waystar_claim_key
             AND el.date_of_service = k.dos
            JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
            WHERE COALESCE(el.oa23_amount, 0) <> 0
        )
        SELECT
            lines.webpt_patient_id,
            lines.date_of_service,
            {_COMPACT_REF_SQL} AS compact,
            SUM(lines.oa23_amount)::numeric AS oa23_amount
        FROM lines
        JOIN keys k
          ON k.emr = lines.webpt_patient_id
         AND k.dos = lines.date_of_service
         AND k.compact = {_COMPACT_REF_SQL}
        GROUP BY 1, 2, 3
        """,
        (emrs, doses, compacts),
    )
    by_key: dict[tuple[str, str, str], float] = {}
    for rec in recs:
        emr = str(rec.get("webpt_patient_id") or "").strip()
        dos_key = _dos_key(rec.get("date_of_service"))
        compact = compact_check_key(rec.get("compact"))
        amount = _as_number(rec.get("oa23_amount"))
        if emr and dos_key and compact and amount:
            by_key[(emr, dos_key, compact)] = amount
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        compact = _primary_check_for_oa23(row)
        if not emr or not dos_key or not compact:
            continue
        amount = by_key.get((emr, dos_key, compact))
        if amount:
            row["recon_oa23_amount"] = amount
            row["recon_oa23_check"] = compact


def overlay_denial_reasons(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """Fill recon_denial_reason from Waystar events, then EOB CARCs."""
    if not rows:
        return
    emrs, doses = _emr_dos_keys(rows)
    if not emrs:
        return
    waystar = client.fetchall(
        conn,
        """
        SELECT DISTINCT ON (m.webpt_patient_id, c.from_date)
            m.webpt_patient_id, c.from_date,
            COALESCE(
                NULLIF(btrim(c.last_event_message), ''),
                NULLIF(btrim(c.last_note), ''),
                NULLIF(btrim(c.status), '')
            ) AS reason
        FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
        JOIN billing.waystar_webpt_map m ON m.webpt_patient_id = k.emr
        JOIN billing.waystar_claim c
          ON c.claim_key = m.waystar_claim_key
         AND c.from_date = k.dos
        WHERE c.status ILIKE '%%deni%%'
           OR c.last_event_message ILIKE '%%deni%%'
           OR c.last_note ILIKE '%%deni%%'
        ORDER BY m.webpt_patient_id, c.from_date, c.sequence, c.scraped_at DESC
        """,
        (emrs, doses),
    )
    by_key: dict[tuple[str, str], str] = {}
    for rec in waystar:
        emr = str(rec.get("webpt_patient_id") or "").strip()
        dos_key = _dos_key(rec.get("from_date"))
        reason = _as_text(rec.get("reason"))
        if emr and dos_key and reason:
            by_key[(emr, dos_key)] = reason
    missing_emrs: list[str] = []
    missing_doses: list[date] = []
    for emr, dos in zip(emrs, doses):
        if (emr, dos.isoformat()) not in by_key:
            missing_emrs.append(emr)
            missing_doses.append(dos)
    if missing_emrs:
        denials = client.fetchall(
            conn,
            """
            SELECT p.webpt_patient_id, COALESCE(d.denial_date, k.dos) AS denial_dos,
                   string_agg(DISTINCT NULLIF(btrim(d.reason_code), ''), '; ') AS reason
            FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
            JOIN core.patient p ON p.webpt_patient_id = k.emr
            JOIN billing.claim c ON c.patient_id = p.patient_id
            JOIN billing.denial_record d ON d.claim_id = c.claim_id
            WHERE NULLIF(btrim(d.reason_code), '') IS NOT NULL
              AND (d.denial_date = k.dos OR d.denial_date IS NULL)
            GROUP BY 1, 2
            """,
            (missing_emrs, missing_doses),
        )
        for rec in denials:
            emr = str(rec.get("webpt_patient_id") or "").strip()
            dos_key = _dos_key(rec.get("denial_dos"))
            reason = _as_text(rec.get("reason"))
            if emr and dos_key and reason and (emr, dos_key) not in by_key:
                by_key[(emr, dos_key)] = reason
        missing_emrs = []
        missing_doses = []
        for emr, dos in zip(emrs, doses):
            if (emr, dos.isoformat()) not in by_key:
                missing_emrs.append(emr)
                missing_doses.append(dos)
    if missing_emrs:
        carcs = client.fetchall(
            conn,
            """
            SELECT m.webpt_patient_id, el.date_of_service,
                   string_agg(DISTINCT NULLIF(btrim(el.carcs), ''), '; ') AS reason
            FROM unnest(%s::text[], %s::date[]) AS k(emr, dos)
            JOIN billing.waystar_webpt_map m ON m.webpt_patient_id = k.emr
            JOIN billing.eob_line el
              ON el.revflow_patient_id = m.waystar_claim_key
             AND el.date_of_service = k.dos
            WHERE NULLIF(btrim(el.carcs), '') IS NOT NULL
            GROUP BY 1, 2
            """,
            (missing_emrs, missing_doses),
        )
        for rec in carcs:
            emr = str(rec.get("webpt_patient_id") or "").strip()
            dos_key = _dos_key(rec.get("date_of_service"))
            reason = _as_text(rec.get("reason"))
            if emr and dos_key and reason and (emr, dos_key) not in by_key:
                by_key[(emr, dos_key)] = reason
    for row in rows:
        emr = str(row.get("emr_patient_id") or "").strip()
        dos_key = _dos_key(row.get("dos"))
        if emr and dos_key and (emr, dos_key) in by_key:
            row["recon_denial_reason"] = by_key[(emr, dos_key)]


def _drop_overlay_keys(row: dict[str, Any]) -> dict[str, Any]:
    for key in _RECON_OVERLAY_KEYS:
        row.pop(key, None)
    for key in _SF_OVERLAY_KEYS:
        row.pop(key, None)
    row.pop("_ledger_present", None)
    row.pop("_sf_overlay_mode", None)
    return row


def normalize_visit_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"blank"}:
        return "pending"
    return text


def sf_overlay_mode(
    ours_status: Any,
    ours_paid: Any,
    sf_status: Any,
    sf_paid: Any,
) -> str:
    """Pending+SF-closed copies everything; paid match fills extras; else skip extras."""
    ours = normalize_visit_status(ours_status)
    sf = normalize_visit_status(sf_status)
    if ours == "pending" and sf in SF_CLOSED_STATUSES:
        return SF_OVERLAY_TAKE_ALL
    ours_amt = _as_number(ours_paid) or 0.0
    sf_amt = _as_number(sf_paid) or 0.0
    if abs(ours_amt - sf_amt) <= SF_PAID_TOLERANCE:
        return SF_OVERLAY_EXTRAS_ONLY
    return SF_OVERLAY_SKIP


def _missing_tracker_set(raw: Any) -> set[str]:
    values: list[Any]
    if raw is None:
        values = []
    elif isinstance(raw, str):
        values = [p for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: set[str] = set()
    for value in values:
        key = compact_check_key(value)
        if key:
            out.add(key)
    return out


_CHECK_SLOT_KEYS = (
    ("insurance_check_number", "insurance_check_amount"),
    ("secondary_check_number", "secondary_check_amount"),
    ("updated_check_number", "updated_check_amount"),
    ("fourth_check_number", "fourth_check_amount"),
)


def _missing_check_originals(raw: Any) -> list[str]:
    values: list[Any]
    if raw is None:
        values = []
    elif isinstance(raw, str):
        values = [p for p in raw.replace(";", ",").split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _as_text(value)
        key = compact_check_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _place_missing_tracker_checks(
    row: dict[str, Any],
    missing: set[str],
    raw: Any = None,
) -> None:
    """Keep missing EFTs visible on the sheet, even when SF hid that number."""
    if not missing:
        return
    if raw is None:
        raw = row.get("recon_missing_tracker_checks")
    displayed = {
        compact_check_key(row.get(num_key))
        for num_key, _amt_key in _CHECK_SLOT_KEYS
    }
    displayed.discard(None)
    leftover = [
        number
        for number in _missing_check_originals(raw)
        if compact_check_key(number) in missing
        and compact_check_key(number) not in displayed
    ]
    for number in leftover:
        for num_key, _amt_key in _CHECK_SLOT_KEYS:
            if _as_text(row.get(num_key)):
                continue
            row[num_key] = number
            break


def apply_sf_overlay_gate(row: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Drop or keep sf_* keys according to take-all / extras-only / skip."""
    ours_status = row.get("source_visit_status") or row.get("recon_visit_status")
    ours_paid = _first_number(row.get("recon_total_paid"), ctx.get("total_paid"))
    mode = sf_overlay_mode(
        ours_status,
        ours_paid,
        row.get("sf_status"),
        row.get("sf_insurance_payment"),
    )
    if mode == SF_OVERLAY_SKIP:
        for key in _SF_OVERLAY_KEYS:
            row.pop(key, None)
    elif mode == SF_OVERLAY_EXTRAS_ONLY:
        for key in _SF_PROTECTED_KEYS:
            row.pop(key, None)
    elif mode == SF_OVERLAY_TAKE_ALL:
        sf_st = _as_text(row.get("sf_status"))
        if sf_st:
            row["source_visit_status"] = sf_st
    row["_sf_overlay_mode"] = mode
    return mode


def _apply_oa23_coinsurance_split(row: dict[str, Any]) -> None:
    """OA-23 on the primary check is the co-insurance slice of that same check."""
    overrides = parse_manual_overrides(row.get("manual_overrides"))
    if {"insurance_payment", "paid_amount", "coinsurance_payment"} & overrides.keys():
        return
    if _as_number(row.get("coinsurance_payment")):
        return
    primary = compact_check_key(row.get("insurance_check_number"))
    secondary = compact_check_key(row.get("secondary_check_number"))
    if secondary and secondary != primary:
        return
    oa23_check = compact_check_key(row.get("recon_oa23_check"))
    if oa23_check and primary and oa23_check != primary:
        return
    oa23 = _as_number(row.get("recon_oa23_amount"))
    insurance = _as_number(row.get("insurance_payment"))
    if oa23 is None or insurance is None or not (0 < oa23 <= insurance):
        return
    row["coinsurance_payment"] = round(oa23, 2)
    row["insurance_payment"] = round(insurance - oa23, 2)
    row["paid_amount"] = row["insurance_payment"]


def _prefer_larger_primary_payment(row: dict[str, Any]) -> None:
    """Keep Insurance Payment as the larger amount; Co-Insurance the smaller."""
    insurance = _as_number(row.get("insurance_payment"))
    coins = _as_number(row.get("coinsurance_payment"))
    if insurance is None or coins is None or coins <= insurance:
        return
    row["insurance_payment"] = coins
    row["coinsurance_payment"] = insurance
    row["paid_amount"] = coins
    for left, right in (
        ("insurance_check_number", "secondary_check_number"),
        ("insurance_check_date", "secondary_check_date"),
        ("insurance_check_amount", "secondary_check_amount"),
    ):
        row[left], row[right] = row.get(right), row.get(left)
    row["check_number"] = row.get("insurance_check_number")
    row["check_date"] = row.get("insurance_check_date")


def attach_sheet_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten sheet money/check fields from SF, live recon, then stored context."""
    ctx = row.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    apply_sf_overlay_gate(row, ctx)
    take_all = row.get("_sf_overlay_mode") == SF_OVERLAY_TAKE_ALL
    if take_all:
        insurance_payment = _first_number(
            row.get("sf_insurance_payment"),
            row.get("recon_check_amount"),
            row.get("recon_total_paid"),
            ctx.get("total_paid"),
        )
        insurance_check = _first_text(
            row.get("sf_insurance_check_number"),
            row.get("recon_check_number"),
            ctx.get("primary_check_number"),
            row.get("reference_number"),
        )
        insurance_check_date = _as_date_text(
            row.get("sf_insurance_check_date")
            or row.get("recon_check_date")
            or ctx.get("primary_check_date")
        )
    else:
        insurance_payment = _first_number(
            row.get("recon_check_amount"),
            row.get("recon_total_paid"),
            ctx.get("total_paid"),
            row.get("sf_insurance_payment"),
        )
        insurance_check = _first_text(
            row.get("recon_check_number"),
            ctx.get("primary_check_number"),
            row.get("sf_insurance_check_number"),
            row.get("reference_number"),
        )
        insurance_check_date = _as_date_text(
            row.get("recon_check_date")
            or ctx.get("primary_check_date")
            or row.get("sf_insurance_check_date")
        )
    row["client_payment"] = _first_number(row.get("sf_client_payment"), ctx.get("client_payment"))
    row["insurance_payment"] = insurance_payment
    row["paid_amount"] = insurance_payment
    same_payer_updated = bool(
        _first_number(row.get("recon_third_check_amount")) is not None
        or _first_text(row.get("recon_third_check_number"))
    ) and not (
        _first_number(row.get("recon_secondary_check_amount")) is not None
        or _first_text(row.get("recon_secondary_check_number"))
    )
    row["updated_payment"] = _first_number(
        row.get("recon_third_check_amount"),
        ctx.get("third_check_amount"),
        ctx.get("updated_check_amount"),
        row.get("sf_updated_payment"),
        ctx.get("updated_payment"),
    )
    if same_payer_updated:
        row["coinsurance_payment"] = _first_number(row.get("recon_secondary_check_amount"))
    else:
        row["coinsurance_payment"] = _first_number(
            row.get("recon_secondary_check_amount"),
            ctx.get("secondary_check_amount"),
            row.get("sf_coinsurance_payment"),
            ctx.get("coinsurance_payment"),
        )
    row["rtm"] = _as_number(row.get("recon_rtm_amount"))
    row["reduction"] = _first_number(
        row.get("recon_pr1_amount"),
        row.get("sf_reduction"),
        ctx.get("reduction"),
    )
    row["charged_amount"] = _first_number(
        row.get("sf_charged_amount"), ctx.get("charged_amount")
    )
    row["adjusted"] = _first_number(row.get("sf_adjusted"), ctx.get("adjusted"))
    row["details"] = _first_text(
        row.get("recon_denial_reason"),
        row.get("sf_details"),
        ctx.get("details"),
        row.get("sf_denial_reason"),
        ctx.get("denial_reason"),
    )
    row["check_number"] = insurance_check
    row["insurance_check_number"] = insurance_check
    row["check_date"] = insurance_check_date
    row["insurance_check_date"] = insurance_check_date
    row["insurance_check_amount"] = (
        _first_number(
            row.get("sf_insurance_check_amount"),
            row.get("recon_check_amount"),
            ctx.get("primary_check_amount"),
        )
        if take_all
        else _first_number(
            row.get("recon_check_amount"),
            ctx.get("primary_check_amount"),
            row.get("sf_insurance_check_amount"),
        )
    )
    if same_payer_updated:
        row["secondary_check_number"] = _first_text(row.get("recon_secondary_check_number"))
        row["secondary_check_date"] = _as_date_text(row.get("recon_secondary_check_date"))
        row["secondary_check_amount"] = _first_number(row.get("recon_secondary_check_amount"))
    else:
        row["secondary_check_number"] = _first_text(
            row.get("recon_secondary_check_number"),
            ctx.get("secondary_check_number"),
            row.get("sf_secondary_check_number"),
        )
        row["secondary_check_date"] = _as_date_text(
            row.get("recon_secondary_check_date")
            or ctx.get("secondary_check_date")
            or row.get("sf_secondary_check_date")
        )
        row["secondary_check_amount"] = _first_number(
            row.get("recon_secondary_check_amount"),
            ctx.get("secondary_check_amount"),
            row.get("sf_secondary_check_amount"),
        )
    row["collector_1"] = _first_text(row.get("sf_collector_1"), ctx.get("collector_1"))
    row["posting_date_1"] = _as_date_text(
        row.get("sf_posting_date_1") or ctx.get("posting_date_1")
    )
    row["collector_2"] = _first_text(row.get("sf_collector_2"), ctx.get("collector_2"))
    row["posting_date_2"] = _as_date_text(
        row.get("sf_posting_date_2") or ctx.get("posting_date_2")
    )
    row["collector_3"] = _first_text(row.get("sf_collector_3"), ctx.get("collector_3"))
    row["posting_date_3"] = _as_date_text(
        row.get("sf_posting_date_3") or ctx.get("posting_date_3")
    )
    row["visit_status_sheet"] = _first_text(
        row.get("sf_visit_status_sheet"), ctx.get("visit_status_sheet")
    )
    row["corrected"] = _first_text(row.get("sf_corrected"), ctx.get("corrected"))
    row["corrected_date"] = _as_date_text(
        row.get("sf_corrected_date") or ctx.get("corrected_date")
    )
    row["sf_visit_id"] = _first_text(row.get("sf_visit_id"), ctx.get("sf_visit_id"))
    row["insurance_id"] = _first_text(row.get("sf_insurance_id"), ctx.get("insurance_id"))
    row["secondary_insurance"] = _first_text(
        row.get("sf_secondary_insurance"), ctx.get("secondary_insurance")
    )
    row["secondary_insurance_id"] = _first_text(
        row.get("sf_secondary_insurance_id"), ctx.get("secondary_insurance_id")
    )
    row["updated_check_number"] = _first_text(
        row.get("recon_third_check_number"),
        ctx.get("third_check_number"),
        ctx.get("updated_check_number"),
        row.get("sf_updated_check_number"),
    )
    row["updated_check_date"] = _as_date_text(
        row.get("recon_third_check_date")
        or ctx.get("third_check_date")
        or ctx.get("updated_check_date")
        or row.get("sf_updated_check_date")
    )
    row["updated_check_amount"] = _first_number(
        row.get("recon_third_check_amount"),
        ctx.get("third_check_amount"),
        ctx.get("updated_check_amount"),
        row.get("sf_updated_check_amount"),
    )
    row["fourth_check_number"] = _first_text(
        row.get("recon_fourth_check_number"),
        ctx.get("fourth_check_number"),
        row.get("sf_fourth_check_number"),
    )
    row["fourth_check_date"] = _as_date_text(
        row.get("recon_fourth_check_date")
        or ctx.get("fourth_check_date")
        or row.get("sf_fourth_check_date")
    )
    row["fourth_check_amount"] = _first_number(
        row.get("recon_fourth_check_amount"),
        ctx.get("fourth_check_amount"),
        row.get("sf_fourth_check_amount"),
    )
    _prefer_larger_primary_payment(row)
    raw_missing = row.get("recon_missing_tracker_checks") or ctx.get(
        "missing_tracker_checks"
    )
    missing = _missing_tracker_set(raw_missing)
    _place_missing_tracker_checks(row, missing, raw_missing)
    row["work_status"] = _first_text(row.get("sf_work_status"), ctx.get("work_status"))
    row["work_date"] = _as_date_text(row.get("sf_work_date") or ctx.get("work_date"))
    row["denial_reason"] = _first_text(
        row.get("recon_denial_reason"),
        row.get("sf_denial_reason"),
        ctx.get("denial_reason"),
        row.get("details"),
    )
    row["root_cause"] = _first_text(row.get("sf_root_cause"), ctx.get("root_cause"))
    row["actions_taken"] = _first_text(row.get("sf_actions_taken"), ctx.get("actions_taken"))
    row["collection_status"] = _first_text(
        row.get("sf_collection_status"), ctx.get("collection_status")
    )
    row["pending_reason"] = _as_text(row.get("recon_pending_reason"))
    recon_status = _as_text(row.get("recon_visit_status"))
    recon_reason = (row.get("pending_reason") or "").lower()
    if (
        recon_status
        and recon_reason == "pending_tracker"
        and row.get("_sf_overlay_mode") != SF_OVERLAY_TAKE_ALL
    ):
        row["source_visit_status"] = recon_status
    elif not _as_text(row.get("source_visit_status")) and recon_status:
        row["source_visit_status"] = recon_status
    _apply_oa23_coinsurance_split(row)
    finalize_sheet_fields(row)
    return row


def finalize_sheet_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Sync Paid/Check aliases, compute Total Amount, fill Added/Deducted fallback."""
    overrides = parse_manual_overrides(row.get("manual_overrides"))
    if "paid_amount" in overrides and "insurance_payment" not in overrides:
        row["insurance_payment"] = _as_number(row.get("paid_amount"))
    if "insurance_payment" in overrides and "paid_amount" not in overrides:
        row["paid_amount"] = _as_number(row.get("insurance_payment"))
    if row.get("paid_amount") is None:
        row["paid_amount"] = _as_number(row.get("insurance_payment"))
    if row.get("insurance_payment") is None:
        row["insurance_payment"] = _as_number(row.get("paid_amount"))
    if "check_number" in overrides and "insurance_check_number" not in overrides:
        row["insurance_check_number"] = _as_text(row.get("check_number"))
    if "insurance_check_number" in overrides and "check_number" not in overrides:
        row["check_number"] = _as_text(row.get("insurance_check_number"))
    if not _as_text(row.get("check_number")):
        row["check_number"] = _as_text(row.get("insurance_check_number"))
    if not _as_text(row.get("insurance_check_number")):
        row["insurance_check_number"] = _as_text(row.get("check_number"))
    if "check_date" in overrides and "insurance_check_date" not in overrides:
        row["insurance_check_date"] = _as_date_text(row.get("check_date"))
    if "insurance_check_date" in overrides and "check_date" not in overrides:
        row["check_date"] = _as_date_text(row.get("insurance_check_date"))
    if not _as_text(row.get("check_date")):
        row["check_date"] = _as_date_text(row.get("insurance_check_date"))
    if not _as_text(row.get("insurance_check_date")):
        row["insurance_check_date"] = _as_date_text(row.get("check_date"))
    if not _as_text(row.get("collector_1")):
        row["collector_1"] = _first_text(row.get("assigned_to_code"), row.get("assigned_to_name"))
    row["total_amount"] = _money_sum(
        row.get("insurance_payment"),
        row.get("updated_payment"),
        row.get("coinsurance_payment"),
    )
    if not row.get("_ledger_present"):
        updated = _as_number(row.get("updated_payment")) or 0.0
        reduction = _as_number(row.get("reduction")) or 0.0
        row["added_amount"] = round(max(updated, 0.0), 2)
        row["deducted_amount"] = round(abs(min(updated, 0.0)) + abs(reduction), 2)
    return row


def parse_manual_overrides(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _history_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return text or None


def _compare_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if key in MONEY_FIELDS:
        num = _as_number(value)
        return None if num is None else round(num, 4)
    if key in DATE_OVERRIDE_FIELDS:
        parsed = parse_date(value)
        return parsed.isoformat() if parsed else None
    if key == "source_visit_status":
        text = _as_text(value)
        return text.lower() if text else None
    return _as_text(value)


def normalize_edit_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if key in MONEY_FIELDS:
        num = _as_number(value)
        if num is None:
            raise ValueError(f"{key} must be a number")
        return round(num, 4)
    if key in DATE_OVERRIDE_FIELDS:
        parsed = parse_date(value)
        if parsed is None:
            raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
        return parsed.isoformat()
    if key == "source_visit_status":
        text = _as_text(value)
        return text.lower() if text else None
    return _as_text(value)


def apply_manual_overrides(row: dict[str, Any]) -> dict[str, Any]:
    """Apply stored manual edits after recon/tracker overlays."""
    overrides = parse_manual_overrides(row.get("manual_overrides"))
    row["manual_overrides"] = overrides
    for key, raw in overrides.items():
        if key not in OVERRIDE_FIELDS:
            continue
        if key in MONEY_FIELDS:
            row[key] = _as_number(raw)
        elif key in DATE_OVERRIDE_FIELDS:
            parsed = parse_date(raw)
            row[key] = parsed.isoformat() if parsed else _as_text(raw)
        elif key == "source_visit_status":
            text = _as_text(raw)
            row[key] = text.lower() if text else None
        else:
            row[key] = _as_text(raw)
    finalize_sheet_fields(row)
    return row


def plan_work_item_patch(
    item: dict[str, Any],
    updates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], bool]:
    """Compute direct column updates, resulting overrides, and history rows."""
    current_ov = parse_manual_overrides(item.get("manual_overrides"))
    new_ov = dict(current_ov)
    direct: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    for key, raw in updates.items():
        if key not in EDITABLE_FIELDS:
            continue
        new_val = normalize_edit_value(key, raw)
        old_cmp = _compare_value(key, item.get(key))
        new_cmp = _compare_value(key, new_val)
        if key in OVERRIDE_FIELDS:
            had_override = key in current_ov
            if new_val is None:
                if not had_override:
                    continue
                new_ov.pop(key, None)
                history.append(
                    {
                        "column_name": key,
                        "old_value": _history_text(item.get(key)),
                        "new_value": None,
                    }
                )
                continue
            if old_cmp == new_cmp:
                continue
            new_ov[key] = new_val
            history.append(
                {
                    "column_name": key,
                    "old_value": _history_text(item.get(key)),
                    "new_value": _history_text(new_val),
                }
            )
            continue
        if old_cmp == new_cmp:
            continue
        direct[key] = new_val
        history.append(
            {
                "column_name": key,
                "old_value": _history_text(item.get(key)),
                "new_value": _history_text(new_val),
            }
        )
    if "work_date" not in updates:
        triggered = any(h["column_name"] in WORK_DATE_TRIGGER_FIELDS for h in history)
        current_date = _as_date_text(item.get("work_date")) or _as_date_text(new_ov.get("work_date"))
        if triggered and not current_date:
            today = date.today().isoformat()
            new_ov["work_date"] = today
            history.append(
                {
                    "column_name": "work_date",
                    "old_value": None,
                    "new_value": today,
                }
            )
    ov_changed = new_ov != current_ov
    return direct, new_ov, history, ov_changed


def list_statuses(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT status_key, display_name, sort_order, is_terminal, is_active
        FROM ref.eligibility_status
        WHERE is_active
        ORDER BY sort_order
        """,
    )


def list_reasons(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT reason_key, display_name, requires_text, sort_order
        FROM ref.eligibility_change_reason
        ORDER BY sort_order
        """,
    )


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = month.split("-")
    start = date(int(y), int(m), 1)
    if int(m) == 12:
        end = date(int(y) + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(int(y), int(m) + 1, 1) - timedelta(days=1)
    return start, end


VISIT_STATUS_ORDER = (
    "pending",
    "paid",
    "partial",
    "denied",
    "deduct",
    "collection",
    "patient_responsibility",
)


def sort_visit_statuses(values: list[str]) -> list[str]:
    order = {key: i for i, key in enumerate(VISIT_STATUS_ORDER)}
    unique: list[str] = []
    seen: set[str] = set()
    for raw in values:
        key = str(raw or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    unique.sort(key=lambda x: (order.get(x, 100), x))
    return unique


def _parse_check_dates(values: list[str] | None) -> list[date]:
    out: list[date] = []
    seen: set[date] = set()
    for raw in values or []:
        parsed = parse_date(raw)
        if parsed is None:
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


FILTER_BLANK = "__blank__"


def _split_blank(values: list[str] | None) -> tuple[list[str], bool]:
    real: list[str] = []
    want_blank = False
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if text == FILTER_BLANK:
            want_blank = True
            continue
        real.append(text)
    return real, want_blank


def _or_clauses(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _build_filters(
    *,
    q: str | None,
    facility: list[str] | None,
    month: list[str] | None,
    insurance: list[str] | None,
    status: list[str] | None,
    assigned_to: list[str] | None,
    unassigned: bool = False,
    visit_status: list[str] | None = None,
    check_date: list[str] | None = None,
    queue: str | None = None,
    bucket: str | None = None,
    collection_status: list[str] | None = None,
    root_cause: list[str] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = [
        "1=1",
        ELIGIBILITY_MIN_DOS_SQL,
        ELIGIBILITY_MAX_DOS_SQL,
    ]
    if not _is_collection_queue(queue):
        clauses.append(KEEP_WORK_ITEM_SQL)
    params: list[Any] = []
    if q:
        q_text = str(q).strip()
        compact_q = compact_check_key(q_text)
        emr_digits = re.sub(r"\D", "", q_text)
        eft_sql = ""
        if compact_q:
            compact_primary = _compact_sql("rv.primary_check_number")
            compact_secondary = _compact_sql("rv.secondary_check_number")
            compact_third = _compact_sql("rv.third_check_number")
            compact_fourth = _compact_sql("rv.fourth_check_number")
            compact_ctx_p = _compact_sql("wi.context->>'primary_check_number'")
            compact_ctx_s = _compact_sql("wi.context->>'secondary_check_number'")
            compact_ctx_t = _compact_sql("wi.context->>'third_check_number'")
            compact_ctx_u = _compact_sql("wi.context->>'updated_check_number'")
            compact_ctx_f = _compact_sql("wi.context->>'fourth_check_number'")
            compact_ref = _compact_sql("wi.reference_number")
            eft_sql = f"""
                OR {compact_ctx_p} = %s
                OR {compact_ctx_s} = %s
                OR {compact_ctx_t} = %s
                OR {compact_ctx_u} = %s
                OR {compact_ctx_f} = %s
                OR {compact_ref} = %s
                OR EXISTS (
                    SELECT 1 FROM billing.reconciliation_visit_agg rv
                    WHERE rv.reconciliation_run_id = (
                        SELECT reconciliation_run_id
                        FROM billing.reconciliation_run
                        WHERE status = 'success'
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                      AND rv.webpt_patient_id = wi.emr_patient_id
                      AND rv.date_of_service = wi.dos
                      AND (
                        {compact_primary} = %s
                        OR {compact_secondary} = %s
                        OR {compact_third} = %s
                        OR {compact_fourth} = %s
                      )
                )
            """
        digit_emr = bool(re.fullmatch(r"\d+", q_text)) and len(q_text) >= 5
        has_letter = bool(re.search(r"[A-Za-z]", q_text))
        if digit_emr:
            clauses.append(
                f"""(
                    wi.emr_patient_id = %s
                    OR wi.emr_patient_id LIKE %s
                    OR {_ACCOUNT_SEARCH_SQL}
                )"""
            )
            params.extend(
                [emr_digits, f"{emr_digits}%", f"%{emr_digits}%", f"{emr_digits}%"]
            )
        elif compact_q and not has_letter:
            eft_clause = eft_sql.strip()
            if eft_clause.startswith("OR"):
                eft_clause = eft_clause[2:].strip()
            clauses.append(f"({eft_clause})")
            params.extend([compact_q] * 10)
        else:
            like = f"%{q_text}%"
            clauses.append(
                f"""(
                    wi.patient_name ILIKE %s
                    OR wi.emr_patient_id ILIKE %s
                    OR wi.reference_number ILIKE %s
                    OR wi.notes ILIKE %s
                    OR wi.insurance_name ILIKE %s
                    OR {_ACCOUNT_SEARCH_SQL}
                    {eft_sql}
                )"""
            )
            params.extend([like, like, like, like, like, like, f"%{emr_digits}%" if emr_digits else like])
            if compact_q:
                params.extend([compact_q] * 10)
    if facility:
        names, want_blank = _split_blank(facility)
        parts: list[str] = []
        if names:
            parts.append(
                """(
                wi.facility_name = ANY(%s)
                OR EXISTS (
                    SELECT 1 FROM ref.facility f
                    WHERE f.name = ANY(%s)
                      AND f.webpt_facility_id = wi.facility_name
                )
            )"""
            )
            params.append(names)
            params.append(names)
        if want_blank:
            parts.append(
                """NULLIF(BTRIM(COALESCE(
                    (
                        SELECT f.name FROM ref.facility f
                        WHERE f.webpt_facility_id = wi.facility_name
                        LIMIT 1
                    ),
                    wi.facility_name
                )), '') IS NULL"""
            )
        if parts:
            clauses.append(_or_clauses(parts))
    if insurance:
        from cashflow_db.repository.insurance import is_blank_sql

        names, want_blank = _split_blank(insurance)
        parts = []
        if names:
            parts.append("lower(btrim(COALESCE(wi.insurance_name, ''))) = ANY(%s)")
            params.append([name.lower() for name in names])
        if want_blank:
            parts.append(is_blank_sql("wi.insurance_name"))
        if parts:
            clauses.append(_or_clauses(parts))
    if status:
        clauses.append("wi.eligibility_status = ANY(%s)")
        params.append(status)
    if visit_status:
        names, want_blank = _split_blank(visit_status)
        parts = []
        if names:
            parts.append(
                f"lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = ANY(%s)"
            )
            params.append([name.strip().lower() for name in names])
        if want_blank:
            parts.append(
                f"NULLIF(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, '')), '') IS NULL"
            )
        if parts:
            clauses.append(_or_clauses(parts))
    if collection_status:
        names, want_blank = _split_blank(collection_status)
        parts = []
        if names:
            folded = [name.lower() for name in names]
            parts.append(
                """(
                    lower(btrim(COALESCE(
                        NULLIF(btrim(wi.manual_overrides->>'collection_status'), ''),
                        NULLIF(btrim(wi.context->>'collection_status'), ''),
                        ''
                    ))) = ANY(%s)
                    OR EXISTS (
                        SELECT 1 FROM analytics.snowflake_visit_kpi sf
                        WHERE sf.emr_id = wi.emr_patient_id
                          AND sf.date_of_service = wi.dos
                          AND lower(btrim(COALESCE(
                            NULLIF(btrim(sf.payload->>'COLLECTION_STATUS'), ''),
                            NULLIF(btrim(sf.payload->>'collection_status'), ''),
                            ''
                          ))) = ANY(%s)
                    )
                )"""
            )
            params.extend([folded, folded])
        if want_blank:
            parts.append(
                """(
                    lower(btrim(COALESCE(
                        NULLIF(btrim(wi.manual_overrides->>'collection_status'), ''),
                        NULLIF(btrim(wi.context->>'collection_status'), ''),
                        ''
                    ))) = ''
                    AND NOT EXISTS (
                        SELECT 1 FROM analytics.snowflake_visit_kpi sf
                        WHERE sf.emr_id = wi.emr_patient_id
                          AND sf.date_of_service = wi.dos
                          AND lower(btrim(COALESCE(
                            NULLIF(btrim(sf.payload->>'COLLECTION_STATUS'), ''),
                            NULLIF(btrim(sf.payload->>'collection_status'), ''),
                            ''
                          ))) <> ''
                    )
                )"""
            )
        if parts:
            clauses.append(_or_clauses(parts))
    if root_cause:
        names, want_blank = _split_blank(root_cause)
        parts = []
        if names:
            folded = [name.lower() for name in names]
            parts.append(
                """(
                    lower(btrim(COALESCE(
                        NULLIF(btrim(wi.manual_overrides->>'root_cause'), ''),
                        NULLIF(btrim(wi.context->>'root_cause'), ''),
                        ''
                    ))) = ANY(%s)
                    OR EXISTS (
                        SELECT 1 FROM analytics.snowflake_visit_kpi sf
                        WHERE sf.emr_id = wi.emr_patient_id
                          AND sf.date_of_service = wi.dos
                          AND lower(btrim(COALESCE(
                            NULLIF(btrim(sf.payload->>'ROOTCAUSE'), ''),
                            NULLIF(btrim(sf.payload->>'ROOT_CAUSE'), ''),
                            NULLIF(btrim(sf.payload->>'root_cause'), ''),
                            ''
                          ))) = ANY(%s)
                    )
                )"""
            )
            params.extend([folded, folded])
        if want_blank:
            parts.append(
                """(
                    lower(btrim(COALESCE(
                        NULLIF(btrim(wi.manual_overrides->>'root_cause'), ''),
                        NULLIF(btrim(wi.context->>'root_cause'), ''),
                        ''
                    ))) = ''
                    AND NOT EXISTS (
                        SELECT 1 FROM analytics.snowflake_visit_kpi sf
                        WHERE sf.emr_id = wi.emr_patient_id
                          AND sf.date_of_service = wi.dos
                          AND lower(btrim(COALESCE(
                            NULLIF(btrim(sf.payload->>'ROOTCAUSE'), ''),
                            NULLIF(btrim(sf.payload->>'ROOT_CAUSE'), ''),
                            NULLIF(btrim(sf.payload->>'root_cause'), ''),
                            ''
                          ))) <> ''
                    )
                )"""
            )
        if parts:
            clauses.append(_or_clauses(parts))
    if assigned_to and unassigned:
        clauses.append("(wi.assigned_to = ANY(%s::uuid[]) OR wi.assigned_to IS NULL)")
        params.append(assigned_to)
    elif assigned_to:
        clauses.append("wi.assigned_to = ANY(%s::uuid[])")
        params.append(assigned_to)
    elif unassigned:
        clauses.append("wi.assigned_to IS NULL")
    if month:
        month_parts: list[str] = []
        for m in month:
            start, end = _month_bounds(m)
            month_parts.append("(wi.dos BETWEEN %s AND %s)")
            params.extend([start, end])
        clauses.append("(" + " OR ".join(month_parts) + ")")
    if check_date:
        dates = _parse_check_dates(check_date)
        if dates:
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM billing.reconciliation_visit_agg rv
                    WHERE rv.reconciliation_run_id = (
                        SELECT reconciliation_run_id
                        FROM billing.reconciliation_run
                        WHERE status = 'success'
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                      AND rv.webpt_patient_id = wi.emr_patient_id
                      AND rv.date_of_service = wi.dos
                      AND rv.primary_check_date = ANY(%s::date[])
                )"""
            )
            params.append(dates)
        else:
            clauses.append("FALSE")
    if _is_collection_queue(queue):
        key = _collection_bucket(bucket)
        member_bucket = "action" if key == "follow_up" else key
        clauses.append(COLLECTION_QUEUE_MEMBER_SQL)
        params.append(member_bucket)
        if key == "follow_up":
            clauses.append(ACTION_IS_FOLLOW_UP_SQL)
        elif key == "action":
            clauses.append(f"NOT ({ACTION_IS_FOLLOW_UP_SQL})")
    elif _is_pr3_queue(queue):
        clauses.append(PR3_UNPAID_SQL)
        clauses.append(f"NOT {SKIPPED_VISIT_SQL}")
    else:
        clauses.append(f"NOT {SKIPPED_VISIT_SQL}")
        clauses.append(f"NOT {DENIED_VISIT_SQL}")
        clauses.append(f"NOT {COLLECTION_VISIT_SQL}")
    return " AND ".join(clauses), params


def _sort_direction(sort_dir: str | None) -> str:
    return "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"


_PAID_SORT_SQL = """
COALESCE(
    NULLIF(wi.manual_overrides->>'insurance_payment', '')::numeric,
    NULLIF(wi.manual_overrides->>'paid_amount', '')::numeric,
    rv.total_paid,
    NULLIF(wi.context->>'total_paid', '')::numeric
)
"""

_SHEET_PAID_AMT_SQL = f"COALESCE({_PAID_SORT_SQL}, 0)"

SHEET_PAID_VISIT_EXISTS_SQL = """
NOT EXISTS (
    SELECT 1
    FROM ops.eligibility_work_item wi
    WHERE wi.emr_patient_id = f.webpt_patient_id
      AND wi.dos = f.dos
      AND lower(btrim(COALESCE(wi.source_visit_status, ''))) = 'paid'
)
"""

_CHECK_NUM_SORT_SQL = """
COALESCE(
    NULLIF(btrim(wi.manual_overrides->>'check_number'), ''),
    NULLIF(btrim(rv.primary_check_number), ''),
    NULLIF(btrim(wi.context->>'primary_check_number'), ''),
    NULLIF(btrim(wi.reference_number), '')
)
"""

_CHECK_DATE_SORT_SQL = """
COALESCE(
    NULLIF(wi.manual_overrides->>'check_date', '')::date,
    rv.primary_check_date,
    NULLIF(wi.context->>'primary_check_date', '')::date
)
"""

_SORTABLE = {
    "patient_name": "wi.patient_name",
    "emr_patient_id": "wi.emr_patient_id",
    "dos": "wi.dos",
    "dob": "wi.dob",
    "facility_name": "COALESCE(f.name, wi.facility_name)",
    "insurance_name": "wi.insurance_name",
    "eligibility_status": "wi.eligibility_status",
    "source_visit_status": "wi.source_visit_status",
    "reference_number": "wi.reference_number",
    "notes": "wi.notes",
    "assigned_to": "COALESCE(au.collector_code, au.display_name)",
    "updated_at": "wi.updated_at",
    "updated_by": "uu.display_name",
    "paid_amount": _PAID_SORT_SQL,
    "insurance_payment": _PAID_SORT_SQL,
    "check_number": _CHECK_NUM_SORT_SQL,
    "insurance_check_number": _CHECK_NUM_SORT_SQL,
    "check_date": _CHECK_DATE_SORT_SQL,
    "insurance_check_date": _CHECK_DATE_SORT_SQL,
    "tracker_date": "trk.txn_date",
    "work_date": "NULLIF(wi.manual_overrides->>'work_date', '')",
    "collection_status": "COALESCE(wi.manual_overrides->>'collection_status', '')",
    "account_number": ACCOUNT_NUMBER_SQL,
}

_RECON_SORT_KEYS = frozenset(
    {
        "paid_amount",
        "insurance_payment",
        "check_number",
        "insurance_check_number",
        "check_date",
        "insurance_check_date",
        "tracker_date",
    }
)


def _compact_sql(expr: str) -> str:
    return _COMPACT_REF_SQL.replace("coalesce(ref, '')", f"coalesce(({expr})::text, '')")


_PR_SORTABLE = {
    "patient_name": "base.patient_name",
    "emr_patient_id": "base.emr_patient_id",
    "account_number": "base.account_number",
    "dos": "base.dos",
    "primary_payer": "base.primary_payer",
    "facility_name": "base.facility_name",
}

_WORK_ITEM_SELECT = f"""
            wi.work_item_id,
            COALESCE(f.name, wi.facility_name) AS facility_name,
            wi.emr_patient_id, wi.dos,
            {ACCOUNT_NUMBER_SQL} AS account_number,
            wi.patient_name, wi.dob, wi.insurance_name, wi.source_visit_status,
            wi.eligibility_status, wi.reference_number, wi.notes,
            wi.context, wi.manual_overrides,
            wi.assigned_to, wi.assigned_at, wi.completed_at,
            wi.locked_by, wi.locked_at, wi.lock_expires_at,
            wi.updated_by, wi.updated_at, wi.created_at, wi.priority,
            au.display_name AS assigned_to_name,
            au.collector_code AS assigned_to_code,
            uu.display_name AS updated_by_name,
            lu.display_name AS locked_by_name
"""

_EXPORT_BATCH = 2000


def _finish_work_item_rows(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    *,
    tracker_dates: dict[str, str] | None = None,
    eft_totals: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Apply the same overlays the sheet list uses, then drop raw context."""
    overlay_live_sf(conn, rows)
    overlay_live_recon(conn, rows)
    overlay_pr1_reductions(conn, rows)
    overlay_rtm_amounts(conn, rows)
    overlay_oa23_amounts(conn, rows)
    overlay_denial_reasons(conn, rows)
    for row in rows:
        attach_sheet_fields(row)
    overlay_tracker_dates(conn, rows, index=tracker_dates)
    overlay_eft_totals(conn, rows, index=eft_totals)
    overlay_ledger_totals(conn, rows)
    items: list[dict[str, Any]] = []
    for row in rows:
        apply_manual_overrides(row)
        row.pop("context", None)
        items.append(_drop_overlay_keys(row))
    return items


def list_work_items(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    insurance: list[str] | None = None,
    status: list[str] | None = None,
    visit_status: list[str] | None = None,
    check_date: list[str] | None = None,
    assigned_to: list[str] | None = None,
    unassigned: bool = False,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    queue: str | None = None,
    bucket: str | None = None,
    collection_status: list[str] | None = None,
    root_cause: list[str] | None = None,
) -> dict[str, Any]:
    where, params = _build_filters(
        q=q,
        facility=facility,
        month=month,
        insurance=insurance,
        status=status,
        visit_status=visit_status,
        check_date=check_date,
        assigned_to=assigned_to,
        unassigned=unassigned,
        queue=queue,
        bucket=bucket,
        collection_status=collection_status,
        root_cause=root_cause,
    )
    sort_key = (sort_by or "dos").strip()
    sort_col = _SORTABLE.get(sort_key, "wi.dos")
    direction = _sort_direction(sort_dir)
    page = max(1, page)
    page_size = min(max(1, page_size), 2000)
    offset = (page - 1) * page_size

    recon_join = ""
    tracker_join = ""
    if sort_key in _RECON_SORT_KEYS:
        recon_join = """
            LEFT JOIN LATERAL (
                SELECT rv.total_paid, rv.primary_check_number, rv.primary_check_date
                FROM billing.reconciliation_visit_agg rv
                WHERE rv.reconciliation_run_id = (
                    SELECT reconciliation_run_id
                    FROM billing.reconciliation_run
                    WHERE status = 'success'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                  AND rv.webpt_patient_id = wi.emr_patient_id
                  AND rv.date_of_service = wi.dos
                ORDER BY CASE
                    WHEN lower(btrim(COALESCE(rv.facility_name, '')))
                       = lower(btrim(COALESCE(f.name, wi.facility_name, ''))) THEN 0
                    ELSE 1
                END
                LIMIT 1
            ) rv ON true
        """
    if sort_key == "tracker_date":
        tracker_join = f"""
            LEFT JOIN LATERAL (
                SELECT min(src.txn_date) AS txn_date
                FROM billing.transaction_tracker_row src
                CROSS JOIN LATERAL unnest(ARRAY[src.eft_1, src.eft_2, src.check_reference]) AS t(ref)
                WHERE src.deleted_at IS NULL
                  AND t.ref IS NOT NULL AND btrim(t.ref) <> ''
                  AND {_compact_sql("t.ref")} = {_compact_sql(_CHECK_NUM_SORT_SQL)}
            ) trk ON true
        """

    count_row = client.fetchone(
        conn,
        f"SELECT count(*)::int AS n FROM ops.eligibility_work_item wi WHERE {where}",
        params,
    )
    total = int(count_row["n"]) if count_row else 0

    rows = client.fetchall(
        conn,
        f"""
        SELECT
            {_WORK_ITEM_SELECT}
        FROM ops.eligibility_work_item wi
        LEFT JOIN ref.facility f ON f.webpt_facility_id = wi.facility_name
        LEFT JOIN auth.app_user au ON au.user_id = wi.assigned_to
        LEFT JOIN auth.app_user uu ON uu.user_id = wi.updated_by
        LEFT JOIN auth.app_user lu ON lu.user_id = wi.locked_by
        {recon_join}
        {tracker_join}
        WHERE {where}
        ORDER BY {sort_col} {direction} NULLS LAST, wi.work_item_id
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    items = _finish_work_item_rows(conn, rows)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 0,
    }


def iter_export_work_items(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    insurance: list[str] | None = None,
    status: list[str] | None = None,
    visit_status: list[str] | None = None,
    check_date: list[str] | None = None,
    assigned_to: list[str] | None = None,
    unassigned: bool = False,
    queue: str | None = None,
    bucket: str | None = None,
    collection_status: list[str] | None = None,
    root_cause: list[str] | None = None,
    batch_size: int = _EXPORT_BATCH,
) -> Iterator[dict[str, Any]]:
    """Yield sheet rows for Excel export.

    Walks ``work_item_id`` with a keyset so an all-months export does not
    recount the table or re-scan earlier pages with OFFSET.
    """
    where, params = _build_filters(
        q=q,
        facility=facility,
        month=month,
        insurance=insurance,
        status=status,
        visit_status=visit_status,
        check_date=check_date,
        assigned_to=assigned_to,
        unassigned=unassigned,
        queue=queue,
        bucket=bucket,
        collection_status=collection_status,
        root_cause=root_cause,
    )
    batch_size = min(max(1, batch_size), _EXPORT_BATCH)
    tracker_dates = load_export_tracker_dates(conn)
    eft_totals = load_export_eft_totals(conn)
    after_id: Any = None
    while True:
        rows = client.fetchall(
            conn,
            f"""
            SELECT
                {_WORK_ITEM_SELECT}
            FROM ops.eligibility_work_item wi
            LEFT JOIN ref.facility f ON f.webpt_facility_id = wi.facility_name
            LEFT JOIN auth.app_user au ON au.user_id = wi.assigned_to
            LEFT JOIN auth.app_user uu ON uu.user_id = wi.updated_by
            LEFT JOIN auth.app_user lu ON lu.user_id = wi.locked_by
            WHERE {where}
              AND (%s::uuid IS NULL OR wi.work_item_id > %s::uuid)
            ORDER BY wi.work_item_id
            LIMIT %s
            """,
            [*params, after_id, after_id, batch_size],
        )
        if not rows:
            break
        yield from _finish_work_item_rows(
            conn,
            rows,
            tracker_dates=tracker_dates,
            eft_totals=eft_totals,
        )
        if len(rows) < batch_size:
            break
        after_id = rows[-1]["work_item_id"]


# Matches PR-2 / PR2 exactly inside ';'-joined pr_oa_codes; never PR-26 / PR-27.
PR2_SQL_PATTERN = "(^|;)PR-?2(;|$)"
# Matches PR-1 / PR1 exactly; never PR-10 / PR-11 / PR-16 / PR-18.
PR1_SQL_PATTERN = "(^|;)PR-?1(;|$)"
# Matches PR-3 / PR3 exactly; never PR-30 / PR-31.
PR3_SQL_PATTERN = "(^|;)PR-?3(;|$)"
# Matches PR-100 / PR100 exactly; never a longer code that only starts with 100.
PR100_SQL_PATTERN = "(^|;)PR-?100(;|$)"

def _pr3_token_sql(expr: str) -> str:
    """Same PR-3 token match on semicolon lists and on raw check/detail text."""
    normalized = (
        f"regexp_replace(COALESCE({expr}, ''), '[[:space:],]+', ';', 'g')"
    )
    return f"{normalized} ~* '{PR3_SQL_PATTERN}'"


# Detail-line codes (pr_oa_codes, carcs) or the check CARC row.
PR3_CODE_SQL = f"""(
    EXISTS (
        SELECT 1
        FROM billing.waystar_webpt_map m
        JOIN billing.eob_line el
          ON el.revflow_patient_id = m.waystar_claim_key
         AND el.date_of_service = wi.dos
        WHERE m.webpt_patient_id = wi.emr_patient_id
          AND (
                {_pr3_token_sql("el.pr_oa_codes")}
             OR {_pr3_token_sql("el.carcs")}
          )
    )
    OR EXISTS (
        SELECT 1
        FROM billing.waystar_webpt_map m
        JOIN billing.eob_carc_raw cr
          ON cr.revflow_patient_id = m.waystar_claim_key
         AND cr.date_of_service = wi.dos
        WHERE m.webpt_patient_id = wi.emr_patient_id
          AND {_pr3_token_sql("cr.carc_code")}
    )
)"""

PR3_UNPAID_SQL = f"""(
    lower(btrim(COALESCE(wi.source_visit_status, ''))) NOT IN ('paid', 'partial')
    AND {PR3_CODE_SQL}
)"""

PR3_PAID_COLLECTION_SQL = f"""(
    {PR3_CODE_SQL}
    AND {EFFECTIVE_COLLECTION_FOLD_SQL} = 'paid'
)"""


def collection_bucket_predicate(bucket: str | None) -> str:
    """Live membership rules stored by refresh_collection_queue()."""
    key = _collection_bucket(bucket)
    if key == "overdue":
        return f"{OVERDUE_PENDING_SQL} AND NOT {PR3_UNPAID_SQL}"
    if key == "collection":
        return f"{COLLECTION_VISIT_SQL} AND NOT {PR3_UNPAID_SQL}"
    if key == "paid_patient_responsibility":
        return (
            f"{PR3_CODE_SQL} AND {EFFECTIVE_COLLECTION_FOLD_SQL} = 'paid' "
            f"AND NOT {PAID_OR_DEDUCT_SQL}"
        )
    return (
        f"(({DENIED_VISIT_SQL}) OR ({PR3_UNPAID_SQL})) "
        f"AND NOT ({PR3_PAID_COLLECTION_SQL})"
    )


def work_item_has_pr3(conn: psycopg.Connection, work_item_id: str) -> bool:
    row = client.fetchone(
        conn,
        f"""
        SELECT 1 AS ok
        FROM ops.eligibility_work_item wi
        WHERE wi.work_item_id = %s::uuid
          AND {PR3_CODE_SQL}
        LIMIT 1
        """,
        (work_item_id,),
    )
    return row is not None


def waystar_paid_exit_sql() -> str:
    """Promote denied and overdue visits that Waystar has paid with a real check."""
    scope = f"""
        {ELIGIBILITY_MIN_DOS_SQL}
        AND {ELIGIBILITY_MAX_DOS_SQL}
        AND {KEEP_WORK_ITEM_SQL}
    """
    return f"""
        WITH candidates AS (
            SELECT
                wi.work_item_id,
                CASE
                    WHEN ({DENIED_VISIT_SQL}) THEN 'denied'
                    ELSE 'overdue'
                END AS exited_from
            FROM ops.eligibility_work_item wi
            WHERE {scope}
              AND NOT ({ROUTED_COLLECTION_SQL})
              AND {WAYSTAR_PAID_CHECK_SQL}
              AND (
                    ({DENIED_VISIT_SQL})
                 OR (({OVERDUE_PENDING_SQL}) AND NOT ({PR3_UNPAID_SQL}))
              )
        ),
        updated AS (
            UPDATE ops.eligibility_work_item wi
            SET
                source_visit_status = 'paid',
                manual_overrides = CASE
                    WHEN lower(btrim(COALESCE(
                        wi.manual_overrides->>'source_visit_status', ''
                    ))) = 'denied'
                        THEN COALESCE(wi.manual_overrides, '{{}}'::jsonb)
                             - 'source_visit_status'
                    ELSE wi.manual_overrides
                END,
                context = COALESCE(wi.context, '{{}}'::jsonb) || jsonb_build_object(
                    'exited_from', c.exited_from,
                    'exited_from_at', to_char(CURRENT_DATE, 'YYYY-MM-DD')
                ),
                updated_at = now()
            FROM candidates c
            WHERE wi.work_item_id = c.work_item_id
            RETURNING wi.work_item_id, c.exited_from
        )
        INSERT INTO ops.eligibility_history (
            work_item_id, column_name, old_value, new_value, reason_text
        )
        SELECT
            work_item_id,
            'source_visit_status',
            exited_from,
            'paid',
            '{WAYSTAR_COLLECTION_EXIT_REASON}'
        FROM updated
        """


def _promote_waystar_paid_collection(conn: psycopg.Connection) -> int:
    """Set denied and overdue visits to paid once Waystar has money and a check.

    Arbitration, action, and at-risk rows stay put. A PR-3 visit stays put unless
    it is actually denied or overdue. History and context.exited_from keep the
    bucket the visit left.
    """
    cur = conn.execute(waystar_paid_exit_sql())
    return cur.rowcount


def refresh_collection_queue(conn: psycopg.Connection) -> dict[str, int]:
    """Rebuild Collection tab membership off the request path."""
    promoted = _promote_waystar_paid_collection(conn)
    client.execute(conn, "DELETE FROM analytics.collection_queue_member")
    client.execute(
        conn,
        """
        CREATE TEMP TABLE tmp_waystar_zero ON COMMIT DROP AS
        SELECT m.webpt_patient_id AS emr, c.from_date AS dos
        FROM billing.waystar_webpt_map m
        JOIN billing.waystar_claim c
          ON c.claim_key = m.waystar_claim_key
        WHERE NULLIF(btrim(m.webpt_patient_id), '') IS NOT NULL
          AND c.from_date IS NOT NULL
        GROUP BY 1, 2
        HAVING bool_or(COALESCE(c.total_remit_amount, 0) = 0)
           AND NOT bool_or(COALESCE(c.total_remit_amount, 0) <> 0)
        """,
    )
    client.execute(conn, "CREATE INDEX ON tmp_waystar_zero (emr, dos)")
    client.execute(
        conn,
        """
        CREATE TEMP TABLE tmp_waystar_details ON COMMIT DROP AS
        SELECT DISTINCT m.webpt_patient_id AS emr, el.date_of_service AS dos
        FROM billing.waystar_webpt_map m
        JOIN billing.eob_line el
          ON el.revflow_patient_id = m.waystar_claim_key
        WHERE NULLIF(btrim(m.webpt_patient_id), '') IS NOT NULL
          AND el.date_of_service IS NOT NULL
          AND NULLIF(btrim(el.carcs), '') IS NOT NULL
        """,
    )
    client.execute(conn, "CREATE INDEX ON tmp_waystar_details (emr, dos)")
    client.execute(
        conn,
        """
        CREATE TEMP TABLE tmp_waystar_past_sla ON COMMIT DROP AS
        SELECT DISTINCT emr, dos
        FROM (
            SELECT fp.webpt_patient_id AS emr, fp.date_of_service AS dos
            FROM analytics.forecast_prediction fp
            WHERE fp.forecast_run_id = (
                SELECT forecast_run_id
                FROM analytics.forecast_run
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
            )
              AND fp.expected_pay_date + 3 < CURRENT_DATE
              AND fp.webpt_patient_id IS NOT NULL
              AND fp.date_of_service IS NOT NULL
            UNION
            SELECT
                NULLIF(BTRIM(fp.payload->>'webpt_patient_id'), '') AS emr,
                COALESCE(
                    fp.date_of_service,
                    CASE
                        WHEN fp.payload->>'date_of_service' ~ '^\\d{4}-\\d{2}-\\d{2}'
                            THEN substring(fp.payload->>'date_of_service' from 1 for 10)::date
                        ELSE NULL
                    END
                ) AS dos
            FROM analytics.forecast_prediction fp
            WHERE fp.forecast_run_id = (
                SELECT forecast_run_id
                FROM analytics.forecast_run
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
            )
              AND fp.expected_pay_date + 3 < CURRENT_DATE
              AND fp.webpt_patient_id IS NULL
        ) s
        WHERE emr IS NOT NULL AND dos IS NOT NULL
        """,
    )
    client.execute(conn, "CREATE INDEX ON tmp_waystar_past_sla (emr, dos)")
    base_scope = f"""
        {ELIGIBILITY_MIN_DOS_SQL}
        AND {ELIGIBILITY_MAX_DOS_SQL}
        AND {KEEP_WORK_ITEM_SQL}
    """
    client.execute(
        conn,
        f"""
        UPDATE ops.eligibility_work_item wi
        SET source_visit_status = 'patient_responsibility'
        WHERE {base_scope}
          AND NULLIF(btrim(wi.manual_overrides->>'source_visit_status'), '') IS NULL
          AND lower(btrim(COALESCE(wi.source_visit_status, ''))) NOT IN (
              'paid', 'partial', 'deduct', 'patient_responsibility'
          )
          AND {PR3_CODE_SQL}
          AND NOT {PAID_OR_DEDUCT_SQL}
        """,
    )
    rules = {
        "denied": f"""((
            (
                (
                    lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = 'denied'
                    OR (
                        EXISTS (
                            SELECT 1 FROM tmp_waystar_zero z
                            WHERE z.emr = wi.emr_patient_id AND z.dos = wi.dos
                        )
                        AND EXISTS (
                            SELECT 1 FROM tmp_waystar_details d
                            WHERE d.emr = wi.emr_patient_id AND d.dos = wi.dos
                        )
                    )
                ) AND NOT {PAID_OR_DEDUCT_SQL}
                ) OR {COLLECTION_VISIT_SQL}
                OR ({PR3_UNPAID_SQL})
            ) AND NOT ({ROUTED_COLLECTION_SQL})
              AND NOT ({PR3_PAID_COLLECTION_SQL})
        )""",
        "overdue": f"""(
            EXISTS (
                SELECT 1 FROM tmp_waystar_past_sla s
                WHERE s.emr = wi.emr_patient_id AND s.dos = wi.dos
            )
            AND NOT {SKIPPED_VISIT_SQL}
            AND NOT (
                (
                    lower(btrim(COALESCE({EFFECTIVE_VISIT_STATUS_SQL}, ''))) = 'denied'
                    OR (
                        EXISTS (
                            SELECT 1 FROM tmp_waystar_zero z
                            WHERE z.emr = wi.emr_patient_id AND z.dos = wi.dos
                        )
                        AND EXISTS (
                            SELECT 1 FROM tmp_waystar_details d
                            WHERE d.emr = wi.emr_patient_id AND d.dos = wi.dos
                        )
                    )
                )
                AND NOT {PAID_OR_DEDUCT_SQL}
            )
            AND NOT {COLLECTION_VISIT_SQL}
            AND NOT {PAID_OR_DEDUCT_SQL}
            AND NOT ({ROUTED_COLLECTION_SQL})
        )""",
        "arbitration": f"{EFFECTIVE_COLLECTION_FOLD_SQL} = 'arbitration'",
        "action": f"{EFFECTIVE_COLLECTION_FOLD_SQL} IN ('actiontaken', 'pending')",
        "at_risk": f"{EFFECTIVE_COLLECTION_FOLD_SQL} = 'submittedwithoutauth'",
        "paid_patient_responsibility": f"""(
            {PR3_CODE_SQL}
            AND {EFFECTIVE_COLLECTION_FOLD_SQL} = 'paid'
            AND NOT {PAID_OR_DEDUCT_SQL}
        )""",
    }
    counts: dict[str, int] = {}
    for bucket, rule in rules.items():
        scope = base_scope
        if bucket == "overdue":
            scope = f"{base_scope} AND NOT {PR3_UNPAID_SQL}"
        cur = conn.execute(
            f"""
            INSERT INTO analytics.collection_queue_member (bucket, work_item_id)
            SELECT %s, wi.work_item_id
            FROM ops.eligibility_work_item wi
            WHERE {scope}
              AND {rule}
            """,
            (bucket,),
        )
        counts[bucket] = cur.rowcount
    counts["promoted_paid"] = promoted
    return counts

COLLECTION_DENIED_CHARGED_SQL = """COALESCE(
    (
        SELECT sf.charged_amount
        FROM analytics.snowflake_visit_kpi sf
        WHERE sf.emr_id = wi.emr_patient_id
          AND sf.date_of_service = wi.dos
          AND sf.charged_amount IS NOT NULL
        ORDER BY sf.charged_amount DESC NULLS LAST
        LIMIT 1
    ),
    CASE
        WHEN NULLIF(BTRIM(COALESCE(wi.context->>'charged_amount', '')), '')
             ~ '^-?[0-9]+(\\.[0-9]+)?$'
            THEN NULLIF(BTRIM(wi.context->>'charged_amount'), '')::numeric
        ELSE 0
    END,
    0
)"""


def _collection_denied_where(
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Same membership as Collection → Denied, including unpaid PR-3."""
    clauses = [
        f"(({DENIED_VISIT_SQL}) OR ({PR3_UNPAID_SQL}))",
        f"NOT ({PR3_PAID_COLLECTION_SQL})",
    ]
    params: list[Any] = []
    if d0:
        clauses.append("wi.dos >= %s")
        params.append(d0)
    if d1:
        clauses.append("wi.dos <= %s")
        params.append(d1)
    if facilities:
        clauses.append("wi.facility_name = ANY(%s)")
        params.append(facilities)
    if insurers:
        clauses.append("wi.insurance_name = ANY(%s)")
        params.append(insurers)
    return " AND ".join(clauses), params


def collection_denied_visit_sql(where: str) -> str:
    return f"""
        SELECT DISTINCT COALESCE(wi.emr_patient_id, ''), wi.dos
        FROM ops.eligibility_work_item wi
        WHERE {where}
          AND ({COLLECTION_DENIED_CHARGED_SQL}) > 0
    """


def collection_denied_exposure(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
) -> dict[str, Any]:
    """Charged $ for Collection Denied visits (Snowflake charged_amount)."""
    from cashflow_db.repository.insurance import usable_sql

    where, params = _collection_denied_where(
        d0=d0, d1=d1, facilities=facilities, insurers=insurers
    )
    ins_sql = usable_sql("wi.insurance_name")
    totals = client.fetchone(
        conn,
        f"""
        SELECT
            COALESCE(SUM(charged), 0) AS exposure_amount,
            COUNT(DISTINCT (emr, dos))
                FILTER (WHERE charged > 0)::int AS visit_count
        FROM (
            SELECT
                COALESCE(wi.emr_patient_id, '') AS emr,
                wi.dos,
                ({COLLECTION_DENIED_CHARGED_SQL}) AS charged
            FROM ops.eligibility_work_item wi
            WHERE {where}
        ) denied
        """,
        params,
    )
    by_insurance = client.fetchall(
        conn,
        f"""
        SELECT
            COALESCE({ins_sql}, '(blank)') AS ins_name,
            COALESCE(SUM(charged), 0) AS exposure_amount,
            COUNT(DISTINCT (emr, dos))
                FILTER (WHERE charged > 0)::int AS visit_count
        FROM (
            SELECT
                COALESCE(wi.emr_patient_id, '') AS emr,
                wi.dos,
                wi.insurance_name,
                ({COLLECTION_DENIED_CHARGED_SQL}) AS charged
            FROM ops.eligibility_work_item wi
            WHERE {where}
        ) denied
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    )
    return {
        "exposure_amount": round(float((totals or {}).get("exposure_amount") or 0), 2),
        "visit_count": int((totals or {}).get("visit_count") or 0),
        "by_insurance": list(by_insurance or []),
        "where_sql": where,
        "params": params,
    }


PR_FLAG_KINDS = frozenset({"pr1", "pr2"})


def _pr_queue_snapshot_select_sql(pr_regex: str) -> str:
    """Identity SQL for one PR CARC. from_date = EOB DOS; never invent Waystar DOS.

    Paid Amount is co-insurance: a smaller other-payer EOB check whose number
    is on the Transaction Tracker. Same-payer extra checks stay unpaid.

    Primary payer is the visit's Eligibility insurance, then the largest paid
    check's payor, then the PR-line payor aggregate.
    """
    tracker_eft1 = _compact_sql("t.eft_1")
    tracker_eft2 = _compact_sql("t.eft_2")
    tracker_ref = _compact_sql("t.check_reference")
    coins_ref = _compact_sql("c.check_eft_num")
    return f"""
        WITH pr AS (
            SELECT
                el.revflow_patient_id,
                regexp_replace(el.revflow_patient_id, '^PV4', '', 'i') AS account_key,
                el.date_of_service AS dos,
                max(el.patient_id::text)::uuid AS patient_uuid,
                string_agg(DISTINCT ec.payor_raw, '; ') AS primary_payer,
                string_agg(DISTINCT NULLIF(btrim(ec.check_eft_num), ''), '; ') AS primary_check_number
            FROM billing.eob_line el
            JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
            WHERE el.pr_oa_codes ~* '{pr_regex}'
              AND el.revflow_patient_id IS NOT NULL
              AND el.date_of_service IS NOT NULL
            GROUP BY el.revflow_patient_id, el.date_of_service
        ),
        check_paid AS (
            SELECT
                el2.revflow_patient_id,
                el2.date_of_service AS dos,
                el2.eob_check_id,
                sum(el2.paid_amount) AS check_paid,
                max(ec2.check_eft_num) AS check_eft_num,
                max(ec2.eob_date) AS eob_date,
                string_agg(DISTINCT ec2.payor_raw, '; ') AS payor_raw
            FROM pr p
            JOIN billing.eob_line el2
              ON el2.revflow_patient_id = p.revflow_patient_id
             AND el2.date_of_service = p.dos
            JOIN billing.eob_check ec2 ON ec2.eob_check_id = el2.eob_check_id
            WHERE COALESCE(el2.paid_amount, 0) > 0
            GROUP BY el2.revflow_patient_id, el2.date_of_service, el2.eob_check_id
        ),
        primary_check AS (
            SELECT DISTINCT ON (revflow_patient_id, dos)
                revflow_patient_id,
                dos,
                eob_check_id,
                payor_raw
            FROM check_paid
            ORDER BY revflow_patient_id, dos, check_paid DESC, eob_date ASC NULLS LAST, eob_check_id
        ),
        tracker_keys AS (
            SELECT DISTINCT compact
            FROM (
                SELECT {tracker_eft1} AS compact
                FROM billing.transaction_tracker_row t
                WHERE t.deleted_at IS NULL
                  AND NULLIF(btrim(t.eft_1), '') IS NOT NULL
                UNION
                SELECT {tracker_eft2} AS compact
                FROM billing.transaction_tracker_row t
                WHERE t.deleted_at IS NULL
                  AND NULLIF(btrim(t.eft_2), '') IS NOT NULL
                UNION
                SELECT {tracker_ref} AS compact
                FROM billing.transaction_tracker_row t
                WHERE t.deleted_at IS NULL
                  AND NULLIF(btrim(t.check_reference), '') IS NOT NULL
            ) src
            WHERE NULLIF(compact, '') IS NOT NULL
        ),
        sec AS (
            SELECT
                c.revflow_patient_id,
                c.dos,
                sum(c.check_paid) AS secondary_amount,
                max(c.check_eft_num) AS secondary_check_number,
                max(c.eob_date) AS secondary_check_date,
                string_agg(DISTINCT c.payor_raw, '; ') AS secondary_payer
            FROM check_paid c
            JOIN primary_check pc
              ON pc.revflow_patient_id = c.revflow_patient_id
             AND pc.dos = c.dos
            JOIN tracker_keys tk ON tk.compact = {coins_ref}
            WHERE c.eob_check_id <> pc.eob_check_id
              AND ops.fold_insurance_name(COALESCE(c.payor_raw, ''))
                  IS DISTINCT FROM ops.fold_insurance_name(COALESCE(pc.payor_raw, ''))
              AND NULLIF(ops.fold_insurance_name(COALESCE(c.payor_raw, '')), '') IS NOT NULL
            GROUP BY 1, 2
        ),
        ident AS (
            SELECT
                pr.revflow_patient_id,
                pr.account_key,
                pr.dos,
                pr.patient_uuid,
                pr.primary_payer,
                pr.primary_check_number,
                COALESCE(ws_key.claim_key, ws_remit.claim_key) AS claim_key,
                COALESCE(ws_key.claim_number, ws_remit.claim_number) AS claim_number,
                COALESCE(ws_key.patient_name, ws_remit.patient_name) AS waystar_patient_name
            FROM pr
            LEFT JOIN LATERAL (
                SELECT wc.claim_key, wc.claim_number, wc.patient_name
                FROM billing.waystar_claim wc
                WHERE wc.claim_key = pr.account_key
                  AND wc.from_date = pr.dos
                ORDER BY wc.sequence, wc.scraped_at DESC
                LIMIT 1
            ) ws_key ON true
            LEFT JOIN LATERAL (
                SELECT wc.claim_key, wc.claim_number, wc.patient_name
                FROM billing.waystar_claim wc
                WHERE ws_key.claim_key IS NULL
                  AND wc.from_date = pr.dos
                  AND EXISTS (
                      SELECT 1
                      FROM unnest(string_to_array(COALESCE(pr.primary_check_number, ''), ';')) AS t(num)
                      WHERE btrim(t.num) <> ''
                        AND btrim(t.num) = ANY (wc.remit_numbers)
                  )
                ORDER BY wc.sequence, wc.scraped_at DESC
                LIMIT 1
            ) ws_remit ON true
        )
        SELECT
            ident.revflow_patient_id,
            ident.dos,
            COALESCE(
                NULLIF(btrim(wi.insurance_name), ''),
                NULLIF(btrim(pc.payor_raw), ''),
                NULLIF(btrim(ident.primary_payer), '')
            ) AS primary_payer,
            ident.primary_check_number,
            COALESCE(
                NULLIF(btrim(by_emr.webpt_patient_id), ''),
                NULLIF(btrim(by_rf.webpt_patient_id), ''),
                NULLIF(btrim(wm.webpt_patient_id), '')
            ) AS emr_patient_id,
            COALESCE(
                NULLIF(btrim(ph.patient_name), ''),
                NULLIF(btrim(ident.waystar_patient_name), ''),
                NULLIF(btrim(wi.patient_name), '')
            ) AS patient_name,
            COALESCE(fac.name, NULLIF(btrim(wi.facility_name), '')) AS facility_name,
            COALESCE(
                NULLIF(btrim(ident.claim_number), ''),
                CASE
                    WHEN ident.claim_key IS NOT NULL AND btrim(ident.claim_key) <> '' THEN
                        CASE
                            WHEN ident.claim_key ILIKE 'PV4%%' THEN ident.claim_key
                            ELSE 'PV4' || ident.claim_key
                        END
                END,
                CASE
                    WHEN wm.waystar_claim_key IS NOT NULL THEN
                        CASE
                            WHEN wm.waystar_claim_key ILIKE 'PV4%%' THEN wm.waystar_claim_key
                            ELSE 'PV4' || wm.waystar_claim_key
                        END
                END
            ) AS account_number,
            sec.secondary_amount,
            sec.secondary_check_number,
            sec.secondary_check_date,
            sec.secondary_payer
        FROM ident
        LEFT JOIN billing.waystar_webpt_map wm
          ON wm.waystar_claim_key = COALESCE(ident.claim_key, ident.account_key)
        LEFT JOIN LATERAL (
            SELECT p.patient_id, p.webpt_patient_id
            FROM core.patient p
            WHERE p.revflow_patient_id = ident.revflow_patient_id
            ORDER BY (p.webpt_patient_id IS NOT NULL AND btrim(p.webpt_patient_id) <> '') DESC,
                     p.created_at
            LIMIT 1
        ) by_rf ON true
        LEFT JOIN LATERAL (
            SELECT p.patient_id, p.webpt_patient_id
            FROM core.patient p
            WHERE p.webpt_patient_id = COALESCE(
                    NULLIF(btrim(wm.webpt_patient_id), ''),
                    NULLIF(btrim(by_rf.webpt_patient_id), '')
                  )
              AND p.webpt_patient_id IS NOT NULL
              AND btrim(p.webpt_patient_id) <> ''
            ORDER BY p.created_at
            LIMIT 1
        ) by_emr ON true
        LEFT JOIN core.patient_history ph
          ON ph.patient_id = COALESCE(by_emr.patient_id, by_rf.patient_id, ident.patient_uuid)
         AND ph.is_current
        LEFT JOIN LATERAL (
            SELECT f.name
            FROM core.visit v
            JOIN ref.facility f ON f.facility_id = v.facility_id
            WHERE v.patient_id = COALESCE(by_emr.patient_id, by_rf.patient_id, ident.patient_uuid)
              AND v.service_date = ident.dos
            ORDER BY v.created_at DESC
            LIMIT 1
        ) fac ON true
        LEFT JOIN LATERAL (
            SELECT ewi.patient_name, ewi.facility_name, ewi.insurance_name
            FROM ops.eligibility_work_item ewi
            WHERE ewi.emr_patient_id = COALESCE(
                    NULLIF(btrim(by_emr.webpt_patient_id), ''),
                    NULLIF(btrim(by_rf.webpt_patient_id), ''),
                    NULLIF(btrim(wm.webpt_patient_id), '')
                  )
              AND ewi.dos = ident.dos
            ORDER BY
                CASE
                    WHEN lower(btrim(COALESCE(ewi.facility_name, '')))
                       = lower(btrim(COALESCE(fac.name, ''))) THEN 0
                    ELSE 1
                END,
                CASE
                    WHEN NULLIF(btrim(ewi.insurance_name), '') IS NULL THEN 1
                    ELSE 0
                END,
                ewi.updated_at DESC
            LIMIT 1
        ) wi ON true
        LEFT JOIN primary_check pc
          ON pc.revflow_patient_id = ident.revflow_patient_id
         AND pc.dos = ident.dos
        LEFT JOIN sec
          ON sec.revflow_patient_id = ident.revflow_patient_id
         AND sec.dos = ident.dos
    """


def _align_kept_primary_payer(conn: psycopg.Connection) -> None:
    """Point ptoc/manual rows at Eligibility insurance, then the largest paid check."""
    client.execute(
        conn,
        """
        UPDATE ops.pr_queue_row r
        SET primary_payer = picked.primary_payer
        FROM (
            SELECT
                r2.revflow_patient_id,
                r2.dos,
                r2.carc_kind,
                COALESCE(
                    NULLIF(btrim(elig.insurance_name), ''),
                    NULLIF(btrim(largest.payor_raw), ''),
                    NULLIF(btrim(r2.primary_payer), '')
                ) AS primary_payer
            FROM ops.pr_queue_row r2
            LEFT JOIN LATERAL (
                SELECT ewi.insurance_name
                FROM ops.eligibility_work_item ewi
                WHERE NULLIF(btrim(r2.emr_patient_id), '') IS NOT NULL
                  AND ewi.emr_patient_id = r2.emr_patient_id
                  AND ewi.dos = r2.dos
                  AND NULLIF(btrim(ewi.insurance_name), '') IS NOT NULL
                ORDER BY
                    CASE
                        WHEN lower(btrim(COALESCE(ewi.facility_name, '')))
                           = lower(btrim(COALESCE(r2.facility_name, ''))) THEN 0
                        ELSE 1
                    END,
                    ewi.updated_at DESC
                LIMIT 1
            ) elig ON true
            LEFT JOIN LATERAL (
                SELECT checks.payor_raw
                FROM (
                    SELECT
                        el.eob_check_id,
                        sum(el.paid_amount) AS check_paid,
                        max(ec.eob_date) AS eob_date,
                        string_agg(DISTINCT NULLIF(btrim(ec.payor_raw), ''), '; ') AS payor_raw
                    FROM billing.eob_line el
                    JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
                    WHERE el.revflow_patient_id = r2.revflow_patient_id
                      AND el.date_of_service = r2.dos
                      AND COALESCE(el.paid_amount, 0) > 0
                    GROUP BY el.eob_check_id
                ) checks
                ORDER BY checks.check_paid DESC, checks.eob_date ASC NULLS LAST, checks.eob_check_id
                LIMIT 1
            ) largest ON true
            WHERE r2.source IN ('ptoc', 'manual')
        ) picked
        WHERE r.revflow_patient_id = picked.revflow_patient_id
          AND r.dos = picked.dos
          AND r.carc_kind = picked.carc_kind
          AND r.primary_payer IS DISTINCT FROM picked.primary_payer
        """,
    )


def refresh_pr_queue_rows(conn: psycopg.Connection) -> dict[str, Any]:
    """Rebuild CARC snapshot rows; keep source='ptoc' / source='manual' Workload seeds."""
    client.execute(conn, "DELETE FROM ops.pr_queue_row WHERE source = 'carc'")
    for carc_kind, pattern in (
        ("pr1", PR1_SQL_PATTERN),
        ("pr2", PR2_SQL_PATTERN),
        ("pr100", PR100_SQL_PATTERN),
    ):
        pr_regex = pattern.replace("'", "''")
        client.execute(
            conn,
            f"""
            INSERT INTO ops.pr_queue_row (
                revflow_patient_id, dos, carc_kind,
                patient_name, emr_patient_id, account_number,
                primary_payer, primary_check_number, facility_name,
                secondary_amount, secondary_check_number,
                secondary_check_date, secondary_payer, refreshed_at, source
            )
            SELECT
                snap.revflow_patient_id,
                snap.dos,
                '{carc_kind}',
                snap.patient_name,
                snap.emr_patient_id,
                snap.account_number,
                snap.primary_payer,
                snap.primary_check_number,
                snap.facility_name,
                snap.secondary_amount,
                snap.secondary_check_number,
                snap.secondary_check_date,
                snap.secondary_payer,
                now(),
                'carc'
            FROM ({_pr_queue_snapshot_select_sql(pr_regex)}) snap
            ON CONFLICT (revflow_patient_id, dos, carc_kind) DO UPDATE
            SET patient_name = EXCLUDED.patient_name,
                emr_patient_id = EXCLUDED.emr_patient_id,
                account_number = EXCLUDED.account_number,
                primary_payer = EXCLUDED.primary_payer,
                primary_check_number = EXCLUDED.primary_check_number,
                facility_name = EXCLUDED.facility_name,
                secondary_amount = EXCLUDED.secondary_amount,
                secondary_check_number = EXCLUDED.secondary_check_number,
                secondary_check_date = EXCLUDED.secondary_check_date,
                secondary_payer = EXCLUDED.secondary_payer,
                refreshed_at = now(),
                source = 'carc'
            """,
        )
    _align_kept_primary_payer(conn)
    promoted = _promote_pr2_siblings_to_workload(conn)
    stamped = _stamp_paid_pr2_second_insurance(conn)
    _jump_paid_pr2_to_action_taken(conn)
    reconciled = _reconcile_paid_without_payment(conn)
    inherited = _inherit_second_insurance_defaults(conn)
    rows = client.fetchall(
        conn,
        """
        SELECT carc_kind, count(*)::int AS n
        FROM ops.pr_queue_row
        GROUP BY carc_kind
        """,
    )
    counts = {str(r["carc_kind"]): int(r["n"]) for r in rows}
    return {
        "ok": True,
        "pr1": counts.get("pr1", 0),
        "pr2": counts.get("pr2", 0),
        "pr100": counts.get("pr100", 0),
        "total": counts.get("pr1", 0) + counts.get("pr2", 0) + counts.get("pr100", 0),
        "inherited": inherited,
        "promoted": promoted,
        "stamped_insurance": stamped,
        "reconciled_filled": reconciled["filled"],
        "reconciled_cleared": reconciled["cleared"],
    }


_PATIENT_KEY_SQL = """
CASE
    WHEN NULLIF(btrim({alias}.emr_patient_id), '') IS NOT NULL
    THEN 'emr:' || btrim({alias}.emr_patient_id)
    ELSE 'rf:' || {alias}.revflow_patient_id
END
"""


def _promote_pr2_siblings_to_workload(conn: psycopg.Connection) -> int:
    """Move PR-2 rows that already share a Workload sibling's insurance."""
    src_key = _PATIENT_KEY_SQL.format(alias="wr")
    dst_key = _PATIENT_KEY_SQL.format(alias="r")
    row = client.fetchone(
        conn,
        f"""
        WITH moved AS (
            UPDATE ops.pr_queue_flag f
            SET second_submission = true,
                updated_at = now()
            FROM ops.pr_queue_row r
            WHERE f.revflow_patient_id = r.revflow_patient_id
              AND f.dos = r.dos
              AND f.carc_kind = r.carc_kind
              AND r.carc_kind = 'pr2'
              AND COALESCE(f.second_submission, false) = false
              AND NULLIF(btrim(f.second_insurance), '') IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM ops.pr_queue_flag w
                JOIN ops.pr_queue_row wr
                  ON wr.revflow_patient_id = w.revflow_patient_id
                 AND wr.dos = w.dos
                 AND wr.carc_kind = w.carc_kind
                WHERE COALESCE(w.second_submission, false)
                  AND NULLIF(btrim(w.second_insurance), '') IS NOT NULL
                  AND ops.fold_insurance_name(w.second_insurance)
                    = ops.fold_insurance_name(f.second_insurance)
                  AND ({src_key}) = ({dst_key})
              )
            RETURNING f.revflow_patient_id
        )
        SELECT count(*)::int AS n FROM moved
        """,
    )
    return int(row["n"]) if row else 0


def _stamp_paid_pr2_second_insurance(conn: psycopg.Connection) -> int:
    """Fill empty Second Insurance on paid PR-2 from the EOB coins payor.

    Do not overwrite a name a human already set. Jump handles Action Taken.
    """
    row = client.fetchone(
        conn,
        """
        WITH stamped AS (
            INSERT INTO ops.pr_queue_flag (
                revflow_patient_id, dos, carc_kind, second_insurance, updated_at
            )
            SELECT
                r.revflow_patient_id,
                r.dos,
                r.carc_kind,
                btrim(r.secondary_payer),
                now()
            FROM ops.pr_queue_row r
            WHERE r.carc_kind = 'pr2'
              AND r.secondary_amount IS NOT NULL
              AND r.secondary_amount <> 0
              AND NULLIF(btrim(r.secondary_payer), '') IS NOT NULL
              AND ops.fold_insurance_name(r.secondary_payer)
                <> ops.fold_insurance_name('No Secondary Payer')
            ON CONFLICT (revflow_patient_id, dos, carc_kind) DO UPDATE
            SET second_insurance = EXCLUDED.second_insurance,
                updated_at = now()
            WHERE NULLIF(btrim(ops.pr_queue_flag.second_insurance), '') IS NULL
            RETURNING 1
        )
        SELECT count(*)::int AS n FROM stamped
        """,
    )
    return int(row["n"]) if row else 0


def _jump_paid_pr2_to_action_taken(
    conn: psycopg.Connection,
    actor_name: str | None = None,
) -> int:
    """Paid PR-2 with Second Insurance and no Status → Action Taken.

    Keep an existing human submitter/date. System only fills blanks.
    """
    _ = actor_name
    row = client.fetchone(
        conn,
        """
        WITH jumped AS (
            UPDATE ops.pr_queue_flag f
            SET second_submission = true,
                workload_status = 'paid',
                payment = COALESCE(
                    NULLIF(btrim(f.payment), ''),
                    trim(to_char(r.secondary_amount, 'FM9999999990.00'))
                ),
                submission_date = COALESCE(f.submission_date, CURRENT_DATE),
                submitter = COALESCE(NULLIF(btrim(f.submitter), ''), %s),
                updated_at = now()
            FROM ops.pr_queue_row r
            WHERE f.revflow_patient_id = r.revflow_patient_id
              AND f.dos = r.dos
              AND f.carc_kind = r.carc_kind
              AND r.carc_kind = 'pr2'
              AND NULLIF(btrim(f.second_insurance), '') IS NOT NULL
              AND ops.fold_insurance_name(f.second_insurance)
                <> ops.fold_insurance_name('No Secondary Payer')
              AND r.secondary_amount IS NOT NULL
              AND r.secondary_amount <> 0
              AND NULLIF(btrim(f.workload_status), '') IS NULL
            RETURNING f.revflow_patient_id
        )
        SELECT count(*)::int AS n FROM jumped
        """,
        (SYSTEM_SUBMITTER,),
    )
    return int(row["n"]) if row else 0


_PAID_PAYMENT_EMPTY_SQL = """
(
    NULLIF(btrim(COALESCE(f.payment, '')), '') IS NULL
    OR (
        replace(replace(btrim(f.payment), '$', ''), ',', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
        AND replace(replace(btrim(f.payment), '$', ''), ',', '')::numeric = 0
    )
)
"""

_ACTION_TAKEN_FLAG_SQL = """
COALESCE(f.second_submission, false)
AND NULLIF(btrim(f.second_insurance), '') IS NOT NULL
"""


def _reconcile_paid_without_payment(conn: psycopg.Connection) -> dict[str, int]:
    """Action Taken paid with no payment: fill coins, else clear status.

    Keep submitter and submission_date. Do not use primary EOB as payment.
    Fill and clear target disjoint rows so the same visit is not updated twice.
    """
    row = client.fetchone(
        conn,
        f"""
        WITH filled AS (
            UPDATE ops.pr_queue_flag f
            SET payment = trim(to_char(r.secondary_amount, 'FM9999999990.00')),
                updated_at = now()
            FROM ops.pr_queue_row r
            WHERE f.revflow_patient_id = r.revflow_patient_id
              AND f.dos = r.dos
              AND f.carc_kind = r.carc_kind
              AND {_ACTION_TAKEN_FLAG_SQL}
              AND lower(btrim(COALESCE(f.workload_status, ''))) = 'paid'
              AND {_PAID_PAYMENT_EMPTY_SQL}
              AND r.secondary_amount IS NOT NULL
              AND r.secondary_amount <> 0
            RETURNING f.revflow_patient_id
        ),
        cleared AS (
            UPDATE ops.pr_queue_flag f
            SET workload_status = NULL,
                updated_at = now()
            FROM ops.pr_queue_row r
            WHERE f.revflow_patient_id = r.revflow_patient_id
              AND f.dos = r.dos
              AND f.carc_kind = r.carc_kind
              AND {_ACTION_TAKEN_FLAG_SQL}
              AND lower(btrim(COALESCE(f.workload_status, ''))) = 'paid'
              AND {_PAID_PAYMENT_EMPTY_SQL}
              AND (r.secondary_amount IS NULL OR r.secondary_amount = 0)
            RETURNING f.revflow_patient_id
        )
        SELECT
            (SELECT count(*)::int FROM filled) AS filled,
            (SELECT count(*)::int FROM cleared) AS cleared
        """,
    )
    return {
        "filled": int((row or {}).get("filled") or 0),
        "cleared": int((row or {}).get("cleared") or 0),
    }


def _second_insurance_patient_key(emr_patient_id: str | None, revflow_patient_id: str) -> str:
    emr = str(emr_patient_id or "").strip()
    if emr:
        return f"emr:{emr}"
    return f"rf:{str(revflow_patient_id or '').strip()}"


def _inherit_second_insurance_defaults(conn: psycopg.Connection) -> int:
    """Stamp current patient default onto snapshot rows with no stored name."""
    client.execute(
        conn,
        """
        INSERT INTO ops.pr_queue_flag (
            revflow_patient_id, dos, carc_kind, second_submission, second_insurance, updated_at
        )
        SELECT
            r.revflow_patient_id,
            r.dos,
            r.carc_kind,
            false,
            d.insurance_name,
            now()
        FROM ops.pr_queue_row r
        JOIN ops.pr_second_insurance_default d
          ON d.patient_key = CASE
                WHEN NULLIF(btrim(r.emr_patient_id), '') IS NOT NULL
                THEN 'emr:' || btrim(r.emr_patient_id)
                ELSE 'rf:' || r.revflow_patient_id
             END
        LEFT JOIN ops.pr_queue_flag f
          ON f.revflow_patient_id = r.revflow_patient_id
         AND f.dos = r.dos
         AND f.carc_kind = r.carc_kind
        WHERE f.revflow_patient_id IS NULL
          AND r.carc_kind IN ('pr1', 'pr2')
          AND NULLIF(btrim(d.insurance_name), '') IS NOT NULL
        ON CONFLICT (revflow_patient_id, dos, carc_kind) DO NOTHING
        """,
    )
    client.execute(
        conn,
        """
        UPDATE ops.pr_queue_flag f
        SET second_insurance = d.insurance_name
        FROM ops.pr_queue_row r
        JOIN ops.pr_second_insurance_default d
          ON d.patient_key = CASE
                WHEN NULLIF(btrim(r.emr_patient_id), '') IS NOT NULL
                THEN 'emr:' || btrim(r.emr_patient_id)
                ELSE 'rf:' || r.revflow_patient_id
             END
        WHERE f.revflow_patient_id = r.revflow_patient_id
          AND f.dos = r.dos
          AND f.carc_kind = r.carc_kind
          AND r.carc_kind IN ('pr1', 'pr2')
          AND NULLIF(btrim(f.second_insurance), '') IS NULL
          AND NULLIF(btrim(d.insurance_name), '') IS NOT NULL
        """,
    )
    row = client.fetchone(
        conn,
        """
        SELECT count(*)::int AS n
        FROM ops.pr_queue_flag f
        JOIN ops.pr_queue_row r
          ON r.revflow_patient_id = f.revflow_patient_id
         AND r.dos = f.dos
         AND r.carc_kind = f.carc_kind
        WHERE NULLIF(btrim(f.second_insurance), '') IS NOT NULL
        """,
    )
    return int(row["n"]) if row else 0


def _pr_queue_filters(
    *,
    q: str | None,
    facility: list[str] | None,
    month: list[str] | None,
    paid: str,
) -> tuple[str, list[Any]]:
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if q:
        clauses.append(
            """(
                base.patient_name ILIKE %s
                OR base.emr_patient_id ILIKE %s
                OR base.revflow_patient_id ILIKE %s
                OR base.account_number ILIKE %s
            )"""
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if facility:
        clauses.append("base.facility_name = ANY(%s)")
        params.append(facility)
    if month:
        month_parts: list[str] = []
        for m in month:
            start, end = _month_bounds(m)
            month_parts.append("(base.dos BETWEEN %s AND %s)")
            params.extend([start, end])
        clauses.append("(" + " OR ".join(month_parts) + ")")
    paid_key = (paid or "all").strip().lower()
    if paid_key == "paid":
        clauses.append("base.secondary_amount IS NOT NULL")
    elif paid_key == "unpaid":
        clauses.append("base.secondary_amount IS NULL")
    return " AND ".join(clauses), params


def _list_pr_queue(
    conn: psycopg.Connection,
    *,
    carc_kind: str,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Read PR-1 / PR-2 from ops.pr_queue_row; Second Submission is live."""
    if carc_kind not in PR_FLAG_KINDS:
        raise ValueError("unsupported PR CARC kind")
    where, params = _pr_queue_filters(q=q, facility=facility, month=month, paid=paid)
    sort_col = _PR_SORTABLE.get((sort_by or "dos").strip(), "base.dos")
    direction = _sort_direction(sort_dir)
    page = max(1, page)
    page_size = min(max(1, page_size), 2000)
    offset = (page - 1) * page_size
    count_row = client.fetchone(
        conn,
        f"""
        SELECT
            count(*)::int AS n,
            count(*) FILTER (WHERE base.secondary_amount IS NOT NULL)::int AS paid_n
        FROM ops.pr_queue_row base
        LEFT JOIN ops.pr_queue_flag flag
          ON flag.revflow_patient_id = base.revflow_patient_id
         AND flag.dos = base.dos
         AND flag.carc_kind = base.carc_kind
        WHERE base.carc_kind = %s AND {where} AND {WORKLOAD_IN_QUEUE_SQL}
          AND NOT {MEDICARE_MEDICAID_SQL}
          AND {SS_ON_ELIGIBILITY_SQL}
        """,
        [carc_kind, *params],
    )
    total = int(count_row["n"]) if count_row else 0
    paid_count = int(count_row["paid_n"]) if count_row else 0

    rows = client.fetchall(
        conn,
        f"""
        SELECT
            base.revflow_patient_id,
            base.dos,
            base.primary_payer,
            base.primary_check_number,
            base.emr_patient_id,
            base.patient_name,
            base.facility_name,
            base.secondary_amount,
            base.secondary_check_number,
            base.secondary_check_date,
            base.secondary_payer,
            base.account_number,
            (base.secondary_amount IS NOT NULL) AS secondary_paid,
            COALESCE(flag.second_submission, false) AS second_submission,
            flag.second_insurance
        FROM ops.pr_queue_row base
        LEFT JOIN ops.pr_queue_flag flag
          ON flag.revflow_patient_id = base.revflow_patient_id
         AND flag.dos = base.dos
         AND flag.carc_kind = %s
        WHERE base.carc_kind = %s AND {where} AND {WORKLOAD_IN_QUEUE_SQL}
          AND NOT {MEDICARE_MEDICAID_SQL}
          AND {SS_ON_ELIGIBILITY_SQL}
        ORDER BY {sort_col} {direction} NULLS LAST, base.dos DESC, base.revflow_patient_id
        LIMIT %s OFFSET %s
        """,
        [carc_kind, carc_kind, *params, page_size, offset],
    )
    return {
        "items": rows,
        "total": total,
        "paid_count": paid_count,
        "unpaid_count": max(0, total - paid_count),
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 0,
    }


def list_secondary_queue(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Visits whose primary EOB carries CARC PR-2 (owed a secondary payment)."""
    return _list_pr_queue(
        conn,
        carc_kind="pr2",
        q=q,
        facility=facility,
        month=month,
        paid=paid,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )


def list_deductible_queue(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Visits whose primary EOB carries CARC PR-1 (deductible / patient responsibility)."""
    return _list_pr_queue(
        conn,
        carc_kind="pr1",
        q=q,
        facility=facility,
        month=month,
        paid=paid,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )


def list_pr100_queue(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Visits whose EOB check carries CARC PR-100. Read-only; no workload flags."""
    where, params = _pr_queue_filters(q=q, facility=facility, month=month, paid="all")
    sort_col = _PR_SORTABLE.get((sort_by or "dos").strip(), "base.dos")
    direction = _sort_direction(sort_dir)
    page = max(1, page)
    page_size = min(max(1, page_size), 2000)
    offset = (page - 1) * page_size
    count_row = client.fetchone(
        conn,
        f"""
        SELECT count(*)::int AS n
        FROM ops.pr_queue_row base
        WHERE base.carc_kind = 'pr100' AND {where}
        """,
        params,
    )
    total = int(count_row["n"]) if count_row else 0
    rows = client.fetchall(
        conn,
        f"""
        SELECT
            base.revflow_patient_id,
            base.dos,
            base.primary_payer,
            base.primary_check_number,
            base.emr_patient_id,
            base.patient_name,
            base.facility_name,
            base.account_number
        FROM ops.pr_queue_row base
        WHERE base.carc_kind = 'pr100' AND {where}
        ORDER BY {sort_col} {direction} NULLS LAST, base.dos DESC, base.revflow_patient_id
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 0,
    }


_WORKLOAD_SORTABLE = {
    "kind": "base.carc_kind",
    "emr_patient_id": "base.emr_patient_id",
    "patient_name": "base.patient_name",
    "primary_payer": "base.primary_payer",
    "second_insurance": "flag.second_insurance",
    "facility_name": "base.facility_name",
    "dos": "base.dos",
    "submission_date": "flag.submission_date",
    "workload_status": "flag.workload_status",
    "payment": "flag.payment",
    "provider": "flag.provider",
    "submitter": "flag.submitter",
    "claim_number": "flag.claim_number",
    "paid_date": "flag.paid_date",
    "check_number": "flag.check_number",
    "tfl_days_left": TFL_DAYS_LEFT_EXPR,
    "tfl_due": "(base.dos + tfl.tfl_days)",
    "tfl_days": "tfl.tfl_days",
}

WORKLOAD_STATUS_NONE = "__none__"
WORKLOAD_MIN_DOS_SQL = "base.dos >= DATE '2026-01-01'"


def _append_workload_filters(
    where: str,
    params: list[Any],
    *,
    status: list[str] | None,
    second_insurance: list[str] | None,
    tfl: str | None = None,
    has_status: bool | None = None,
    exclude_status: list[str] | None = None,
    exclude_second_insurance: list[str] | None = None,
    submitter: list[str] | None = None,
    submission_date: list[str] | None = None,
) -> tuple[str, list[Any]]:
    where = f"{where} AND {WORKLOAD_MIN_DOS_SQL}"
    if has_status is True:
        where = f"{where} AND NULLIF(btrim(flag.workload_status), '') IS NOT NULL"
    elif has_status is False:
        where = f"{where} AND NULLIF(btrim(flag.workload_status), '') IS NULL"
    statuses = [str(s).strip() for s in (status or []) if str(s).strip()]
    if statuses and has_status is not False:
        named = [s for s in statuses if s != WORKLOAD_STATUS_NONE]
        include_none = WORKLOAD_STATUS_NONE in statuses
        parts: list[str] = []
        if named:
            parts.append("flag.workload_status = ANY(%s)")
            params.append(named)
        if include_none:
            parts.append("NULLIF(btrim(flag.workload_status), '') IS NULL")
        if parts:
            where = f"{where} AND ({' OR '.join(parts)})"
    excluded = [str(s).strip() for s in (exclude_status or []) if str(s).strip()]
    if excluded:
        where = f"{where} AND btrim(flag.workload_status) <> ALL(%s)"
        params.append(excluded)
    ins = [fold_insurance_name(s) for s in (second_insurance or []) if fold_insurance_name(s)]
    if ins:
        where = f"{where} AND ops.fold_insurance_name(flag.second_insurance) = ANY(%s)"
        params.append(ins)
    excluded_ins = [
        fold_insurance_name(s) for s in (exclude_second_insurance or []) if fold_insurance_name(s)
    ]
    if excluded_ins:
        where = f"{where} AND ops.fold_insurance_name(flag.second_insurance) <> ALL(%s)"
        params.append(excluded_ins)
    submitters = [str(s).strip() for s in (submitter or []) if str(s).strip()]
    if submitters:
        where = f"{where} AND btrim(flag.submitter) = ANY(%s)"
        params.append(submitters)
    sub_dates: list[date] = []
    for raw in submission_date or []:
        parsed = raw if isinstance(raw, date) else parse_date(str(raw or "").strip()[:10])
        if parsed:
            sub_dates.append(parsed)
    if sub_dates:
        where = f"{where} AND flag.submission_date = ANY(%s)"
        params.append(sub_dates)
    tfl_key = (tfl or "").strip().lower()
    tfl_clause = WORKLOAD_TFL_FILTERS.get(tfl_key)
    if tfl_clause:
        where = f"{where} AND ({tfl_clause})"
    return where, params


def _merge_second_insurance_options(used: list[str]) -> list[str]:
    seed = list(SECOND_INSURANCE_NAMES)
    seen = {name.casefold() for name in seed}
    extras: list[str] = []
    for raw in used:
        name = str(raw or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        extras.append(name)
    extras.sort(key=str.casefold)
    return seed + extras


def pr_submission_meta(conn: psycopg.Connection) -> dict[str, Any]:
    facilities = client.fetchall(
        conn,
        """
        SELECT DISTINCT base.facility_name AS v
        FROM ops.pr_queue_row base
        WHERE base.facility_name IS NOT NULL
          AND btrim(base.facility_name) <> ''
        ORDER BY 1
        """,
    )
    months = client.fetchall(
        conn,
        """
        SELECT DISTINCT to_char(base.dos, 'YYYY-MM') AS v
        FROM ops.pr_queue_row base
        ORDER BY 1 DESC
        """,
    )
    second_ins = client.fetchall(
        conn,
        """
        SELECT DISTINCT btrim(v) AS v
        FROM (
            SELECT flag.second_insurance AS v
            FROM ops.pr_queue_flag flag
            WHERE NULLIF(btrim(flag.second_insurance), '') IS NOT NULL
            UNION
            SELECT d.insurance_name AS v
            FROM ops.pr_second_insurance_default d
            WHERE NULLIF(btrim(d.insurance_name), '') IS NOT NULL
        ) names
        ORDER BY 1
        """,
    )
    submission_dates = client.fetchall(
        conn,
        """
        SELECT DISTINCT to_char(flag.submission_date, 'YYYY-MM-DD') AS v
        FROM ops.pr_queue_flag flag
        WHERE flag.submission_date IS NOT NULL
        ORDER BY 1 DESC
        """,
    )
    submitters = client.fetchall(
        conn,
        """
        SELECT DISTINCT btrim(flag.submitter) AS v
        FROM ops.pr_queue_flag flag
        WHERE NULLIF(btrim(flag.submitter), '') IS NOT NULL
        ORDER BY 1
        """,
    )
    return {
        "filters": {
            "facility": [r["v"] for r in facilities],
            "month": [r["v"] for r in months],
            "second_insurance": _merge_second_insurance_options(
                [r["v"] for r in second_ins]
            ),
            "submission_date": [r["v"] for r in submission_dates],
        },
        "workload": {
            "statuses": list(WORKLOAD_STATUSES),
            "providers": list(WORKLOAD_PROVIDERS),
            "submitters": [r["v"] for r in submitters],
        },
    }


def _stamp_overdue_timely_filing(conn: psycopg.Connection) -> int:
    """Set Timely Filing on overdue PR-2 Workload rows with real second insurance."""
    row = client.fetchone(
        conn,
        f"""
        WITH overdue AS (
            SELECT
                flag.revflow_patient_id,
                flag.dos,
                flag.carc_kind
            FROM ops.pr_queue_flag flag
            JOIN ops.pr_queue_row base
              ON flag.revflow_patient_id = base.revflow_patient_id
             AND flag.dos = base.dos
             AND flag.carc_kind = base.carc_kind
            {TFL_JOIN_SQL}
            WHERE flag.carc_kind = 'pr2'
              AND {WORKLOAD_MIN_DOS_SQL}
              AND NULLIF(btrim(flag.workload_status), '') IS NULL
              AND NULLIF(btrim(flag.second_insurance), '') IS NOT NULL
              AND NOT {NO_SECONDARY_PAYER_SQL}
              AND NOT {COLLECTED_PR2_SQL}
              AND tfl.tfl_days IS NOT NULL
              AND {TFL_DAYS_LEFT_EXPR} < 0
        ),
        stamped AS (
            UPDATE ops.pr_queue_flag flag
            SET second_submission = true,
                workload_status = 'Timely Filing',
                submission_date = COALESCE(flag.submission_date, CURRENT_DATE),
                submitter = COALESCE(NULLIF(btrim(flag.submitter), ''), %s),
                updated_at = now()
            FROM overdue o
            WHERE flag.revflow_patient_id = o.revflow_patient_id
              AND flag.dos = o.dos
              AND flag.carc_kind = o.carc_kind
            RETURNING flag.revflow_patient_id
        )
        SELECT count(*)::int AS n FROM stamped
        """,
        (SYSTEM_SUBMITTER,),
    )
    return int(row["n"]) if row else 0


def _requests_collected(names: list[str] | None) -> bool:
    target = fold_insurance_name(COLLECTED)
    return any(fold_insurance_name(s) == target for s in (names or []))


def list_workload_queue(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    kind: str | None = None,
    status: list[str] | None = None,
    second_insurance: list[str] | None = None,
    tfl: str | None = None,
    has_status: bool | None = None,
    exclude_status: list[str] | None = None,
    exclude_second_insurance: list[str] | None = None,
    submitter: list[str] | None = None,
    submission_date: list[str] | None = None,
    require_moved: bool = True,
    medicare_medicaid: bool = False,
    sort_by: str = "tfl_days_left",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    _stamp_overdue_timely_filing(conn)
    if medicare_medicaid:
        require_moved = False
    where, params = _pr_queue_filters(q=q, facility=facility, month=month, paid="all")
    kind_key = (kind or "").strip().lower()
    if kind_key in PR_FLAG_KINDS:
        where = f"{where} AND base.carc_kind = %s"
        params.append(kind_key)
    where, params = _append_workload_filters(
        where,
        params,
        status=status,
        second_insurance=second_insurance,
        tfl=tfl,
        has_status=has_status,
        exclude_status=exclude_status,
        exclude_second_insurance=exclude_second_insurance,
        submitter=submitter,
        submission_date=submission_date,
    )
    if medicare_medicaid:
        where = f"{where} AND {MEDICARE_MEDICAID_SQL}"
    else:
        where = f"{where} AND NOT {MEDICARE_MEDICAID_SQL}"
    where = f"{where} AND {SS_ON_ELIGIBILITY_SQL}"
    if not _requests_collected(second_insurance):
        where = f"{where} AND NOT {COLLECTED_PR2_SQL}"
    moved_sql = WORKLOAD_MOVED_SQL if require_moved else "TRUE"
    sort_col = _WORKLOAD_SORTABLE.get((sort_by or "tfl_days_left").strip(), TFL_DAYS_LEFT_EXPR)
    direction = _sort_direction(sort_dir)
    page = max(1, page)
    page_size = min(max(1, page_size), 2000)
    offset = (page - 1) * page_size
    count_row = client.fetchone(
        conn,
        f"""
        SELECT count(*)::int AS n
        FROM ops.pr_queue_row base
        JOIN ops.pr_queue_flag flag
          ON flag.revflow_patient_id = base.revflow_patient_id
         AND flag.dos = base.dos
         AND flag.carc_kind = base.carc_kind
        {TFL_JOIN_SQL}
        WHERE {where} AND {moved_sql}
        """,
        params,
    )
    total = int(count_row["n"]) if count_row else 0
    rows = client.fetchall(
        conn,
        f"""
        SELECT
            base.revflow_patient_id,
            base.dos,
            base.carc_kind,
            base.emr_patient_id,
            base.patient_name,
            base.primary_payer,
            base.facility_name,
            flag.second_insurance,
            flag.second_submission,
            flag.submission_date,
            flag.workload_status,
            flag.payment,
            flag.provider,
            flag.submitter,
            flag.claim_number,
            flag.paid_date,
            flag.check_number,
            flag.workload_note,
            {TFL_SELECT_SQL}
        FROM ops.pr_queue_row base
        JOIN ops.pr_queue_flag flag
          ON flag.revflow_patient_id = base.revflow_patient_id
         AND flag.dos = base.dos
         AND flag.carc_kind = base.carc_kind
        {TFL_JOIN_SQL}
        WHERE {where} AND {moved_sql}
        ORDER BY {sort_col} {direction} NULLS LAST, base.dos DESC, base.revflow_patient_id
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 0,
    }


def search_workload_visits(
    conn: psycopg.Connection,
    *,
    q: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find existing core.visit rows by name, EMR, RevFlow, DOS, or facility."""
    needle = str(q or "").strip()
    if len(needle) < 2:
        return []
    lim = min(max(1, int(limit)), 50)
    like = f"%{needle}%"
    return client.fetchall(
        conn,
        """
        SELECT
            v.visit_id,
            p.revflow_patient_id,
            p.webpt_patient_id AS emr_patient_id,
            ph.patient_name,
            v.service_date AS dos,
            f.name AS facility_name,
            v.insurance_name_raw AS primary_payer
        FROM core.visit v
        JOIN core.patient p ON p.patient_id = v.patient_id
        LEFT JOIN core.patient_history ph
          ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        WHERE v.service_date >= DATE '2026-01-01'
          AND (
            NULLIF(btrim(p.webpt_patient_id), '') IS NOT NULL
            OR NULLIF(btrim(p.revflow_patient_id), '') IS NOT NULL
          )
          AND (
            COALESCE(ph.patient_name, '') ILIKE %s
            OR COALESCE(p.webpt_patient_id, '') ILIKE %s
            OR COALESCE(p.revflow_patient_id, '') ILIKE %s
            OR to_char(v.service_date, 'YYYY-MM-DD') ILIKE %s
            OR to_char(v.service_date, 'MM/DD/YYYY') ILIKE %s
            OR COALESCE(f.name, '') ILIKE %s
          )
        ORDER BY v.service_date DESC, ph.patient_name
        LIMIT %s
        """,
        (like, like, like, like, like, like, lim),
    )


def _queue_account_number(rf: str, emr: str | None) -> str | None:
    text = str(rf or "").strip()
    if text.upper().startswith("PV4"):
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return f"PV4{digits.lstrip('0') or digits}"
    emr_text = str(emr or "").strip()
    return emr_text or None


def add_workload_visit(
    conn: psycopg.Connection,
    *,
    visit_id: str,
    carc_kind: str,
    second_insurance: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Insert an existing core.visit into Workload. Rejects unknown visits."""
    kind = (carc_kind or "").strip().lower()
    if kind not in PR_FLAG_KINDS:
        raise ValueError("kind must be pr1 or pr2")
    ins = str(second_insurance or "").strip()
    if not ins:
        raise ValueError("second_insurance is required")
    vid = str(visit_id or "").strip()
    if not vid:
        raise ValueError("visit_id is required")
    visit = client.fetchone(
        conn,
        """
        SELECT
            v.visit_id,
            p.revflow_patient_id,
            p.webpt_patient_id AS emr_patient_id,
            ph.patient_name,
            v.service_date AS dos,
            f.name AS facility_name,
            v.insurance_name_raw AS primary_payer
        FROM core.visit v
        JOIN core.patient p ON p.patient_id = v.patient_id
        LEFT JOIN core.patient_history ph
          ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        WHERE v.visit_id = %s::uuid
          AND v.service_date >= DATE '2026-01-01'
        """,
        (vid,),
    )
    if not visit:
        raise ValueError("visit is not in the system")
    emr = str(visit.get("emr_patient_id") or "").strip() or None
    rf = str(visit.get("revflow_patient_id") or "").strip() or emr
    if not rf:
        raise ValueError("visit is not in the system")
    dos = visit.get("dos")
    client.execute(
        conn,
        """
        INSERT INTO ops.pr_queue_row (
            revflow_patient_id, dos, carc_kind,
            patient_name, emr_patient_id, account_number,
            primary_payer, facility_name, refreshed_at, source
        )
        VALUES (
            %s, %s::date, %s,
            %s, %s, %s,
            %s, %s, now(), 'manual'
        )
        ON CONFLICT (revflow_patient_id, dos, carc_kind) DO UPDATE
        SET patient_name = COALESCE(EXCLUDED.patient_name, ops.pr_queue_row.patient_name),
            emr_patient_id = COALESCE(EXCLUDED.emr_patient_id, ops.pr_queue_row.emr_patient_id),
            account_number = COALESCE(EXCLUDED.account_number, ops.pr_queue_row.account_number),
            facility_name = COALESCE(EXCLUDED.facility_name, ops.pr_queue_row.facility_name),
            primary_payer = COALESCE(EXCLUDED.primary_payer, ops.pr_queue_row.primary_payer),
            source = CASE
                WHEN ops.pr_queue_row.source = 'carc' THEN 'carc'
                WHEN ops.pr_queue_row.source = 'ptoc' THEN 'ptoc'
                ELSE 'manual'
            END
        """,
        (
            rf,
            dos,
            kind,
            visit.get("patient_name"),
            emr,
            _queue_account_number(rf, emr),
            visit.get("primary_payer"),
            visit.get("facility_name"),
        ),
    )
    flag = upsert_pr_queue_flag(
        conn,
        revflow_patient_id=rf,
        dos=dos,
        carc_kind=kind,
        second_submission=True,
        second_insurance=ins,
        actor_id=actor_id,
        promote=True,
    )
    return {
        "ok": True,
        "revflow_patient_id": rf,
        "dos": dos,
        "carc_kind": kind,
        "second_insurance": ins,
        "flag": flag,
    }


def add_workload_visits(
    conn: psycopg.Connection,
    *,
    visit_ids: list[str],
    carc_kind: str,
    second_insurance: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw in visit_ids:
        vid = str(raw or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        ids.append(vid)
    if not ids:
        raise ValueError("visit_id is required")
    items: list[dict[str, Any]] = []
    for vid in ids:
        items.append(
            add_workload_visit(
                conn,
                visit_id=vid,
                carc_kind=carc_kind,
                second_insurance=second_insurance,
                actor_id=actor_id,
            )
        )
    return {"ok": True, "items": items, "n": len(items)}


def _parse_workload_key(item: dict[str, Any]) -> tuple[str, date, str]:
    rf = str(item.get("revflow_patient_id") or "").strip()
    kind = str(item.get("kind") or item.get("carc_kind") or "").strip().lower()
    parsed = item.get("dos")
    if not isinstance(parsed, date):
        parsed = parse_date(str(parsed or ""))
    if not rf or parsed is None or kind not in PR_FLAG_KINDS:
        raise ValueError("revflow_patient_id, dos, and kind are required")
    return rf, parsed, kind


def remove_workload_visits(
    conn: psycopg.Connection,
    *,
    items: list[dict[str, Any]],
    actor_id: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw in items or []:
        rf, parsed, kind = _parse_workload_key(raw)
        rows.append(
            upsert_pr_queue_flag(
                conn,
                revflow_patient_id=rf,
                dos=parsed,
                carc_kind=kind,
                second_submission=False,
                second_insurance="",
                actor_id=actor_id,
                promote=False,
                stamp_missing_submission_date=False,
            )
        )
    return {"ok": True, "items": rows, "n": len(rows)}


def bulk_update_workload_visits(
    conn: psycopg.Connection,
    *,
    items: list[dict[str, Any]],
    patch: dict[str, Any],
    actor_id: str | None = None,
    actor_name: str | None = None,
) -> dict[str, Any]:
    allowed = {
        "second_insurance",
        "workload_status",
        "payment",
        "provider",
        "claim_number",
        "paid_date",
        "check_number",
        "workload_note",
    }
    fields = {k: patch[k] for k in allowed if k in patch and patch[k] is not None}
    rows: list[dict[str, Any]] = []
    for raw in items or []:
        rf, parsed, kind = _parse_workload_key(raw)
        rows.append(
            upsert_pr_queue_flag(
                conn,
                revflow_patient_id=rf,
                dos=parsed,
                carc_kind=kind,
                actor_id=actor_id,
                actor_name=actor_name,
                **fields,
            )
        )
    return {"ok": True, "items": rows, "n": len(rows)}


def _fill_sibling_insurance(
    conn: psycopg.Connection,
    *,
    revflow_patient_id: str,
    dos: date,
    carc_kind: str,
    insurance_name: str,
) -> int:
    """Stamp insurance_name onto later same-patient DOS rows (past dates stay)."""
    name = str(insurance_name or "").strip()
    if not name:
        return 0
    src_key = _PATIENT_KEY_SQL.format(alias="src")
    dst_key = _PATIENT_KEY_SQL.format(alias="r")
    row = client.fetchone(
        conn,
        f"""
        WITH siblings AS (
            SELECT r.revflow_patient_id, r.dos, r.carc_kind
            FROM ops.pr_queue_row r
            JOIN ops.pr_queue_row src
              ON ({dst_key}) = ({src_key})
             AND src.revflow_patient_id = %s
             AND src.dos = %s::date
             AND src.carc_kind = %s
            WHERE r.carc_kind = %s
              AND r.dos > src.dos
        ),
        upserted AS (
            INSERT INTO ops.pr_queue_flag (
                revflow_patient_id, dos, carc_kind, second_insurance, updated_at
            )
            SELECT s.revflow_patient_id, s.dos, s.carc_kind, %s, now()
            FROM siblings s
            ON CONFLICT (revflow_patient_id, dos, carc_kind) DO UPDATE
            SET second_insurance = EXCLUDED.second_insurance,
                updated_at = now()
            WHERE ops.fold_insurance_name(
                    COALESCE(ops.pr_queue_flag.second_insurance, '')
                )
                IS DISTINCT FROM ops.fold_insurance_name(EXCLUDED.second_insurance)
            RETURNING 1
        )
        SELECT count(*)::int AS n FROM upserted
        """,
        (revflow_patient_id, dos, carc_kind, carc_kind, name),
    )
    return int(row["n"]) if row else 0


def _upsert_second_insurance_default(
    conn: psycopg.Connection,
    *,
    emr_patient_id: str | None,
    revflow_patient_id: str,
    insurance_name: str,
    actor_id: str | None,
) -> None:
    name = str(insurance_name or "").strip()
    if not name:
        return
    key = _second_insurance_patient_key(emr_patient_id, revflow_patient_id)
    if not key.endswith(":") and len(key) > 3:
        client.execute(
            conn,
            """
            INSERT INTO ops.pr_second_insurance_default (
                patient_key, insurance_name, updated_by, updated_at
            )
            VALUES (%s, %s, %s::uuid, now())
            ON CONFLICT (patient_key) DO UPDATE
            SET insurance_name = EXCLUDED.insurance_name,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (key, name, actor_id),
        )


def _is_no_secondary_payer(name: str | None) -> bool:
    folded = fold_insurance_name(name or "")
    return bool(folded) and folded == fold_insurance_name(NO_SECONDARY_PAYER)


def _payment_has_money(raw: Any) -> bool:
    text = str(raw or "").replace("$", "").replace(",", "").strip()
    if not text:
        return False
    amount = _as_number(text)
    if amount is None:
        return True
    return amount != 0


def _format_workload_payment(value: Any) -> str | None:
    amount = _as_number(value)
    if amount is None or amount == 0:
        return None
    return f"{amount:.2f}"


def upsert_pr_queue_flag(
    conn: psycopg.Connection,
    *,
    revflow_patient_id: str,
    dos: date | str,
    carc_kind: str,
    second_submission: bool | None = None,
    second_insurance: str | None = None,
    submission_date: date | str | None = None,
    workload_status: str | None = None,
    payment: str | None = None,
    provider: str | None = None,
    submitter: str | None = None,
    claim_number: str | None = None,
    paid_date: date | str | None = None,
    check_number: str | None = None,
    workload_note: str | None = None,
    actor_id: str | None = None,
    actor_name: str | None = None,
    promote: bool = True,
    fill_empty_only: bool = False,
    stamp_missing_submission_date: bool = True,
) -> dict[str, Any]:
    """Persist PR-1 / PR-2 flag + workload fields for one visit."""
    kind = (carc_kind or "").strip().lower()
    if kind not in PR_FLAG_KINDS:
        raise ValueError("carc_kind must be pr1 or pr2")
    rf = str(revflow_patient_id or "").strip()
    if not rf:
        raise ValueError("revflow_patient_id is required")
    parsed = dos if isinstance(dos, date) else parse_date(str(dos or ""))
    if parsed is None:
        raise ValueError("dos is required")

    existing = client.fetchone(
        conn,
        """
        SELECT *
        FROM ops.pr_queue_flag
        WHERE revflow_patient_id = %s AND dos = %s::date AND carc_kind = %s
        """,
        (rf, parsed, kind),
    ) or {}

    def _cur_text(current: Any) -> str | None:
        if current in (None, ""):
            return None
        text = str(current).strip()
        return text or None

    def _text(value: Any, current: Any) -> str | None:
        cur = _cur_text(current)
        if fill_empty_only and cur:
            return cur
        if value is None:
            return cur
        text = str(value).strip()
        return text or None

    def _date(value: Any, current: Any) -> date | None:
        cur = current if isinstance(current, date) else parse_date(str(current or ""))
        if fill_empty_only and cur is not None:
            return cur
        if value is None:
            return cur
        if isinstance(value, date):
            return value
        return parse_date(str(value))

    if fill_empty_only and existing.get("second_submission"):
        sub = True
    else:
        sub = (
            bool(second_submission)
            if second_submission is not None
            else bool(existing.get("second_submission"))
        )
    ins = _text(second_insurance, existing.get("second_insurance"))
    status = _text(workload_status, existing.get("workload_status"))
    pay = _text(payment, existing.get("payment"))
    prov = _text(provider, existing.get("provider"))
    claim = _text(claim_number, existing.get("claim_number"))
    paid = _date(paid_date, existing.get("paid_date"))
    chk = _text(check_number, existing.get("check_number"))
    note = _text(workload_note, existing.get("workload_note"))

    snap = None
    if ins:
        snap = client.fetchone(
            conn,
            """
            SELECT emr_patient_id, secondary_amount
            FROM ops.pr_queue_row
            WHERE revflow_patient_id = %s AND dos = %s::date AND carc_kind = %s
            """,
            (rf, parsed, kind),
        )
        if (
            kind == "pr2"
            and not fill_empty_only
            and not _is_no_secondary_payer(ins)
            and _format_workload_payment((snap or {}).get("secondary_amount"))
        ):
            sub = True
            if not status:
                status = "paid"
            if not pay:
                pay = _format_workload_payment((snap or {}).get("secondary_amount"))

    existing_date = existing.get("submission_date")
    if isinstance(existing_date, date):
        locked_date = existing_date
    else:
        locked_date = parse_date(str(existing_date or "")) if existing_date else None
    already_taken = bool(_cur_text(existing.get("workload_status")))
    if locked_date is not None and already_taken:
        sub_date = locked_date
    elif stamp_missing_submission_date and status:
        sub_date = date.today()
    else:
        sub_date = _date(submission_date, None)

    actor = str(actor_name or "").strip() or None
    existing_submitter = _cur_text(existing.get("submitter"))
    first_status = bool(status) and not _cur_text(existing.get("workload_status"))
    if existing_submitter:
        subm = existing_submitter
    elif actor and first_status:
        subm = actor
    else:
        subm = _text(submitter, existing.get("submitter"))

    if (status or "").strip().lower() == "paid" and not _payment_has_money(pay):
        raise ValueError("Payment is required when Status is paid")

    row = client.fetchone(
        conn,
        """
        INSERT INTO ops.pr_queue_flag (
            revflow_patient_id, dos, carc_kind, second_submission, second_insurance,
            submission_date, workload_status, payment, provider, submitter,
            claim_number, paid_date, check_number, workload_note, updated_by, updated_at
        )
        VALUES (
            %s, %s::date, %s, %s, %s,
            %s::date, %s, %s, %s, %s,
            %s, %s::date, %s, %s, %s::uuid, now()
        )
        ON CONFLICT (revflow_patient_id, dos, carc_kind) DO UPDATE
        SET second_submission = EXCLUDED.second_submission,
            second_insurance = EXCLUDED.second_insurance,
            submission_date = EXCLUDED.submission_date,
            workload_status = EXCLUDED.workload_status,
            payment = EXCLUDED.payment,
            provider = EXCLUDED.provider,
            submitter = EXCLUDED.submitter,
            claim_number = EXCLUDED.claim_number,
            paid_date = EXCLUDED.paid_date,
            check_number = EXCLUDED.check_number,
            workload_note = EXCLUDED.workload_note,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING *
        """,
        (
            rf, parsed, kind, sub, ins,
            sub_date, status, pay, prov, subm,
            claim, paid, chk, note, actor_id,
        ),
    )
    if not row:
        raise RuntimeError("pr_queue_flag upsert returned no row")

    if ins:
        _upsert_second_insurance_default(
            conn,
            emr_patient_id=(snap or {}).get("emr_patient_id"),
            revflow_patient_id=rf,
            insurance_name=ins,
            actor_id=actor_id,
        )
        _fill_sibling_insurance(
            conn,
            revflow_patient_id=rf,
            dos=parsed,
            carc_kind=kind,
            insurance_name=ins,
        )
    if promote:
        _promote_pr2_siblings_to_workload(conn)
        _jump_paid_pr2_to_action_taken(conn, actor_name)
    return row


def _with_facility_display(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return row
    resolved = row.get("facility_name_resolved")
    if resolved:
        row["facility_name"] = resolved
    return row


def get_work_item(conn: psycopg.Connection, work_item_id: str) -> dict[str, Any] | None:
    row = _with_facility_display(
        client.fetchone(
            conn,
            """
            SELECT
                wi.*,
                COALESCE(f.name, wi.facility_name) AS facility_name_resolved,
                au.display_name AS assigned_to_name,
                au.collector_code AS assigned_to_code,
                uu.display_name AS updated_by_name,
                lu.display_name AS locked_by_name
            FROM ops.eligibility_work_item wi
            LEFT JOIN ref.facility f ON f.webpt_facility_id = wi.facility_name
            LEFT JOIN auth.app_user au ON au.user_id = wi.assigned_to
            LEFT JOIN auth.app_user uu ON uu.user_id = wi.updated_by
            LEFT JOIN auth.app_user lu ON lu.user_id = wi.locked_by
            WHERE wi.work_item_id = %s::uuid
            """,
            (work_item_id,),
        )
    )
    if not row:
        return None
    overlay_live_sf(conn, [row])
    overlay_live_recon(conn, [row])
    overlay_pr1_reductions(conn, [row])
    overlay_rtm_amounts(conn, [row])
    overlay_oa23_amounts(conn, [row])
    overlay_denial_reasons(conn, [row])
    attach_sheet_fields(row)
    overlay_tracker_dates(conn, [row])
    overlay_eft_totals(conn, [row])
    overlay_ledger_totals(conn, [row])
    apply_manual_overrides(row)
    row.pop("context", None)
    return _drop_overlay_keys(row)


def get_history(conn: psycopg.Connection, work_item_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT h.*, u.display_name AS changed_by_name, r.display_name AS reason_display
        FROM ops.eligibility_history h
        LEFT JOIN auth.app_user u ON u.user_id = h.changed_by
        LEFT JOIN ref.eligibility_change_reason r ON r.reason_key = h.reason_key
        WHERE h.work_item_id = %s::uuid
        ORDER BY h.changed_at DESC
        """,
        (work_item_id,),
    )


def get_comments(conn: psycopg.Connection, work_item_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT c.*, u.display_name AS created_by_name
        FROM ops.eligibility_comment c
        LEFT JOIN auth.app_user u ON u.user_id = c.created_by
        WHERE c.work_item_id = %s::uuid
        ORDER BY c.created_at DESC
        """,
        (work_item_id,),
    )


def get_attachments(conn: psycopg.Connection, work_item_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT a.*, d.storage_path AS document_storage_path, d.filename AS document_filename
        FROM ops.eligibility_attachment a
        LEFT JOIN docs.document d ON d.document_id = a.document_id
        WHERE a.work_item_id = %s::uuid
        ORDER BY a.created_at
        """,
        (work_item_id,),
    )


def get_attachment(conn: psycopg.Connection, attachment_id: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        """
        SELECT a.*, d.storage_path AS document_storage_path, d.filename AS document_filename
        FROM ops.eligibility_attachment a
        LEFT JOIN docs.document d ON d.document_id = a.document_id
        WHERE a.attachment_id = %s::uuid
        """,
        (attachment_id,),
    )


def add_history(
    conn: psycopg.Connection,
    *,
    work_item_id: str,
    column_name: str,
    old_value: Any,
    new_value: Any,
    changed_by: str | None,
    reason_key: str | None = None,
    reason_text: str | None = None,
) -> None:
    client.execute(
        conn,
        """
        INSERT INTO ops.eligibility_history (
            work_item_id, column_name, old_value, new_value,
            changed_by, reason_key, reason_text
        ) VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s, %s)
        """,
        (
            work_item_id,
            column_name,
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            changed_by,
            reason_key,
            reason_text,
        ),
    )


def get_amount_ledger(conn: psycopg.Connection, work_item_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT l.*, u.display_name AS created_by_name
        FROM ops.eligibility_amount_ledger l
        LEFT JOIN auth.app_user u ON u.user_id = l.created_by
        WHERE l.work_item_id = %s::uuid
        ORDER BY l.created_at DESC, l.ledger_id DESC
        """,
        (work_item_id,),
    )


def insert_amount_ledger(
    conn: psycopg.Connection,
    *,
    work_item_id: str,
    column_name: str,
    amount: float,
    source: str = "manual",
    check_number: str | None = None,
    check_date: str | date | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> None:
    if abs(float(amount)) < 0.0001:
        return
    src = (source or "manual").strip().lower()
    if src not in LEDGER_SOURCES:
        src = "manual"
    parsed_date = check_date if isinstance(check_date, date) else parse_date(str(check_date or ""))
    client.execute(
        conn,
        """
        INSERT INTO ops.eligibility_amount_ledger (
            work_item_id, column_name, amount, check_number, check_date,
            source, note, created_by
        ) VALUES (%s::uuid, %s, %s, %s, %s::date, %s, %s, %s::uuid)
        """,
        (
            work_item_id,
            ledger_column_name(column_name),
            round(float(amount), 2),
            _as_text(check_number),
            parsed_date,
            src,
            _as_text(note),
            created_by,
        ),
    )


def money_deltas_from_history(history: list[dict[str, Any]]) -> list[tuple[str, float]]:
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for entry in history:
        raw_col = str(entry.get("column_name") or "")
        if raw_col not in MONEY_FIELDS:
            continue
        canon = ledger_column_name(raw_col)
        if canon in seen:
            continue
        seen.add(canon)
        old = _as_number(entry.get("old_value")) or 0.0
        new = _as_number(entry.get("new_value")) or 0.0
        delta = round(new - old, 2)
        if abs(delta) < 0.0001:
            continue
        out.append((canon, delta))
    return out


def assert_paid_has_manual_payment(item: dict[str, Any], updates: dict[str, Any]) -> None:
    """Paid is allowed only after Insurance Payment was saved by hand."""
    status = str(updates.get("source_visit_status") or "").strip().lower()
    if status != "paid":
        return
    current = parse_manual_overrides(item.get("manual_overrides"))
    for key in ("insurance_payment", "paid_amount"):
        if key in updates:
            if normalize_edit_value(key, updates.get(key)) is not None:
                return
            continue
        if _as_number(current.get(key)) is not None:
            return
    raise ValueError("Enter Insurance Payment before marking the visit Paid")


def _collection_membership_bucket(
    conn: psycopg.Connection,
    work_item_id: str,
    collection_status: str,
) -> str | None:
    """Tab for the visit's current Collection Status, including paid PR-3."""
    bucket = collection_status_bucket(collection_status)
    if (
        bucket is None
        and fold_label(collection_status) == "paid"
        and work_item_has_pr3(conn, work_item_id)
    ):
        return "paid_patient_responsibility"
    return bucket


def _rehome_collection_member(
    conn: psycopg.Connection,
    work_item_id: str,
    bucket: str | None,
) -> None:
    """Replace Collection tab membership after a status edit.

    A routed Collection Status wins. Otherwise the visit lands on Denied
    (including collection and unpaid PR-3) or Overdue when those rules match.
    """
    client.execute(
        conn,
        "DELETE FROM analytics.collection_queue_member WHERE work_item_id = %s::uuid",
        (work_item_id,),
    )
    if bucket:
        client.execute(
            conn,
            """
            INSERT INTO analytics.collection_queue_member (bucket, work_item_id)
            VALUES (%s, %s::uuid)
            ON CONFLICT DO NOTHING
            """,
            (bucket, work_item_id),
        )
        return
    for home_bucket, home_rule in (
        (
            "denied",
            f"""(
                (({DENIED_VISIT_SQL}) OR ({COLLECTION_VISIT_SQL}) OR ({PR3_UNPAID_SQL}))
                AND NOT ({ROUTED_COLLECTION_SQL})
                AND NOT ({PR3_PAID_COLLECTION_SQL})
            )""",
        ),
        (
            "overdue",
            f"({OVERDUE_PENDING_SQL}) AND NOT ({ROUTED_COLLECTION_SQL}) AND NOT ({PR3_UNPAID_SQL})",
        ),
    ):
        client.execute(
            conn,
            f"""
            INSERT INTO analytics.collection_queue_member (bucket, work_item_id)
            SELECT %s, wi.work_item_id
            FROM ops.eligibility_work_item wi
            WHERE wi.work_item_id = %s::uuid
              AND {home_rule}
            ON CONFLICT DO NOTHING
            """,
            (home_bucket, work_item_id),
        )


def patch_work_item(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    updates: dict[str, Any],
    reason_key: str | None = None,
    reason_text: str | None = None,
    ledger_source: str = "manual",
    ledger_note: str | None = None,
    ledger_check_number: str | None = None,
    ledger_check_date: str | date | None = None,
) -> dict[str, Any] | None:
    item = get_work_item(conn, work_item_id)
    if not item:
        return None
    assert_paid_has_manual_payment(item, updates)
    direct, new_ov, history, ov_changed = plan_work_item_patch(item, updates)
    routed = (
        collection_status_bucket(str(new_ov.get("collection_status") or ""))
        if "collection_status" in updates
        else None
    )
    if (
        routed is None
        and "collection_status" in updates
        and fold_label(str(new_ov.get("collection_status") or "")) == "paid"
        and work_item_has_pr3(conn, work_item_id)
    ):
        routed = "paid_patient_responsibility"
    if routed == "action":
        today = date.today().isoformat()
        prior_work = next((h for h in history if h["column_name"] == "work_date"), None)
        old_work = prior_work["old_value"] if prior_work else _history_text(item.get("work_date"))
        new_ov["work_date"] = today
        if prior_work:
            prior_work["new_value"] = today
        elif old_work != today:
            history.append(
                {
                    "column_name": "work_date",
                    "old_value": old_work,
                    "new_value": today,
                }
            )
        actor = client.fetchone(
            conn,
            """
            SELECT COALESCE(
                NULLIF(btrim(collector_code), ''),
                NULLIF(btrim(display_name), '')
            ) AS label
            FROM auth.app_user
            WHERE user_id = %s::uuid
            """,
            (actor_id,),
        )
        label = _as_text((actor or {}).get("label")) or "Collector"
        new_ov["collector_1"] = label
        prior_collector = next((h for h in history if h["column_name"] == "collector_1"), None)
        old_collector = (
            prior_collector["old_value"] if prior_collector else _history_text(item.get("collector_1"))
        )
        if prior_collector:
            prior_collector["new_value"] = label
        elif old_collector != label:
            history.append(
                {
                    "column_name": "collector_1",
                    "old_value": old_collector,
                    "new_value": label,
                }
            )
        ov_changed = True
    if not history and not ov_changed and not direct and routed != "action":
        return item

    for entry in history:
        add_history(
            conn,
            work_item_id=work_item_id,
            column_name=entry["column_name"],
            old_value=entry["old_value"],
            new_value=entry["new_value"],
            changed_by=actor_id,
            reason_key=reason_key,
            reason_text=reason_text,
        )
    for column_name, delta in money_deltas_from_history(history):
        insert_amount_ledger(
            conn,
            work_item_id=work_item_id,
            column_name=column_name,
            amount=delta,
            source=ledger_source,
            check_number=ledger_check_number,
            check_date=ledger_check_date,
            note=ledger_note or reason_text,
            created_by=actor_id,
        )

    applied = dict(direct)
    sets = [f"{k} = %s" for k in applied]
    params: list[Any] = list(applied.values())
    if ov_changed:
        sets.append("manual_overrides = %s::jsonb")
        params.append(json.dumps(new_ov, default=str))
    if "eligibility_status" in applied:
        terminal = client.fetchone(
            conn,
            "SELECT is_terminal FROM ref.eligibility_status WHERE status_key = %s",
            (applied["eligibility_status"],),
        )
        if terminal and terminal.get("is_terminal"):
            sets.append("completed_at = COALESCE(completed_at, now())")
        else:
            sets.append("completed_at = NULL")
    if routed == "action":
        sets.append("assigned_to = %s::uuid")
        params.append(actor_id)
        sets.append("assigned_at = now()")
    if not sets:
        return item
    sets.append("updated_by = %s::uuid")
    params.append(actor_id)
    sets.append("updated_at = now()")
    params.append(work_item_id)
    client.execute(
        conn,
        f"UPDATE ops.eligibility_work_item SET {', '.join(sets)} WHERE work_item_id = %s::uuid",
        params,
    )
    saved_status = str(new_ov.get("source_visit_status") or "").strip().lower()
    if "source_visit_status" in updates and saved_status in ("paid", "deduct"):
        client.execute(
            conn,
            "DELETE FROM analytics.collection_queue_member WHERE work_item_id = %s::uuid",
            (work_item_id,),
        )
    elif "source_visit_status" in updates or "collection_status" in updates:
        if "collection_status" in updates:
            bucket = routed
        else:
            bucket = _collection_membership_bucket(
                conn,
                work_item_id,
                str(new_ov.get("collection_status") or ""),
            )
        _rehome_collection_member(conn, work_item_id, bucket)
    return get_work_item(conn, work_item_id)


def add_amount_adjustment(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    column_name: str,
    amount: float,
    check_number: str | None = None,
    check_date: str | date | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    canon = ledger_column_name(column_name)
    if canon not in MONEY_FIELDS or canon == "paid_amount":
        raise ValueError("column_name must be a sheet money field")
    delta = _as_number(amount)
    if delta is None:
        raise ValueError("amount must be a number")
    item = get_work_item(conn, work_item_id)
    if not item:
        return None
    current = _as_number(item.get(canon)) or 0.0
    new_val = round(current + delta, 4)
    updates = {canon: new_val}
    if canon == "insurance_payment":
        updates["paid_amount"] = new_val
    return patch_work_item(
        conn,
        work_item_id,
        actor_id=actor_id,
        updates=updates,
        reason_text=note,
        ledger_source="manual",
        ledger_note=note,
        ledger_check_number=check_number,
        ledger_check_date=check_date,
    )


def collection_member_ids(conn: psycopg.Connection, work_item_ids: list[str]) -> set[str]:
    """Work items that currently belong to the Collection queue."""
    if not work_item_ids:
        return set()
    rows = client.fetchall(
        conn,
        """
        SELECT DISTINCT work_item_id::text AS work_item_id
        FROM analytics.collection_queue_member
        WHERE work_item_id = ANY(%s::uuid[])
        """,
        (list(work_item_ids),),
    )
    return {str(row["work_item_id"]) for row in rows}


def assign_work_item(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    assignee_id: str | None,
    reason_key: str | None = None,
    reason_text: str | None = None,
) -> dict[str, Any] | None:
    item = get_work_item(conn, work_item_id)
    if not item:
        return None
    old = item.get("assigned_to")
    old_s = str(old) if old else None
    new_s = str(assignee_id) if assignee_id else None
    if old_s == new_s:
        return item
    add_history(
        conn,
        work_item_id=work_item_id,
        column_name="assigned_to",
        old_value=old_s,
        new_value=new_s,
        changed_by=actor_id,
        reason_key=reason_key,
        reason_text=reason_text,
    )
    client.execute(
        conn,
        """
        UPDATE ops.eligibility_work_item
        SET assigned_to = %s::uuid,
            assigned_at = CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END,
            updated_by = %s::uuid,
            updated_at = now()
        WHERE work_item_id = %s::uuid
        """,
        (assignee_id, assignee_id, actor_id, work_item_id),
    )
    return get_work_item(conn, work_item_id)


def _lock_active(item: dict[str, Any]) -> bool:
    exp = item.get("lock_expires_at")
    if not item.get("locked_by") or not exp:
        return False
    if isinstance(exp, str):
        exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def acquire_lock(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    force: bool = False,
) -> dict[str, Any]:
    item = get_work_item(conn, work_item_id)
    if not item:
        return {"ok": False, "error": "not_found"}
    if _lock_active(item) and str(item["locked_by"]) != actor_id and not force:
        return {
            "ok": False,
            "error": "locked",
            "locked_by": str(item["locked_by"]),
            "locked_by_name": item.get("locked_by_name"),
            "lock_expires_at": item.get("lock_expires_at"),
        }
    expires = datetime.now(timezone.utc) + timedelta(minutes=LOCK_TTL_MINUTES)
    client.execute(
        conn,
        """
        UPDATE ops.eligibility_work_item
        SET locked_by = %s::uuid, locked_at = now(), lock_expires_at = %s
        WHERE work_item_id = %s::uuid
        """,
        (actor_id, expires, work_item_id),
    )
    return {"ok": True, "item": get_work_item(conn, work_item_id)}


def heartbeat_lock(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
) -> dict[str, Any]:
    item = get_work_item(conn, work_item_id)
    if not item:
        return {"ok": False, "error": "not_found"}
    if str(item.get("locked_by") or "") != actor_id:
        return {"ok": False, "error": "not_owner"}
    expires = datetime.now(timezone.utc) + timedelta(minutes=LOCK_TTL_MINUTES)
    client.execute(
        conn,
        """
        UPDATE ops.eligibility_work_item
        SET lock_expires_at = %s
        WHERE work_item_id = %s::uuid AND locked_by = %s::uuid
        """,
        (expires, work_item_id, actor_id),
    )
    return {"ok": True, "lock_expires_at": expires}


def release_lock(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    force: bool = False,
) -> dict[str, Any]:
    item = get_work_item(conn, work_item_id)
    if not item:
        return {"ok": False, "error": "not_found"}
    if not force and str(item.get("locked_by") or "") != actor_id:
        return {"ok": False, "error": "not_owner"}
    client.execute(
        conn,
        """
        UPDATE ops.eligibility_work_item
        SET locked_by = NULL, locked_at = NULL, lock_expires_at = NULL
        WHERE work_item_id = %s::uuid
        """,
        (work_item_id,),
    )
    return {"ok": True}


def kpis(
    conn: psycopg.Connection,
    *,
    facility: list[str] | None = None,
    month: list[str] | None = None,
) -> dict[str, int]:
    where, params = _build_filters(
        q=None, facility=facility, month=month, insurance=None, status=None, assigned_to=None
    )
    overdue_cut = date.today() - timedelta(days=OVERDUE_DAYS)
    row = client.fetchone(
        conn,
        f"""
        SELECT
            count(*) FILTER (WHERE wi.eligibility_status = 'pending')::int AS pending,
            count(*) FILTER (WHERE wi.eligibility_status = 'checking')::int AS in_progress,
            count(*) FILTER (WHERE wi.eligibility_status = 'waiting_insurance')::int AS waiting_insurance,
            count(*) FILTER (WHERE wi.eligibility_status = 'waiting_patient')::int AS waiting_patient,
            count(*) FILTER (WHERE wi.eligibility_status = 'completed')::int AS completed,
            count(*) FILTER (
                WHERE wi.eligibility_status = 'completed'
                  AND wi.completed_at::date = CURRENT_DATE
            )::int AS completed_today,
            count(*) FILTER (
                WHERE wi.eligibility_status IN (
                    SELECT status_key FROM ref.eligibility_status WHERE NOT is_terminal
                )
                  AND wi.dos <= %s
            )::int AS overdue
        FROM ops.eligibility_work_item wi
        WHERE {where}
        """,
        [overdue_cut, *params],
    )
    return dict(row) if row else {}


def chart_aggregates(
    conn: psycopg.Connection,
    *,
    facility: list[str] | None = None,
    month: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    where, params = _build_filters(
        q=None, facility=facility, month=month, insurance=None, status=None, assigned_to=None
    )
    by_facility = client.fetchall(
        conn,
        f"""
        SELECT COALESCE(f.name, wi.facility_name) AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        LEFT JOIN ref.facility f ON f.webpt_facility_id = wi.facility_name
        WHERE {where}
        GROUP BY COALESCE(f.name, wi.facility_name)
        ORDER BY value DESC
        LIMIT 20
        """,
        params,
    )
    by_user = client.fetchall(
        conn,
        f"""
        SELECT u.display_name AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        JOIN auth.app_user u ON u.user_id = wi.assigned_to
        JOIN ref.eligibility_status s ON s.status_key = wi.eligibility_status
        WHERE {where}
          AND NOT s.is_terminal
        GROUP BY u.display_name
        ORDER BY value DESC
        LIMIT 20
        """,
        params,
    )
    by_status = client.fetchall(
        conn,
        f"""
        SELECT COALESCE(s.display_name, wi.eligibility_status) AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        LEFT JOIN ref.eligibility_status s ON s.status_key = wi.eligibility_status
        WHERE {where}
        GROUP BY COALESCE(s.display_name, wi.eligibility_status)
        ORDER BY value DESC
        """,
        params,
    )
    return {
        "by_facility": by_facility,
        "by_user": by_user,
        "by_status": by_status,
    }


def filter_options(conn: psycopg.Connection) -> dict[str, list[Any]]:
    dos_window = f"{ELIGIBILITY_MIN_DOS_SQL} AND {ELIGIBILITY_MAX_DOS_SQL}"
    facilities = client.fetchall(
        conn,
        f"""
        SELECT DISTINCT COALESCE(f.name, wi.facility_name) AS v
        FROM ops.eligibility_work_item wi
        LEFT JOIN ref.facility f ON f.webpt_facility_id = wi.facility_name
        WHERE {dos_window}
          AND COALESCE(f.name, wi.facility_name) IS NOT NULL
        ORDER BY 1
        """,
    )
    from cashflow_db.repository.insurance import is_blank_sql, usable_sql

    insurances = client.fetchall(
        conn,
        f"""
        SELECT DISTINCT {usable_sql("wi.insurance_name")} AS v
        FROM ops.eligibility_work_item wi
        WHERE {dos_window}
          AND NOT {is_blank_sql("wi.insurance_name")}
        ORDER BY 1
        """,
    )
    months = client.fetchall(
        conn,
        f"""
        SELECT DISTINCT to_char(wi.dos, 'YYYY-MM') AS v
        FROM ops.eligibility_work_item wi
        WHERE {dos_window}
        ORDER BY 1 DESC
        """,
    )
    assignees = client.fetchall(
        conn,
        f"""
        SELECT DISTINCT u.user_id, u.display_name
        FROM ops.eligibility_work_item wi
        JOIN auth.app_user u ON u.user_id = wi.assigned_to
        WHERE {dos_window}
        ORDER BY u.display_name
        """,
    )
    return {
        "facility": [r["v"] for r in facilities],
        "insurance": [r["v"] for r in insurances],
        "month": [r["v"] for r in months],
        "assignees": assignees,
        "status": [r["status_key"] for r in list_statuses(conn)],
        "visit_status": sort_visit_statuses(
            [
                str(r["v"])
                for r in client.fetchall(
                    conn,
                    f"""
                    SELECT DISTINCT lower(wi.source_visit_status) AS v
                    FROM ops.eligibility_work_item wi
                    WHERE {dos_window}
                      AND wi.source_visit_status IS NOT NULL
                      AND btrim(wi.source_visit_status) <> ''
                      AND regexp_replace(lower(btrim(wi.source_visit_status)), '[\\s/-]+', '_', 'g')
                          NOT IN ('cancelled', 'canceled', 'no_show', 'noshow', 'cancelled_no_show', 'canceled_no_show')
                    """,
                )
            ]
        ),
        "check_date": [
            r["v"].isoformat() if hasattr(r["v"], "isoformat") else str(r["v"])[:10]
            for r in client.fetchall(
                conn,
                """
                SELECT DISTINCT rv.primary_check_date AS v
                FROM billing.reconciliation_visit_agg rv
                WHERE rv.reconciliation_run_id = (
                    SELECT reconciliation_run_id
                    FROM billing.reconciliation_run
                    WHERE status = 'success'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                  AND rv.primary_check_date IS NOT NULL
                ORDER BY 1 DESC
                """,
            )
            if r.get("v")
        ],
    }


def close_settled_work_items(
    conn: psycopg.Connection,
    rows: list[tuple[str, Any, str]],
    *,
    reason_key: str = "corrected_after_eob",
) -> int:
    """Complete unlocked queue items whose recon visit is paid/denied/etc.

    Skips ``checking`` and rows with ``locked_by``. Matches on EMR + DOS
    (facility names can differ between WebPT and Snowflake).
    """
    if not rows:
        return 0
    emrs: list[str] = []
    doses: list[date] = []
    statuses: list[str] = []
    seen: set[tuple[str, date]] = set()
    for emr, dos, st in rows:
        parsed = dos if isinstance(dos, date) else parse_date(str(dos or ""))
        key_emr = str(emr or "").strip()
        if not key_emr or parsed is None:
            continue
        key = (key_emr, parsed)
        if key in seen:
            continue
        seen.add(key)
        emrs.append(key_emr)
        doses.append(parsed)
        statuses.append((st or "").strip().lower())
    if not emrs:
        return 0
    closed = client.fetchall(
        conn,
        """
        WITH src AS (
            SELECT * FROM unnest(%s::text[], %s::date[], %s::text[])
                AS t(emr, dos, visit_status)
        ),
        picked AS (
            SELECT wi.work_item_id, wi.eligibility_status AS old_status, src.visit_status
            FROM ops.eligibility_work_item wi
            JOIN src ON wi.emr_patient_id = src.emr AND wi.dos = src.dos
            WHERE wi.eligibility_status IN ('pending', 'waiting_insurance', 'waiting_patient')
              AND wi.locked_by IS NULL
        ),
        upd AS (
            UPDATE ops.eligibility_work_item wi
            SET
                eligibility_status = 'completed',
                completed_at = COALESCE(wi.completed_at, now()),
                source_visit_status = picked.visit_status,
                updated_at = now()
            FROM picked
            WHERE wi.work_item_id = picked.work_item_id
            RETURNING wi.work_item_id
        )
        INSERT INTO ops.eligibility_history (
            work_item_id, column_name, old_value, new_value, reason_key, reason_text
        )
        SELECT
            picked.work_item_id,
            'eligibility_status',
            picked.old_status,
            'completed',
            %s,
            'recon visit_status settled'
        FROM picked
        RETURNING work_item_id
        """,
        (emrs, doses, statuses, reason_key),
    )
    return len(closed)


def resolve_facility_display_name(conn: psycopg.Connection, raw: str) -> str:
    """Map a WebPT facility id to the clinic name; leave unknown values as-is."""
    value = str(raw or "").strip()
    if not value:
        return value
    row = client.fetchone(
        conn,
        """
        SELECT name
        FROM ref.facility
        WHERE webpt_facility_id = %s OR name = %s
        ORDER BY CASE WHEN webpt_facility_id = %s THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (value, value, value),
    )
    return str(row["name"]).strip() if row and row.get("name") else value


def upsert_from_visit(
    conn: psycopg.Connection,
    visit: dict[str, Any],
    *,
    recon_run_id: str | None = None,
    etl_run_id: str | None = None,
) -> str:
    """Insert or refresh snapshot fields only; preserve ops edits."""
    facility = resolve_facility_display_name(
        conn, str(visit.get("facility_name") or "").strip()
    )
    emr = str(visit.get("webpt_patient_id") or visit.get("emr_patient_id") or "").strip()
    dos = visit.get("date_of_service") or visit.get("dos")
    if not facility or not emr or not dos:
        raise ValueError("facility_name, emr_patient_id, dos required")

    context = {
        "total_paid": visit.get("total_paid"),
        "matched_paid": visit.get("matched_paid"),
        "visit_paid_total": visit.get("visit_paid_total"),
        "primary_check_number": visit.get("primary_check_number"),
        "primary_check_date": str(visit.get("primary_check_date") or "") or None,
        "primary_check_amount": visit.get("primary_check_amount"),
        "secondary_check_number": visit.get("secondary_check_number"),
        "secondary_check_date": str(visit.get("secondary_check_date") or "") or None,
        "secondary_check_amount": visit.get("secondary_check_amount"),
        "third_check_number": visit.get("third_check_number"),
        "third_check_date": str(visit.get("third_check_date") or "") or None,
        "third_check_amount": visit.get("third_check_amount"),
        "fourth_check_number": visit.get("fourth_check_number"),
        "fourth_check_date": str(visit.get("fourth_check_date") or "") or None,
        "fourth_check_amount": visit.get("fourth_check_amount"),
        "updated_check_number": visit.get("third_check_number"),
        "updated_check_date": str(visit.get("third_check_date") or "") or None,
        "updated_check_amount": visit.get("third_check_amount"),
        "missing_tracker_checks": visit.get("missing_tracker_checks") or [],
        "paid_lines": visit.get("paid_lines"),
        "pending_lines": visit.get("pending_lines"),
    }
    from cashflow_db.repository import insurance as ins_repo
    from cashflow_forecast.payer_plan import fill_blank_ins

    raw_ins = (
        visit.get("insurance_name")
        or visit.get("ins_name")
        or visit.get("primary_payor")
    )
    insurance_name = fill_blank_ins(
        str(raw_ins or ""),
        str(visit.get("insurance_revflow") or ""),
    )
    if not ins_repo.usable_insurance_name(insurance_name):
        insurance_name = ins_repo.lookup_insurance_name(
            conn,
            emr=emr,
            dos=dos,
            check_numbers=[
                visit.get("primary_check_number"),
                visit.get("secondary_check_number"),
            ],
            current_name=insurance_name,
        ) or None
    else:
        insurance_name = ins_repo.usable_insurance_name(insurance_name)

    existing = client.fetchone(
        conn,
        """
        SELECT work_item_id, context FROM ops.eligibility_work_item
        WHERE emr_patient_id = %s AND dos = %s::date
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (emr, dos),
    )
    if existing:
        old_ctx = existing.get("context") or {}
        if isinstance(old_ctx, str):
            try:
                old_ctx = json.loads(old_ctx)
            except json.JSONDecodeError:
                old_ctx = {}
        if not isinstance(old_ctx, dict):
            old_ctx = {}
        old_paid = _as_number(old_ctx.get("total_paid"))
        new_paid = _as_number(visit.get("total_paid"))
        if old_paid is not None and new_paid is not None:
            delta = round(new_paid - old_paid, 2)
            if abs(delta) >= 0.01:
                insert_amount_ledger(
                    conn,
                    work_item_id=str(existing["work_item_id"]),
                    column_name="insurance_payment",
                    amount=delta,
                    source="recon",
                    check_number=_as_text(visit.get("primary_check_number")),
                    check_date=visit.get("primary_check_date"),
                    note="generate from recon",
                )
        client.execute(
            conn,
            """
            UPDATE ops.eligibility_work_item SET
                patient_name = COALESCE(%s, patient_name),
                dob = COALESCE(%s::date, dob),
                insurance_name = COALESCE(%s, insurance_name),
                source_visit_status = %s,
                context = %s::jsonb,
                source_recon_run_id = COALESCE(%s::uuid, source_recon_run_id),
                etl_run_id = COALESCE(%s::uuid, etl_run_id)
            WHERE work_item_id = %s::uuid
            """,
            (
                visit.get("patient_name"),
                visit.get("dob") or None,
                insurance_name,
                visit.get("visit_status") or visit.get("source_visit_status"),
                json.dumps(context, default=str),
                recon_run_id,
                etl_run_id,
                str(existing["work_item_id"]),
            ),
        )
        return str(existing["work_item_id"])

    row = client.fetchone(
        conn,
        """
        INSERT INTO ops.eligibility_work_item (
            facility_name, emr_patient_id, dos,
            patient_name, dob, insurance_name, source_visit_status,
            context, source_recon_run_id, etl_run_id
        ) VALUES (
            %s, %s, %s::date, %s, %s::date, %s, %s,
            %s::jsonb, %s::uuid, %s::uuid
        )
        ON CONFLICT (facility_name, emr_patient_id, dos) DO UPDATE SET
            patient_name = COALESCE(
                NULLIF(BTRIM(EXCLUDED.patient_name), ''),
                ops.eligibility_work_item.patient_name
            ),
            dob = EXCLUDED.dob,
            insurance_name = COALESCE(EXCLUDED.insurance_name, ops.eligibility_work_item.insurance_name),
            source_visit_status = EXCLUDED.source_visit_status,
            context = EXCLUDED.context,
            source_recon_run_id = COALESCE(EXCLUDED.source_recon_run_id, ops.eligibility_work_item.source_recon_run_id),
            etl_run_id = COALESCE(EXCLUDED.etl_run_id, ops.eligibility_work_item.etl_run_id),
            updated_at = ops.eligibility_work_item.updated_at
        RETURNING work_item_id
        """,
        (
            facility,
            emr,
            dos,
            visit.get("patient_name"),
            visit.get("dob") or None,
            insurance_name,
            visit.get("visit_status") or visit.get("source_visit_status"),
            json.dumps(context, default=str),
            recon_run_id,
            etl_run_id,
        ),
    )
    assert row
    return str(row["work_item_id"])


def link_attachment(
    conn: psycopg.Connection,
    *,
    work_item_id: str,
    document_id: str | None = None,
    storage_path: str | None = None,
    filename: str | None = None,
    doc_kind: str = "eligibility_pdf",
) -> None:
    if document_id:
        exists = client.fetchone(
            conn,
            """
            SELECT 1 AS ok FROM ops.eligibility_attachment
            WHERE work_item_id = %s::uuid AND doc_kind = %s AND document_id = %s::uuid
            """,
            (work_item_id, doc_kind, document_id),
        )
        if exists:
            return
        client.execute(
            conn,
            """
            INSERT INTO ops.eligibility_attachment (
                work_item_id, document_id, storage_path, filename, doc_kind
            ) VALUES (%s::uuid, %s::uuid, %s, %s, %s)
            """,
            (work_item_id, document_id, storage_path, filename, doc_kind),
        )
        return
    if not storage_path:
        return
    exists = client.fetchone(
        conn,
        """
        SELECT 1 AS ok FROM ops.eligibility_attachment
        WHERE work_item_id = %s::uuid AND doc_kind = %s AND storage_path = %s
        """,
        (work_item_id, doc_kind, storage_path),
    )
    if exists:
        return
    client.execute(
        conn,
        """
        INSERT INTO ops.eligibility_attachment (
            work_item_id, storage_path, filename, doc_kind
        ) VALUES (%s::uuid, %s, %s, %s)
        """,
        (work_item_id, storage_path, filename, doc_kind),
    )


def find_eligibility_docs_for_emr(
    conn: psycopg.Connection,
    emr_patient_id: str,
) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT d.document_id, d.storage_path, d.filename, d.source
        FROM docs.document d
        JOIN core.patient p ON p.patient_id = d.patient_id
        WHERE p.webpt_patient_id = %s
          AND (
              lower(coalesce(d.filename, '')) LIKE '%%eligib%%'
              OR lower(coalesce(d.storage_path, '')) LIKE '%%eligib%%'
          )
        ORDER BY d.created_at DESC NULLS LAST
        LIMIT 5
        """,
        (emr_patient_id,),
    )


def list_visit_insurance_names(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """(emr_patient_id, dos) → Eligibility Sheet insurance_name for forecast fill."""
    return client.fetchall(
        conn,
        """
        SELECT DISTINCT ON (BTRIM(emr_patient_id), dos)
            BTRIM(emr_patient_id) AS emr_patient_id,
            dos,
            BTRIM(insurance_name) AS insurance_name
        FROM ops.eligibility_work_item
        WHERE NULLIF(BTRIM(insurance_name), '') IS NOT NULL
          AND NULLIF(BTRIM(emr_patient_id), '') IS NOT NULL
          AND dos IS NOT NULL
        ORDER BY BTRIM(emr_patient_id), dos, updated_at DESC NULLS LAST
        """,
    )


def sheet_paid_by_insurance(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Eligibility Sheet paid $ by insurer (status paid/partial or sheet paid amount)."""
    from cashflow_db.repository.insurance import insurance_match_tokens, usable_sql

    clauses = [
        f"""(
            lower(btrim(COALESCE(wi.source_visit_status, ''))) IN ('paid', 'partial')
            OR {_SHEET_PAID_AMT_SQL} > 0
        )"""
    ]
    params: list[Any] = []
    if d0:
        clauses.append("wi.dos >= %s")
        params.append(d0)
    if d1:
        clauses.append("wi.dos <= %s")
        params.append(d1)
    if facilities:
        clauses.append("wi.facility_name = ANY(%s)")
        params.append(facilities)
    tokens = insurance_match_tokens(insurers)
    if tokens:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM unnest(%s::text[]) AS tok(t)
                WHERE length(tok.t) >= 3
                  AND regexp_replace(lower(COALESCE(wi.insurance_name, '')), '[^a-z0-9]+', '', 'g')
                      LIKE '%%' || tok.t || '%%'
            )
            """
        )
        params.append(tokens)
    where = " AND ".join(clauses)
    cap = int(limit) if limit is not None and int(limit) > 0 else 0
    limit_sql = "LIMIT %s" if cap else ""
    query_params: list[Any] = list(params)
    if cap:
        query_params.append(cap)
    rows = client.fetchall(
        conn,
        f"""
        WITH latest AS (
            SELECT reconciliation_run_id
            FROM billing.reconciliation_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
        ),
        visit_paid AS (
            SELECT
                wi.work_item_id,
                {usable_sql("wi.insurance_name")} AS ins_name,
                MAX({_SHEET_PAID_AMT_SQL}) AS paid_amt
            FROM ops.eligibility_work_item wi
            LEFT JOIN billing.reconciliation_visit_agg rv
              ON rv.webpt_patient_id = wi.emr_patient_id
             AND rv.date_of_service = wi.dos
             AND rv.reconciliation_run_id = (SELECT reconciliation_run_id FROM latest)
            WHERE {where}
            GROUP BY 1, 2
        )
        SELECT ins_name, ROUND(SUM(paid_amt), 2) AS landed
        FROM visit_paid
        WHERE ins_name IS NOT NULL
        GROUP BY 1
        ORDER BY 2 DESC
        {limit_sql}
        """,
        query_params,
    )
    return [
        {
            "ins_name": str(row.get("ins_name") or ""),
            "landed": round(float(row.get("landed") or 0), 2),
        }
        for row in rows
    ]
