"""Transaction Tracker rows, grants, audit, and upload preview staging."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Json

from cashflow_db.repository import client

RESOURCE_KEY = "transaction_tracker"

ROW_FIELDS = (
    "payment_id",
    "month_date",
    "txn_date",
    "amount",
    "eft_1",
    "eft_2",
    "transaction_type",
    "description",
    "check_reference",
    "bank_name",
    "billing_status",
    "collector",
    "posted",
    "notes",
    "assigned_date",
    "claims",
)


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = str(v)
        elif hasattr(v, "hex"):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _month_bounds(month: str) -> tuple[date, date]:
    """Parse YYYY-MM → inclusive start/end dates."""
    year_s, month_s = month.split("-", 1)
    y, m = int(year_s), int(month_s)
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start, end


def empty_grant() -> dict[str, bool]:
    return {
        "can_view": False,
        "can_edit": False,
        "can_upload": False,
        "can_admin": False,
    }


def get_grant(
    conn: psycopg.Connection, user_id: str, *, resource_key: str = RESOURCE_KEY
) -> dict[str, Any]:
    row = client.fetchone(
        conn,
        """
        SELECT grant_id, user_id, resource_key,
               can_view, can_edit, can_upload, can_admin,
               granted_by, created_at, updated_at
        FROM auth.resource_grant
        WHERE user_id = %s::uuid AND resource_key = %s
        """,
        (user_id, resource_key),
    )
    if not row:
        return {"user_id": user_id, "resource_key": resource_key, **empty_grant()}
    return row


def list_grants(
    conn: psycopg.Connection, *, resource_key: str = RESOURCE_KEY
) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT g.grant_id, g.user_id, g.resource_key,
               g.can_view, g.can_edit, g.can_upload, g.can_admin,
               g.granted_by, g.created_at, g.updated_at,
               u.username, u.display_name, u.is_active
        FROM auth.resource_grant g
        JOIN auth.app_user u ON u.user_id = g.user_id
        WHERE g.resource_key = %s
        ORDER BY u.display_name, u.username
        """,
        (resource_key,),
    )


def upsert_grant(
    conn: psycopg.Connection,
    *,
    user_id: str,
    can_view: bool,
    can_edit: bool,
    can_upload: bool,
    can_admin: bool,
    granted_by: str | None,
    resource_key: str = RESOURCE_KEY,
) -> dict[str, Any]:
    if can_edit or can_upload or can_admin:
        can_view = True
    before = get_grant(conn, user_id, resource_key=resource_key)
    row = client.fetchone(
        conn,
        """
        INSERT INTO auth.resource_grant (
            user_id, resource_key, can_view, can_edit, can_upload, can_admin, granted_by
        )
        VALUES (
            %s::uuid, %s, %s, %s, %s, %s, CAST(%s AS uuid)
        )
        ON CONFLICT (user_id, resource_key) DO UPDATE SET
            can_view = EXCLUDED.can_view,
            can_edit = EXCLUDED.can_edit,
            can_upload = EXCLUDED.can_upload,
            can_admin = EXCLUDED.can_admin,
            granted_by = EXCLUDED.granted_by,
            updated_at = now()
        RETURNING grant_id, user_id, resource_key,
                  can_view, can_edit, can_upload, can_admin,
                  granted_by, created_at, updated_at
        """,
        (user_id, resource_key, can_view, can_edit, can_upload, can_admin, granted_by),
    )
    assert row is not None
    write_audit(
        conn,
        entity_type="grant",
        action="grant_change",
        actor_user_id=granted_by,
        before_json=_jsonable(before),
        after_json=_jsonable(row),
        payment_id=None,
        row_id=None,
    )
    return row


def delete_grant(
    conn: psycopg.Connection,
    *,
    user_id: str,
    actor_user_id: str | None,
    resource_key: str = RESOURCE_KEY,
) -> bool:
    before = get_grant(conn, user_id, resource_key=resource_key)
    if not before.get("grant_id"):
        return False
    client.execute(
        conn,
        """
        DELETE FROM auth.resource_grant
        WHERE user_id = %s::uuid AND resource_key = %s
        """,
        (user_id, resource_key),
    )
    write_audit(
        conn,
        entity_type="grant",
        action="grant_change",
        actor_user_id=actor_user_id,
        before_json=_jsonable(before),
        after_json={**empty_grant(), "user_id": user_id, "resource_key": resource_key},
    )
    return True


