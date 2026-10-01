"""Collection dropdown catalogs and overdue-pending SLA helper."""

from __future__ import annotations

from typing import Any

import psycopg

from cashflow_db.repository import client
from cashflow_db.repository.pr_tfl import fold_insurance_name

LOOKUP_KINDS = ("denial_reason", "root_cause", "collection_status")

DENIAL_REASON_SEED = (
    "Covered by another payer",
    "Coverage terminated",
    "Auth Absent",
    "Auth Exceeded",
    "Invalid Diagnosis",
    "Benefit maximum",
)

ROOT_CAUSE_SEED = (
    "Auth delay",
    "Case got denied",
    "Medical documentation delay",
    "Provider out of state",
    "Missing referral",
    "missing Intake/consent",
    "Free Visits Exceeded (patient)",
    "Free Visits Exceeded (auth)",
    "Auth (invalid Denial)",
    "Covered by another payer (benefits)",
    "Coverage terminated (benefits)",
    "No OON benefits",
    "Submission Error (incorrect Payer)",
    "Submission error (patient demo)",
    "Invalid Diagnosis",
    "Patient Placed On hold",
    "Wrong schedule",
    "benefit maximum (Medicare KX)",
    "benefit maximum (Limit Visits Met)",
)

COLLECTION_STATUS_SEED = (
    "Pending",
    "Paid",
    "Dead",
    "Action Taken",
    "Arbitration",
    "Canceled - No Show",
    "Submitted without Auth",
)

SEED_BY_KIND: dict[str, tuple[str, ...]] = {
    "denial_reason": DENIAL_REASON_SEED,
    "root_cause": ROOT_CAUSE_SEED,
    "collection_status": COLLECTION_STATUS_SEED,
}

def still_pending_visit_sql(
    *,
    emr_expr: str,
    dos_expr: str,
    status_expr: str,
) -> str:
    """True when the visit is still pending on the sheet, Snowflake, and latest recon."""
    return f"""(
        lower(btrim(COALESCE({status_expr}, ''))) IN ('pending', '')
        AND NOT EXISTS (
            SELECT 1 FROM analytics.snowflake_visit_kpi sf
            WHERE sf.emr_id = {emr_expr}
              AND sf.date_of_service = {dos_expr}
              AND lower(btrim(COALESCE(sf.status, ''))) NOT IN ('pending', '')
        )
        AND NOT EXISTS (
            SELECT 1 FROM billing.reconciliation_visit_agg rv
            WHERE rv.reconciliation_run_id = (
                SELECT reconciliation_run_id
                FROM billing.reconciliation_run
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
            )
              AND rv.webpt_patient_id = {emr_expr}
              AND rv.date_of_service = {dos_expr}
              AND lower(btrim(COALESCE(rv.visit_status, ''))) NOT IN ('pending', '')
        )
    )"""


FORECAST_OVERDUE_EXISTS_SQL = """EXISTS (
        SELECT 1
        FROM analytics.forecast_prediction fp
        WHERE fp.forecast_run_id = (
            SELECT forecast_run_id
            FROM analytics.forecast_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
        )
          AND fp.outcome_stage = 'overdue'
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

_LIST_SQL = """
        SELECT
            lookup_id,
            kind,
            label,
            sort_order,
            active,
            updated_at
        FROM ops.collection_lookup
