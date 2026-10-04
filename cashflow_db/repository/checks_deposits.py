"""Checks & Deposits rows, grants, audit, and upload preview staging."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Json

from cashflow_db.repository import client

RESOURCE_KEY = "checks_deposits"

ROW_FIELDS = (
    "sheet_key",
    "check_date",
    "payer",
    "amount",
    "check_number",
    "deposit_date",
    "deposit_month",
    "link",
    "notes",
)


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif hasattr(value, "hex"):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def _month_bounds(month: str) -> tuple[date, date]:
    year_s, month_s = month.split("-", 1)
    year, month_n = int(year_s), int(month_s)
    start = date(year, month_n, 1)
    end = date(year, month_n, calendar.monthrange(year, month_n)[1])
    return start, end


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        from cashflow_db.util import parse_date

        return parse_date(value)
    return None


def _with_deposit_month(data: dict[str, Any]) -> dict[str, Any]:
    deposit = _as_date(data.get("deposit_date"))
    if deposit is not None and data.get("deposit_month") in (None, ""):
        data = dict(data)
        data["deposit_month"] = deposit.replace(day=1)
        data["deposit_date"] = deposit
    return data


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
    sheet_key: str | None = None,
    before_json: dict[str, Any] | None = None,
    after_json: dict[str, Any] | None = None,
    upload_batch_id: str | None = None,
    request_id: str | None = None,
) -> None:
    client.execute(
        conn,
        """
        INSERT INTO billing.checks_deposits_audit (
            entity_type, row_id, sheet_key, action, actor_user_id,
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
            sheet_key,
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
        FROM billing.checks_deposits_row
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
        clauses.append("deposit_date >= %s AND deposit_date <= %s")
        params.extend([start, end])
    else:
        if date_from:
            clauses.append("deposit_date >= %s")
            params.append(date_from)
        if date_to:
            clauses.append("deposit_date <= %s")
            params.append(date_to)

    if q:
        clauses.append(
            """
            (
                COALESCE(check_number, '') ILIKE %s
                OR COALESCE(payer, '') ILIKE %s
                OR COALESCE(link, '') ILIKE %s
                OR COALESCE(notes, '') ILIKE %s
                OR sheet_key ILIKE %s
            )
            """
        )
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like, like])

    where = " AND ".join(clauses)
    page = max(1, page)
    page_size = min(max(1, page_size), 500)
    offset = (page - 1) * page_size

    total_row = client.fetchone(
        conn,
        f"""
        SELECT count(*)::int AS n, COALESCE(SUM(amount), 0) AS amount_total
        FROM billing.checks_deposits_row
        WHERE {where}
        """,
        params,
    )
    total = int((total_row or {}).get("n") or 0)
    amount_total = (total_row or {}).get("amount_total") or 0

    items = client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.checks_deposits_row
        WHERE {where}
        ORDER BY deposit_date DESC NULLS LAST, check_number NULLS LAST, sheet_key
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "amount_total": amount_total,
    }


def create_row(
    conn: psycopg.Connection,
    data: dict[str, Any],
    *,
    actor_user_id: str | None,
    upload_batch_id: str | None = None,
    action: str = "create",
) -> dict[str, Any]:
    data = _with_deposit_month(dict(data))
    cols = [f for f in ROW_FIELDS if f in data]
    values = [data[f] for f in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)
    row = client.fetchone(
        conn,
        f"""
        INSERT INTO billing.checks_deposits_row (
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
        sheet_key=row.get("sheet_key"),
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

    data = _with_deposit_month(dict(data))
    sets = []
    params: list[Any] = []
    for field in ROW_FIELDS:
        if field in data:
            sets.append(f"{field} = %s")
            params.append(data[field])
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
        UPDATE billing.checks_deposits_row
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
        sheet_key=row.get("sheet_key"),
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
        UPDATE billing.checks_deposits_row
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
        sheet_key=row.get("sheet_key"),
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

    clash = client.fetchone(
        conn,
        """
        SELECT row_id FROM billing.checks_deposits_row
        WHERE sheet_key = %s AND deleted_at IS NULL AND row_id <> %s::uuid
        """,
        (before["sheet_key"], row_id),
    )
    if clash:
        raise ValueError(
            f"Cannot restore: sheet_key {before['sheet_key']} already active"
        )

    row = client.fetchone(
        conn,
        """
        UPDATE billing.checks_deposits_row
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
        sheet_key=row.get("sheet_key"),
        before_json=_jsonable(before),
        after_json=_jsonable(row),
    )
    return row