def write_audit(
    conn: psycopg.Connection,
    *,
    action: str,
    actor_user_id: str | None,
    entity_type: str = "row",
    row_id: str | None = None,
    payment_id: str | None = None,
    before_json: dict[str, Any] | None = None,
    after_json: dict[str, Any] | None = None,
    upload_batch_id: str | None = None,
    request_id: str | None = None,
) -> None:
    client.execute(
        conn,
        """
        INSERT INTO billing.transaction_tracker_audit (
            entity_type, row_id, payment_id, action, actor_user_id,
            before_json, after_json, upload_batch_id, request_id
        )
        VALUES (
            %s,
            CAST(%s AS uuid),
            %s,
            %s,
            CAST(%s AS uuid),
            %s,
            %s,
            CAST(%s AS uuid),
            %s
        )
        """,
        (
            entity_type,
            row_id,
            payment_id,
            action,
            actor_user_id,
            Json(before_json) if before_json is not None else None,
            Json(after_json) if after_json is not None else None,
            upload_batch_id,
            request_id,
        ),
    )


def get_row(
    conn: psycopg.Connection, row_id: str, *, include_deleted: bool = True
) -> dict[str, Any] | None:
    sql = """
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE row_id = %s::uuid
    """
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return client.fetchone(conn, sql, (row_id,))


def list_rows(
    conn: psycopg.Connection,
    *,
    month: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []

    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    if month:
        start, end = _month_bounds(month)
        clauses.append("txn_date >= %s AND txn_date <= %s")
        params.extend([start, end])
    else:
        if date_from:
            clauses.append("txn_date >= %s")
            params.append(date_from)
        if date_to:
            clauses.append("txn_date <= %s")
            params.append(date_to)

    if q:
        clauses.append(
            """
            (
                payment_id ILIKE %s
                OR COALESCE(description, '') ILIKE %s
                OR COALESCE(eft_1, '') ILIKE %s
                OR COALESCE(eft_2, '') ILIKE %s
                OR COALESCE(collector, '') ILIKE %s
                OR COALESCE(bank_name, '') ILIKE %s
            )
            """
        )
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like, like, like])

    where = " AND ".join(clauses)
    page = max(1, page)
    page_size = min(max(1, page_size), 500)
    offset = (page - 1) * page_size

    total_row = client.fetchone(
        conn,
        f"SELECT count(*)::int AS n FROM billing.transaction_tracker_row WHERE {where}",
        params,
    )
    total = int((total_row or {}).get("n") or 0)

    items = client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE {where}
        ORDER BY txn_date DESC NULLS LAST, payment_id
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def create_row(
    conn: psycopg.Connection,
    data: dict[str, Any],
    *,
    actor_user_id: str | None,
    upload_batch_id: str | None = None,
    action: str = "create",
) -> dict[str, Any]:
    cols = [f for f in ROW_FIELDS if f in data]
    values = [data[f] for f in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)
    row = client.fetchone(
        conn,
        f"""
        INSERT INTO billing.transaction_tracker_row (
            {col_sql}, created_by, updated_by
        )
        VALUES ({placeholders}, CAST(%s AS uuid), CAST(%s AS uuid))
        RETURNING *
        """,
        [*values, actor_user_id, actor_user_id],
    )
    assert row is not None
    write_audit(
        conn,
        action=action,
        actor_user_id=actor_user_id,
        row_id=str(row["row_id"]),
        payment_id=row.get("payment_id"),
        before_json=None,
        after_json=_jsonable(row),
        upload_batch_id=upload_batch_id,
    )
    return row


def update_row(
    conn: psycopg.Connection,
    row_id: str,
    data: dict[str, Any],
    *,
    version: int,
    actor_user_id: str | None,
    upload_batch_id: str | None = None,
    action: str = "update",
) -> dict[str, Any] | None:
    before = get_row(conn, row_id, include_deleted=True)
    if not before or before.get("deleted_at"):
        return None
    if int(before["version"]) != int(version):
        return {"__conflict__": True, "current": before}

    sets = []
    params: list[Any] = []
    for f in ROW_FIELDS:
        if f in data and f != "payment_id":
            sets.append(f"{f} = %s")
            params.append(data[f])
        elif f == "payment_id" and f in data:
            sets.append("payment_id = %s")
            params.append(data[f])
    if not sets:
        return before
    sets.append("version = version + 1")
    sets.append("updated_at = now()")
    sets.append("updated_by = CAST(%s AS uuid)")
    params.append(actor_user_id)
    params.extend([row_id, version])

    row = client.fetchone(
        conn,
        f"""
        UPDATE billing.transaction_tracker_row
        SET {', '.join(sets)}
        WHERE row_id = %s::uuid AND version = %s AND deleted_at IS NULL
        RETURNING *
        """,
        params,
    )
    if row is None:
        current = get_row(conn, row_id)
        return {"__conflict__": True, "current": current}
    write_audit(
        conn,
        action=action,
        actor_user_id=actor_user_id,
        row_id=str(row["row_id"]),
        payment_id=row.get("payment_id"),
        before_json=_jsonable(before),
        after_json=_jsonable(row),
        upload_batch_id=upload_batch_id,
    )
    return row