"""


def _normalize_kind(kind: str | None) -> str:
    raw = str(kind or "").strip().lower()
    if raw not in LOOKUP_KINDS:
        raise ValueError("kind must be denial_reason, root_cause, or collection_status")
    return raw


def _normalize_label(label: str | None) -> str:
    text = str(label or "").strip()
    if not text:
        raise ValueError("label is required")
    if len(text) > 200:
        raise ValueError("label is too long")
    return text


def list_lookups(
    conn: psycopg.Connection,
    *,
    kind: str | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if kind:
        clauses.append("kind = %s")
        params.append(_normalize_kind(kind))
    if not include_inactive:
        clauses.append("active")
    return client.fetchall(
        conn,
        f"""
        {_LIST_SQL}
        WHERE {' AND '.join(clauses)}
        ORDER BY kind, sort_order, label
        """,
        params,
    )


def lookups_by_kind(
    conn: psycopg.Connection,
    *,
    include_inactive: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    grouped = {k: [] for k in LOOKUP_KINDS}
    for row in list_lookups(conn, include_inactive=include_inactive):
        grouped.setdefault(row["kind"], []).append(row)
    return grouped


def get_lookup(conn: psycopg.Connection, lookup_id: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        f"{_LIST_SQL} WHERE lookup_id = %s::uuid",
        (lookup_id,),
    )


def _next_sort_order(conn: psycopg.Connection, kind: str) -> int:
    row = client.fetchone(
        conn,
        """
        SELECT COALESCE(MAX(sort_order), 0)::int AS n
        FROM ops.collection_lookup
        WHERE kind = %s
        """,
        (kind,),
    )
    return int(row["n"] if row else 0) + 10


def _assert_unique(
    conn: psycopg.Connection,
    kind: str,
    label: str,
    *,
    exclude_id: str | None = None,
) -> None:
    sql = """
        SELECT label
        FROM ops.collection_lookup
        WHERE kind = %s
          AND ops.fold_insurance_name(label) = %s
    """
    params: list[Any] = [kind, fold_insurance_name(label)]
    if exclude_id:
        sql += " AND lookup_id <> %s::uuid"
        params.append(exclude_id)
    clash = client.fetchone(conn, sql, params)
    if clash:
        raise ValueError(f"Value already exists: {clash['label']}")


def create_lookup(
    conn: psycopg.Connection,
    *,
    kind: str,
    label: str,
    sort_order: int | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    kind_key = _normalize_kind(kind)
    text = _normalize_label(label)
    _assert_unique(conn, kind_key, text)
    order = int(sort_order) if sort_order is not None else _next_sort_order(conn, kind_key)
    row = client.fetchone(
        conn,
        """
        INSERT INTO ops.collection_lookup (kind, label, sort_order, updated_by)
        VALUES (%s, %s, %s, %s::uuid)
        RETURNING lookup_id
        """,
        (kind_key, text, order, actor_id),
    )
    if not row:
        raise ValueError("Failed to create lookup")
    created = get_lookup(conn, str(row["lookup_id"]))
    if not created:
        raise ValueError("Failed to load lookup")
    return created


def update_lookup(
    conn: psycopg.Connection,
    lookup_id: str,
    *,
    label: str | None = None,
    sort_order: int | None = None,
    active: bool | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    current = get_lookup(conn, lookup_id)
    if not current:
        raise KeyError(lookup_id)
    text = str(current["label"])
    if label is not None:
        text = _normalize_label(label)
        _assert_unique(conn, str(current["kind"]), text, exclude_id=lookup_id)
    order = int(current["sort_order"])
    if sort_order is not None:
        order = int(sort_order)
    is_active = bool(current["active"])
    if active is not None:
        is_active = bool(active)
    client.execute(
        conn,
        """
        UPDATE ops.collection_lookup
        SET label = %s,
            sort_order = %s,
            active = %s,
            updated_by = %s::uuid,
            updated_at = now()
        WHERE lookup_id = %s::uuid
        """,
        (text, order, is_active, actor_id, lookup_id),
    )
    updated = get_lookup(conn, lookup_id)
    if not updated:
        raise KeyError(lookup_id)
    return updated


def delete_lookup(conn: psycopg.Connection, lookup_id: str) -> bool:
    row = client.fetchone(
        conn,
        "DELETE FROM ops.collection_lookup WHERE lookup_id = %s::uuid RETURNING lookup_id",
        (lookup_id,),
    )
    return row is not None


def fold_label(raw: str | None) -> str:
    return fold_insurance_name(raw)


def collection_status_bucket(label: str | None) -> str | None:
    """Tab a Collection Status edit moves into. Follow up is aged Action, not a status."""
    folded = fold_label(label)
    if folded == "arbitration":
        return "arbitration"
    if folded in {"actiontaken", "pending"}:
        return "action"
    if folded == "submittedwithoutauth":
        return "at_risk"
    if folded == "dead":
        return "dead"
    return None
