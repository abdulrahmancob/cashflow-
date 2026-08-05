"""Eligibility work-queue repository (ops schema)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from cashflow_db.repository import client

EDITABLE_FIELDS = frozenset({"eligibility_status", "reference_number", "notes"})
LOCK_TTL_MINUTES = 5
OVERDUE_DAYS = 7


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


def _build_filters(
    *,
    q: str | None,
    facility: list[str] | None,
    month: list[str] | None,
    insurance: list[str] | None,
    status: list[str] | None,
    assigned_to: list[str] | None,
    unassigned: bool = False,
) -> tuple[str, list[Any]]:
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if q:
        clauses.append(
            """(
                wi.patient_name ILIKE %s
                OR wi.emr_patient_id ILIKE %s
                OR wi.reference_number ILIKE %s
                OR wi.notes ILIKE %s
                OR wi.insurance_name ILIKE %s
            )"""
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if facility:
        clauses.append("wi.facility_name = ANY(%s)")
        params.append(facility)
    if insurance:
        clauses.append("wi.insurance_name = ANY(%s)")
        params.append(insurance)
    if status:
        clauses.append("wi.eligibility_status = ANY(%s)")
        params.append(status)
    if assigned_to:
        clauses.append("wi.assigned_to = ANY(%s::uuid[])")
        params.append(assigned_to)
    if unassigned:
        clauses.append("wi.assigned_to IS NULL")
    if month:
        month_parts: list[str] = []
        for m in month:
            start, end = _month_bounds(m)
            month_parts.append("(wi.dos BETWEEN %s AND %s)")
            params.extend([start, end])
        clauses.append("(" + " OR ".join(month_parts) + ")")
    return " AND ".join(clauses), params


_SORTABLE = {
    "patient_name": "wi.patient_name",
    "dos": "wi.dos",
    "dob": "wi.dob",
    "facility_name": "wi.facility_name",
    "insurance_name": "wi.insurance_name",
    "eligibility_status": "wi.eligibility_status",
    "reference_number": "wi.reference_number",
    "assigned_to": "au.display_name",
    "updated_at": "wi.updated_at",
    "updated_by": "uu.display_name",
}


def list_work_items(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    facility: list[str] | None = None,
    month: list[str] | None = None,
    insurance: list[str] | None = None,
    status: list[str] | None = None,
    assigned_to: list[str] | None = None,
    unassigned: bool = False,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    where, params = _build_filters(
        q=q,
        facility=facility,
        month=month,
        insurance=insurance,
        status=status,
        assigned_to=assigned_to,
        unassigned=unassigned,
    )
    sort_col = _SORTABLE.get(sort_by, "wi.dos")
    direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
    page = max(1, page)
    page_size = min(max(1, page_size), 500)
    offset = (page - 1) * page_size

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
            wi.work_item_id, wi.facility_name, wi.emr_patient_id, wi.dos,
            wi.patient_name, wi.dob, wi.insurance_name, wi.source_visit_status,
            wi.eligibility_status, wi.reference_number, wi.notes,
            wi.assigned_to, wi.assigned_at, wi.completed_at,
            wi.locked_by, wi.locked_at, wi.lock_expires_at,
            wi.updated_by, wi.updated_at, wi.created_at, wi.priority,
            au.display_name AS assigned_to_name,
            uu.display_name AS updated_by_name,
            lu.display_name AS locked_by_name
        FROM ops.eligibility_work_item wi
        LEFT JOIN auth.app_user au ON au.user_id = wi.assigned_to
        LEFT JOIN auth.app_user uu ON uu.user_id = wi.updated_by
        LEFT JOIN auth.app_user lu ON lu.user_id = wi.locked_by
        WHERE {where}
        ORDER BY {sort_col} {direction} NULLS LAST, wi.work_item_id
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


def get_work_item(conn: psycopg.Connection, work_item_id: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        """
        SELECT
            wi.*,
            au.display_name AS assigned_to_name,
            uu.display_name AS updated_by_name,
            lu.display_name AS locked_by_name
        FROM ops.eligibility_work_item wi
        LEFT JOIN auth.app_user au ON au.user_id = wi.assigned_to
        LEFT JOIN auth.app_user uu ON uu.user_id = wi.updated_by
        LEFT JOIN auth.app_user lu ON lu.user_id = wi.locked_by
        WHERE wi.work_item_id = %s::uuid
        """,
        (work_item_id,),
    )


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


def patch_work_item(
    conn: psycopg.Connection,
    work_item_id: str,
    *,
    actor_id: str,
    updates: dict[str, Any],
    reason_key: str | None = None,
    reason_text: str | None = None,
) -> dict[str, Any] | None:
    item = get_work_item(conn, work_item_id)
    if not item:
        return None
    applied: dict[str, Any] = {}
    for key, new_val in updates.items():
        if key not in EDITABLE_FIELDS:
            continue
        old_val = item.get(key)
        if (old_val or None) == (new_val or None):
            continue
        if key in ("eligibility_status", "reference_number") and not reason_key:
            raise ValueError("reason_key required for status/reference changes")
        applied[key] = new_val
        add_history(
            conn,
            work_item_id=work_item_id,
            column_name=key,
            old_value=old_val,
            new_value=new_val,
            changed_by=actor_id,
            reason_key=reason_key,
            reason_text=reason_text,
        )

    if not applied:
        return item

    sets = [f"{k} = %s" for k in applied]
    params: list[Any] = list(applied.values())
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
    sets.append("updated_by = %s::uuid")
    params.append(actor_id)
    sets.append("updated_at = now()")
    params.append(work_item_id)
    client.execute(
        conn,
        f"UPDATE ops.eligibility_work_item SET {', '.join(sets)} WHERE work_item_id = %s::uuid",
        params,
    )
    return get_work_item(conn, work_item_id)


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
            count(*) FILTER (
                WHERE wi.eligibility_status = 'completed'
                  AND wi.completed_at::date = CURRENT_DATE
            )::int AS completed_today,
            count(*) FILTER (
                WHERE wi.eligibility_status IN ('pending', 'checking')
                  AND wi.created_at::date <= %s
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
        SELECT wi.facility_name AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        WHERE {where}
        GROUP BY wi.facility_name
        ORDER BY value DESC
        LIMIT 20
        """,
        params,
    )
    by_user = client.fetchall(
        conn,
        f"""
        SELECT COALESCE(u.display_name, '(Unassigned)') AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        LEFT JOIN auth.app_user u ON u.user_id = wi.assigned_to
        WHERE {where}
        GROUP BY COALESCE(u.display_name, '(Unassigned)')
        ORDER BY value DESC
        LIMIT 20
        """,
        params,
    )
    by_status = client.fetchall(
        conn,
        f"""
        SELECT wi.eligibility_status AS key, count(*)::int AS value
        FROM ops.eligibility_work_item wi
        WHERE {where}
        GROUP BY wi.eligibility_status
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
    facilities = client.fetchall(
        conn,
        """
        SELECT DISTINCT facility_name AS v
        FROM ops.eligibility_work_item
        WHERE facility_name IS NOT NULL
        ORDER BY 1
        """,
    )
    insurances = client.fetchall(
        conn,
        """
        SELECT DISTINCT insurance_name AS v
        FROM ops.eligibility_work_item
        WHERE insurance_name IS NOT NULL
        ORDER BY 1
        """,
    )
    months = client.fetchall(
        conn,
        """
        SELECT DISTINCT to_char(dos, 'YYYY-MM') AS v
        FROM ops.eligibility_work_item
        ORDER BY 1 DESC
        """,
    )
    assignees = client.fetchall(
        conn,
        """
        SELECT DISTINCT u.user_id, u.display_name
        FROM ops.eligibility_work_item wi
        JOIN auth.app_user u ON u.user_id = wi.assigned_to
        ORDER BY u.display_name
        """,
    )
    return {
        "facility": [r["v"] for r in facilities],
        "insurance": [r["v"] for r in insurances],
        "month": [r["v"] for r in months],
        "assignees": assignees,
        "status": [r["status_key"] for r in list_statuses(conn)],
    }


def upsert_from_visit(
    conn: psycopg.Connection,
    visit: dict[str, Any],
    *,
    recon_run_id: str | None = None,
    etl_run_id: str | None = None,
) -> str:
    """Insert or refresh snapshot fields only; preserve ops edits."""
    facility = str(visit.get("facility_name") or "").strip()
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
        "paid_lines": visit.get("paid_lines"),
        "pending_lines": visit.get("pending_lines"),
    }
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
            patient_name = EXCLUDED.patient_name,
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
            visit.get("insurance_name") or visit.get("ins_name") or visit.get("primary_payor"),
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