def soft_delete_row(
    conn: psycopg.Connection,
    row_id: str,
    *,
    version: int,
    actor_user_id: str | None,
    upload_batch_id: str | None = None,
    action: str = "soft_delete",
) -> dict[str, Any] | None:
    before = get_row(conn, row_id, include_deleted=True)
    if not before or before.get("deleted_at"):
        return None
    if int(before["version"]) != int(version):
        return {"__conflict__": True, "current": before}
    row = client.fetchone(
        conn,
        """
        UPDATE billing.transaction_tracker_row
        SET deleted_at = now(),
            deleted_by = CAST(%s AS uuid),
            version = version + 1,
            updated_at = now(),
            updated_by = CAST(%s AS uuid)
        WHERE row_id = %s::uuid AND version = %s AND deleted_at IS NULL
        RETURNING *
        """,
        (actor_user_id, actor_user_id, row_id, version),
    )
    if row is None:
        return {"__conflict__": True, "current": get_row(conn, row_id)}
    write_audit(
        conn,
        action=action,
        actor_user_id=actor_user_id,
        row_id=str(row["row_id"]),
        payment_id=row.get("payment_id"),
        before_json=_jsonable(before),
        after_json=_jsonable(row),
        upload_batch_id=upload_batch_id,
    )
    return row


def restore_row(
    conn: psycopg.Connection,
    row_id: str,
    *,
    version: int,
    actor_user_id: str | None,
) -> dict[str, Any] | None:
    before = get_row(conn, row_id, include_deleted=True)
    if not before or not before.get("deleted_at"):
        return None
    if int(before["version"]) != int(version):
        return {"__conflict__": True, "current": before}

    # Ensure payment_id not taken by another active row
    clash = client.fetchone(
        conn,
        """
        SELECT row_id FROM billing.transaction_tracker_row
        WHERE payment_id = %s AND deleted_at IS NULL AND row_id <> %s::uuid
        """,
        (before["payment_id"], row_id),
    )
    if clash:
        raise ValueError(
            f"Cannot restore: payment_id {before['payment_id']} already active"
        )

    row = client.fetchone(
        conn,
        """
        UPDATE billing.transaction_tracker_row
        SET deleted_at = NULL,
            deleted_by = NULL,
            version = version + 1,
            updated_at = now(),
            updated_by = CAST(%s AS uuid)
        WHERE row_id = %s::uuid AND version = %s AND deleted_at IS NOT NULL
        RETURNING *
        """,
        (actor_user_id, row_id, version),
    )
    if row is None:
        return {"__conflict__": True, "current": get_row(conn, row_id)}
    write_audit(
        conn,
        action="restore",
        actor_user_id=actor_user_id,
        row_id=str(row["row_id"]),
        payment_id=row.get("payment_id"),
        before_json=_jsonable(before),
        after_json=_jsonable(row),
    )
    return row


def row_history(conn: psycopg.Connection, row_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT a.*, u.display_name AS actor_display_name, u.username AS actor_username
        FROM billing.transaction_tracker_audit a
        LEFT JOIN auth.app_user u ON u.user_id = a.actor_user_id
        WHERE a.entity_type = 'row' AND a.row_id = %s::uuid
        ORDER BY a.acted_at DESC
        LIMIT 200
        """,
        (row_id,),
    )


def list_active_for_export(
    conn: psycopg.Connection,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if date_from:
        clauses.append("txn_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("txn_date <= %s")
        params.append(date_to)
    return client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE {' AND '.join(clauses)}
        ORDER BY txn_date, payment_id
        """,
        params,
    )


