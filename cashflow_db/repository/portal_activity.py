"""Unified portal activity log (one row per save, grouped by entity in UI)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Json

from cashflow_db.repository import client

ACTIONS = (
    "created",
    "updated",
    "deleted",
    "assigned",
    "restored",
    "uploaded",
    "published",
)
AREAS = (
    "eligibility",
    "collection",
    "second_submission",
    "tfl",
    "cpt_guide",
    "cpt_audit",
    "tracker",
    "users",
)
SKIP_DIFF_KEYS = frozenset(
    {
        "updated_at",
        "updated_by",
        "updated_by_name",
        "locked_by",
        "locked_by_name",
        "lock_expires_at",
        "password_hash",
        "password",
        "version",
        "context",
        "__conflict__",
        "current",
    }
)
FIELD_LABELS = {
    "paid_amount": "paid amount",
    "client_payment": "client payment",
    "insurance_payment": "insurance payment",
    "updated_payment": "updated payment",
    "coinsurance_payment": "coinsurance",
    "charged_amount": "charged",
    "eligibility_status": "status",
    "assigned_to": "assignee",
    "assigned_to_name": "assignee",
    "patient_name": "patient",
    "insurance_name": "insurance",
    "second_insurance": "second insurance",
    "second_submission": "second submission",
    "workload_status": "workload status",
    "workload_note": "note",
    "tfl_days": "TFL days",
    "roles": "roles",
    "workflow_status": "workflow",
    "resolution_code": "resolution",
    "resolution_note": "resolution note",
    "ignore_reason": "ignore reason",
    "display_name": "name",
    "is_active": "active",
    "check_number": "check number",
    "check_date": "check date",
    "payment_id": "payment",
    "claim_number": "claim",
    "aliases": "aliases",
    "denial_reason": "denial reason",
    "root_cause": "root cause",
    "actions_taken": "actions taken",
    "collection_status": "collection status",
    "work_date": "work date",
    "label": "value",
    "can_view": "view",
    "can_edit": "edit",
    "can_upload": "upload",
    "can_admin": "admin",
    "amount": "amount",
}


def pretty_value(value: Any) -> str:
    if value is None or value == "":
        return "empty"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10] if not isinstance(value, datetime) else value.isoformat()[:19]
    if isinstance(value, Decimal):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(pretty_value(v) for v in value) or "empty"
    text = str(value).strip()
    return text or "empty"


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name, name.replace("_", " "))


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def field_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    keys: list[str] | None = None,
    skip: set[str] | None = None,
) -> list[dict[str, str]]:
    old = before or {}
    new = after or {}
    ignored = SKIP_DIFF_KEYS | (skip or set())
    names = keys or sorted(set(old) | set(new))
    out: list[dict[str, str]] = []
    for name in names:
        if name in ignored or name.startswith("_"):
            continue
        left = _norm(old.get(name))
        right = _norm(new.get(name))
        if left == right:
            continue
        if pretty_value(left) == pretty_value(right):
            continue
        out.append(
            {
                "name": name,
                "label": field_label(name),
                "old": pretty_value(left),
                "new": pretty_value(right),
            }
        )
    return out


def summarize(action: str, fields: list[dict[str, str]], fallback: str) -> str:
    if action == "created":
        return fallback if fallback.lower().startswith("created") else f"Created {fallback}"
    if action == "deleted":
        return fallback if fallback.lower().startswith("deleted") else f"Deleted {fallback}"
    if action == "assigned":
        hit = next((f for f in fields if f["name"] in {"assigned_to", "assigned_to_name"}), None)
        if hit:
            return f"Assigned to {hit['new']}"
        return "Changed assignee"
    if action == "restored":
        return "Restored"
    if action == "uploaded":
        return fallback
    if action == "published":
        return fallback
    if len(fields) == 1:
        f = fields[0]
        return f"Updated {f['label']} {f['old']} → {f['new']}"
    if fields:
        labels = ", ".join(f["label"] for f in fields[:4])
        extra = f" +{len(fields) - 4}" if len(fields) > 4 else ""
        return f"Updated {len(fields)} fields ({labels}{extra})"
    return fallback


def work_item_label(item: dict[str, Any] | None, *, area: str = "eligibility") -> str:
    row = item or {}
    name = str(row.get("patient_name") or "Visit").strip() or "Visit"
    dos = row.get("dos") or row.get("service_date")
    dos_s = pretty_value(dos) if dos not in (None, "") else ""
    tag = "Collection" if area == "collection" else "Eligibility"
    if dos_s and dos_s != "empty":
        return f"{name} · {dos_s} · {tag}"
    return f"{name} · {tag}"


def pr_flag_id(revflow_patient_id: str, dos: Any, kind: str) -> str:
    return f"{revflow_patient_id}|{pretty_value(dos)}|{kind}"


def tracker_row_label(row: dict[str, Any] | None) -> str:
    item = row or {}
    pid = str(item.get("payment_id") or "Tracker row").strip() or "Tracker row"
    when = item.get("txn_date") or item.get("month_date")
    when_s = pretty_value(when) if when not in (None, "") else ""
    if when_s and when_s != "empty":
        return f"{pid} · {when_s} · Tracker"
    return f"{pid} · Tracker"


def checks_deposits_row_label(row: dict[str, Any] | None) -> str:
    item = row or {}
    number = str(item.get("check_number") or item.get("sheet_key") or "Check").strip() or "Check"
    when = item.get("deposit_date")
    when_s = pretty_value(when) if when not in (None, "") else ""
    if when_s and when_s != "empty":
        return f"{number} · {when_s} · Checks & Deposits"
    return f"{number} · Checks & Deposits"


def user_label(row: dict[str, Any] | None) -> str:
    item = row or {}
    name = str(item.get("display_name") or item.get("username") or "User").strip()
    return f"{name} · Users"


def entity_href(entity_type: str, entity_id: str, area: str) -> str | None:
    mapping = {
        "work_item": "/eligibility" if area != "collection" else "/collection",
        "pr_flag": "/second-submission",
        "tfl_rule": "/second-submission",
        "guide": "/cpt-guide",
        "cpt_audit_item": "/cpt-audit",
        "insights": "/finance/insights",
        "tracker_row": "/tracker",
        "tracker_grant": "/tracker",
        "tracker_upload": "/tracker",
        "checks_deposits_row": "/checks-deposits",
        "checks_deposits_grant": "/checks-deposits",
        "checks_deposits_upload": "/checks-deposits",
        "user": "/users",
        "sheet": "/eligibility",
    }
    return mapping.get(entity_type)


def record_activity(
    conn: psycopg.Connection,
    *,
    actor_user_id: str | None,
    action: str,
    area: str,
    entity_type: str,
    entity_id: str,
    entity_label: str,
    summary: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if action not in ACTIONS:
        raise ValueError(f"unknown activity action: {action}")
    if not entity_id or not entity_label or not summary:
        return None
    payload = details or {}
    row = client.fetchone(
        conn,
        """
        INSERT INTO ops.portal_activity (
            actor_user_id, action, area, entity_type, entity_id,
            entity_label, summary, details
        )
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING *
        """,
        (
            actor_user_id,
            action,
            area,
            entity_type,
            str(entity_id),
            str(entity_label)[:240],
            str(summary)[:400],
            Json(payload),
        ),
    )
    return dict(row) if row else None


def record_from_diff(
    conn: psycopg.Connection,
    *,
    actor_user_id: str | None,
    action: str,
    area: str,
    entity_type: str,
    entity_id: str,
    entity_label: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    skip: set[str] | None = None,
    extra: dict[str, Any] | None = None,
    fallback: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    fields = field_changes(before, after, keys=keys, skip=skip)
    if action == "updated" and not fields and not force:
        return None
    summary = summarize(action, fields, fallback or entity_label)
    details: dict[str, Any] = {"fields": fields}
    if extra:
        details.update(extra)
    return record_activity(
        conn,
        actor_user_id=actor_user_id,
        action=action,
        area=area,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        summary=summary,
        details=details,
    )


def group_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[tuple[str, str]] = []
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in events:
        key = (str(ev.get("entity_type") or ""), str(ev.get("entity_id") or ""))
        if key not in buckets:
            order.append(key)
            buckets[key] = {
                "entity_type": key[0],
                "entity_id": key[1],
                "entity_label": ev.get("entity_label") or key[1],
                "area": ev.get("area"),
                "href": entity_href(key[0], key[1], str(ev.get("area") or "")),
                "last_at": ev.get("occurred_at"),
                "event_count": 0,
                "events": [],
            }
        group = buckets[key]
        group["events"].append(ev)
        group["event_count"] = len(group["events"])
        group["last_at"] = group["last_at"] or ev.get("occurred_at")
    return [buckets[k] for k in order]


def list_activity(
    conn: psycopg.Connection,
    *,
    actor_user_id: str | None = None,
    area: str | None = None,
    action: str | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 400,
) -> dict[str, Any]:
    where = ["TRUE"]
    params: list[Any] = []
    if actor_user_id:
        where.append("a.actor_user_id = %s::uuid")
        params.append(actor_user_id)
    if area:
        where.append("a.area = %s")
        params.append(area)
    if action:
        where.append("a.action = %s")
        params.append(action)
    if date_from:
        where.append("a.occurred_at >= %s::date")
        params.append(date_from)
    if date_to:
        where.append("a.occurred_at < (%s::date + interval '1 day')")
        params.append(date_to)
    needle = (q or "").strip()
    if needle:
        where.append(
            "(a.entity_label ILIKE %s OR a.summary ILIKE %s OR a.entity_id ILIKE %s)"
        )
        like = f"%{needle}%"
        params.extend([like, like, like])
    cap = max(1, min(int(limit or 400), 800))
    rows = client.fetchall(
        conn,
        f"""
        SELECT
            a.activity_id,
            a.occurred_at,
            a.actor_user_id,
            a.action,
            a.area,
            a.entity_type,
            a.entity_id,
            a.entity_label,
            a.summary,
            a.details,
            u.display_name AS actor_name,
            u.username AS actor_username
        FROM ops.portal_activity a
        LEFT JOIN auth.app_user u ON u.user_id = a.actor_user_id
        WHERE {' AND '.join(where)}
        ORDER BY a.occurred_at DESC
        LIMIT %s
        """,
        (*params, cap),
    )
    events = [dict(r) for r in rows]
    actors = client.fetchall(
        conn,
        """
        SELECT DISTINCT u.user_id, u.display_name, u.username
        FROM ops.portal_activity a
        JOIN auth.app_user u ON u.user_id = a.actor_user_id
        ORDER BY u.display_name
        """,
    )
    return {
        "groups": group_events(events),
        "actors": [dict(r) for r in actors],
        "count": len(events),
    }