def row_history(conn: psycopg.Connection, row_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT a.*, u.display_name AS actor_display_name, u.username AS actor_username
        FROM billing.checks_deposits_audit a
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
        clauses.append("deposit_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("deposit_date <= %s")
        params.append(date_to)
    return client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.checks_deposits_row
        WHERE {' AND '.join(clauses)}
        ORDER BY deposit_date, check_number, sheet_key
        """,
        params,
    )


def active_by_sheet_keys(
    conn: psycopg.Connection, sheet_keys: list[str]
) -> dict[str, dict[str, Any]]:
    if not sheet_keys:
        return {}
    rows = client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.checks_deposits_row
        WHERE deleted_at IS NULL AND sheet_key = ANY(%s)
        """,
        (sheet_keys,),
    )
    return {str(row["sheet_key"]): row for row in rows}


def active_in_month_ranges(
    conn: psycopg.Connection, bounds: list[tuple[date, date]]
) -> list[dict[str, Any]]:
    if not bounds:
        return []
    clauses = []
    params: list[Any] = []
    for start, end in bounds:
        clauses.append("(deposit_date >= %s AND deposit_date <= %s)")
        params.extend([start, end])
    return client.fetchall(
        conn,
        f"""
        SELECT *
        FROM billing.checks_deposits_row
        WHERE deleted_at IS NULL AND ({' OR '.join(clauses)})
        """,
        params,
    )


def _norm_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row_snapshot_equal(db_row: dict[str, Any], incoming: dict[str, Any]) -> bool:
    for field in ROW_FIELDS:
        if _norm_value(db_row.get(field)) != _norm_value(incoming.get(field)):
            return False
    return True


def _covered_bounds(parsed_rows: list[dict[str, Any]]) -> list[tuple[date, date]]:
    months: set[date] = set()
    for row in parsed_rows:
        anchor = _as_date(row.get("deposit_month")) or _as_date(row.get("deposit_date"))
        if anchor:
            months.add(anchor.replace(day=1))
    bounds: list[tuple[date, date]] = []
    for start in sorted(months):
        last = calendar.monthrange(start.year, start.month)[1]
        bounds.append((start, date(start.year, start.month, last)))
    return bounds


def build_upload_diff(
    conn: psycopg.Connection, parsed_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare parsed file rows to active DB rows in covered deposit months."""
    bounds = _covered_bounds(parsed_rows)
    existing = active_in_month_ranges(conn, bounds)
    by_key = {str(row["sheet_key"]): row for row in existing}
    file_keys = {str(row["sheet_key"]) for row in parsed_rows}

    adds: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged = 0
    for incoming in parsed_rows:
        key = str(incoming["sheet_key"])
        current = by_key.get(key)
        if not current:
            adds.append(incoming)
        elif _row_snapshot_equal(current, incoming):
            unchanged += 1
        else:
            updates.append(
                {
                    "row_id": str(current["row_id"]),
                    "version": int(current["version"]),
                    "before": _jsonable(current),
                    "after": incoming,
                }
            )

    soft_deletes = [
        {
            "row_id": str(row["row_id"]),
            "version": int(row["version"]),
            "sheet_key": row["sheet_key"],
            "before": _jsonable(row),
        }
        for row in existing
        if str(row["sheet_key"]) not in file_keys
    ]
    return {
        "adds": adds,
        "updates": updates,
        "unchanged": unchanged,
        "soft_deletes": soft_deletes,
        "month_bounds": [{"from": start.isoformat(), "to": end.isoformat()} for start, end in bounds],
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
        INSERT INTO billing.checks_deposits_upload_preview (
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
        FROM billing.checks_deposits_upload_preview
        WHERE preview_id = %s::uuid
        """,
        (preview_id,),
    )


def delete_upload_preview(conn: psycopg.Connection, preview_id: str) -> None:
    client.execute(
        conn,
        "DELETE FROM billing.checks_deposits_upload_preview WHERE preview_id = %s::uuid",
        (preview_id,),
    )


def apply_upload_payload(
    conn: psycopg.Connection,
    payload: dict[str, Any],
    *,
    actor_user_id: str,
) -> dict[str, Any]:
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
            current = result.get("current")
            if current and not current.get("deleted_at"):
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
            current = result.get("current")
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

    return {**counts, "upload_batch_id": batch_id}


def _coerce_incoming(incoming: dict[str, Any]) -> dict[str, Any]:
    from cashflow_db.util import parse_date, parse_money

    out: dict[str, Any] = {}
    for field in ROW_FIELDS:
        if field not in incoming:
            continue
        value = incoming[field]
        if field in ("check_date", "deposit_date", "deposit_month"):
            out[field] = value if isinstance(value, date) else parse_date(value)
        elif field == "amount":
            out[field] = value if isinstance(value, Decimal) else parse_money(value)
        else:
            out[field] = value
    deposit = out.get("deposit_date")
    if isinstance(deposit, date) and out.get("deposit_month") is None:
        out["deposit_month"] = deposit.replace(day=1)
    return out


def list_active_sheet_keys(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT row_id, sheet_key, version, deleted_at
        FROM billing.checks_deposits_row
        WHERE deleted_at IS NULL
        """,
    )


def import_parsed_rows(
    conn: psycopg.Connection,
    parsed_rows: list[dict[str, Any]],
    *,
    actor_user_id: str | None = None,
    replace_missing: bool = False,
) -> dict[str, int]:
    """Seed/upsert from CLI import.

    When ``replace_missing`` is True, soft-delete active rows whose sheet_key
    is not present in ``parsed_rows``.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "soft_deleted": 0}
    keys = [str(row["sheet_key"]) for row in parsed_rows]
    incoming_keys = set(keys)
    existing = active_by_sheet_keys(conn, keys)
    for incoming_raw in parsed_rows:
        incoming = _coerce_incoming(incoming_raw)
        key = str(incoming["sheet_key"])
        current = existing.get(key)
        if not current:
            create_row(conn, incoming, actor_user_id=actor_user_id)
            counts["inserted"] += 1
        elif _row_snapshot_equal(current, incoming):
            counts["unchanged"] += 1
        else:
            result = update_row(
                conn,
                str(current["row_id"]),
                incoming,
                version=int(current["version"]),
                actor_user_id=actor_user_id,
            )
            if result and not result.get("__conflict__"):
                counts["updated"] += 1
            elif result and result.get("__conflict__"):
                refreshed = result.get("current")
                if refreshed and not refreshed.get("deleted_at"):
                    update_row(
                        conn,
                        str(refreshed["row_id"]),
                        incoming,
                        version=int(refreshed["version"]),
                        actor_user_id=actor_user_id,
                    )
                    counts["updated"] += 1
    if replace_missing:
        for row in list_active_sheet_keys(conn):
            key = str(row.get("sheet_key") or "")
            if not key or key in incoming_keys:
                continue
            result = soft_delete_row(
                conn,
                str(row["row_id"]),
                version=int(row["version"]),
                actor_user_id=actor_user_id,
            )
            if result and not result.get("__conflict__"):
                counts["soft_deleted"] += 1
            elif result and result.get("__conflict__"):
                refreshed = result.get("current") if isinstance(result, dict) else None
                if refreshed and not refreshed.get("deleted_at"):
                    result2 = soft_delete_row(
                        conn,
                        str(refreshed["row_id"]),
                        version=int(refreshed["version"]),
                        actor_user_id=actor_user_id,
                    )
                    if result2 and not result2.get("__conflict__"):
                        counts["soft_deleted"] += 1
    return counts


def available_months(conn: psycopg.Connection) -> list[str]:
    rows = client.fetchall(
        conn,
        """
        SELECT to_char(date_trunc('month', deposit_date), 'YYYY-MM') AS month
        FROM billing.checks_deposits_row
        WHERE deleted_at IS NULL AND deposit_date IS NOT NULL
        GROUP BY 1
        ORDER BY 1 DESC
        """,
    )
    return [str(row["month"]) for row in rows if row.get("month")]