def list_active_for_etl(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL
        ORDER BY txn_date NULLS LAST, payment_id
        """,
    )


def count_active_rows(conn: psycopg.Connection) -> int:
    row = client.fetchone(
        conn,
        """
        SELECT count(*)::int AS n
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL
        """,
    )
    return int((row or {}).get("n") or 0)


def active_by_payment_ids(
    conn: psycopg.Connection, payment_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not payment_ids:
        return {}
    rows = client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL AND payment_id = ANY(%s)
        """,
        (payment_ids,),
    )
    return {str(r["payment_id"]): r for r in rows}


def active_in_month_ranges(
    conn: psycopg.Connection, bounds: list[tuple[date, date]]
) -> list[dict[str, Any]]:
    if not bounds:
        return []
    clauses = []
    params: list[Any] = []
    for start, end in bounds:
        clauses.append("(txn_date >= %s AND txn_date <= %s)")
        params.extend([start, end])
    return client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL AND ({' OR '.join(clauses)})
        """,
        params,
    )


def _row_snapshot_equal(db_row: dict[str, Any], incoming: dict[str, Any]) -> bool:
    def norm(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, datetime):
            return v.date().isoformat()
        return v

    for f in ROW_FIELDS:
        a = norm(db_row.get(f))
        b = norm(incoming.get(f))
        if a != b:
            return False
    return True


def build_upload_diff(
    conn: psycopg.Connection, parsed_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare parsed file rows to active DB rows in covered month ranges."""
    bounds: list[tuple[date, date]] = []
    months: set[date] = set()
    for r in parsed_rows:
        md = r.get("month_date")
        td = r.get("txn_date")
        if isinstance(md, str):
            from cashflow_db.util import parse_date

            md = parse_date(md)
        if isinstance(td, str):
            from cashflow_db.util import parse_date

            td = parse_date(td)
        anchor = md if isinstance(md, date) else (
            td.replace(day=1) if isinstance(td, date) else None
        )
        if anchor:
            months.add(anchor.replace(day=1))
    for start in sorted(months):
        last = calendar.monthrange(start.year, start.month)[1]
        bounds.append((start, date(start.year, start.month, last)))

    existing = active_in_month_ranges(conn, bounds)
    by_pid = {str(r["payment_id"]): r for r in existing}
    file_pids = {str(r["payment_id"]) for r in parsed_rows}

    adds: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged = 0
    for incoming in parsed_rows:
        pid = str(incoming["payment_id"])
        cur = by_pid.get(pid)
        if not cur:
            adds.append(incoming)
        elif _row_snapshot_equal(cur, incoming):
            unchanged += 1
        else:
            updates.append(
                {
                    "row_id": str(cur["row_id"]),
                    "version": int(cur["version"]),
                    "before": _jsonable(cur),
                    "after": incoming,
                }
            )

    soft_deletes = [
        {
            "row_id": str(r["row_id"]),
            "version": int(r["version"]),
            "payment_id": r["payment_id"],
            "before": _jsonable(r),
        }
        for r in existing
        if str(r["payment_id"]) not in file_pids
    ]

    return {
        "adds": adds,
        "updates": updates,
        "unchanged": unchanged,
        "soft_deletes": soft_deletes,
        "month_bounds": [
            {"from": a.isoformat(), "to": b.isoformat()} for a, b in bounds
        ],
        "counts": {
            "adds": len(adds),
            "updates": len(updates),
            "unchanged": unchanged,
            "soft_deletes": len(soft_deletes),
        },
    }


def save_upload_preview(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    summary: dict[str, Any],
    payload: dict[str, Any],
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    row = client.fetchone(
        conn,
        """
        INSERT INTO billing.transaction_tracker_upload_preview (
            created_by, expires_at, summary_json, payload_json
        )
        VALUES (%s::uuid, %s, %s, %s)
        RETURNING preview_id, created_at, expires_at, summary_json
        """,
        (actor_user_id, expires, Json(summary), Json(payload)),
    )
    assert row is not None
    return row


def get_upload_preview(
    conn: psycopg.Connection, preview_id: str
) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        """
        SELECT *
        FROM billing.transaction_tracker_upload_preview
        WHERE preview_id = %s::uuid
        """,
        (preview_id,),
    )


def delete_upload_preview(conn: psycopg.Connection, preview_id: str) -> None:
    client.execute(
        conn,
        "DELETE FROM billing.transaction_tracker_upload_preview WHERE preview_id = %s::uuid",
        (preview_id,),
    )


def apply_upload_payload(
    conn: psycopg.Connection,
    payload: dict[str, Any],
    *,
    actor_user_id: str,
) -> dict[str, int]:
    batch_id = str(uuid4())
    counts = {"adds": 0, "updates": 0, "soft_deletes": 0}

    for incoming in payload.get("adds") or []:
        create_row(
            conn,
            _coerce_incoming(incoming),
            actor_user_id=actor_user_id,
            upload_batch_id=batch_id,
            action="upload_apply",
        )
        counts["adds"] += 1

    for item in payload.get("updates") or []:
        after = _coerce_incoming(item["after"])
        result = update_row(
            conn,
            item["row_id"],
            after,
            version=int(item["version"]),
            actor_user_id=actor_user_id,
            upload_batch_id=batch_id,
            action="upload_apply",
        )
        if result and not result.get("__conflict__"):
            counts["updates"] += 1
        elif result and result.get("__conflict__"):
            # Re-read and force update by current version for upload commit
            current = result["current"]
            if current:
                result2 = update_row(
                    conn,
                    str(current["row_id"]),
                    after,
                    version=int(current["version"]),
                    actor_user_id=actor_user_id,
                    upload_batch_id=batch_id,
                    action="upload_apply",
                )
                if result2 and not result2.get("__conflict__"):
                    counts["updates"] += 1

    for item in payload.get("soft_deletes") or []:
        result = soft_delete_row(
            conn,
            item["row_id"],
            version=int(item["version"]),
            actor_user_id=actor_user_id,
            upload_batch_id=batch_id,
            action="upload_apply",
        )
        if result and not result.get("__conflict__"):
            counts["soft_deletes"] += 1
        elif result and result.get("__conflict__"):
            current = result["current"]
            if current and not current.get("deleted_at"):
                result2 = soft_delete_row(
                    conn,
                    str(current["row_id"]),
                    version=int(current["version"]),
                    actor_user_id=actor_user_id,
                    upload_batch_id=batch_id,
                    action="upload_apply",
                )
                if result2 and not result2.get("__conflict__"):
                    counts["soft_deletes"] += 1

    return {**counts, "upload_batch_id": batch_id}  # type: ignore[return-value]


def _coerce_incoming(incoming: dict[str, Any]) -> dict[str, Any]:
    from cashflow_db.util import parse_bool, parse_date, parse_money

    out: dict[str, Any] = {}
    for f in ROW_FIELDS:
        if f not in incoming:
            continue
        v = incoming[f]
        if f in ("month_date", "txn_date", "assigned_date"):
            out[f] = parse_date(v) if not isinstance(v, date) else v
        elif f == "amount":
            out[f] = parse_money(v) if not isinstance(v, Decimal) else v
        elif f == "posted":
            out[f] = parse_bool(v) if not isinstance(v, bool) else v
        else:
            out[f] = v
    return out


def import_parsed_rows(
    conn: psycopg.Connection,
    parsed_rows: list[dict[str, Any]],
    *,
    actor_user_id: str | None = None,
) -> dict[str, int]:
    """Seed/upsert from CLI import (no soft-deletes of missing rows)."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    pids = [str(r["payment_id"]) for r in parsed_rows]
    existing = active_by_payment_ids(conn, pids)
    for incoming_raw in parsed_rows:
        incoming = _coerce_incoming(incoming_raw)
        pid = str(incoming["payment_id"])
        cur = existing.get(pid)
        if not cur:
            create_row(conn, incoming, actor_user_id=actor_user_id)
            counts["inserted"] += 1
        elif _row_snapshot_equal(cur, incoming):
            counts["unchanged"] += 1
        else:
            result = update_row(
                conn,
                str(cur["row_id"]),
                incoming,
                version=int(cur["version"]),
                actor_user_id=actor_user_id,
            )
            if result and not result.get("__conflict__"):
                counts["updated"] += 1
            elif result and result.get("__conflict__"):
                current = result["current"]
                if current:
                    update_row(
                        conn,
                        str(current["row_id"]),
                        incoming,
                        version=int(current["version"]),
                        actor_user_id=actor_user_id,
                    )
                    counts["updated"] += 1
    return counts


def available_months(conn: psycopg.Connection) -> list[str]:
    rows = client.fetchall(
        conn,
        """
        SELECT to_char(date_trunc('month', txn_date), 'YYYY-MM') AS month
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL AND txn_date IS NOT NULL
        GROUP BY 1
        ORDER BY 1 DESC
        """,
    )
    return [str(r["month"]) for r in rows if r.get("month")]
