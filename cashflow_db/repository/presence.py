"""Live presence: one ping stream per source, the portal tab and the desk extension.

Each ping books the gap since the same stream's previous ping. Silence books nothing,
so a dropped network or a sleeping laptop reads as no signal, never as idle.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from cashflow_db.repository import client

SOURCE_TAB = "tab"
SOURCE_EXTENSION = "extension"
SOURCES = (SOURCE_TAB, SOURCE_EXTENSION)

STATE_ACTIVE = "active"
STATE_IDLE = "idle"
STATE_LOCKED = "locked"
STATE_UNKNOWN = "unknown"
STATE_PAUSED = "paused"
STATES = (STATE_ACTIVE, STATE_IDLE, STATE_LOCKED, STATE_UNKNOWN, STATE_PAUSED)

STREAM_GAP_SECONDS = 120
FRESH_SECONDS = 150
TAB_OPEN_SECONDS = 90
SLICE_GAP_SECONDS = 120
LOG_KEEP = timedelta(hours=48)
IDLE_GRACE_SECONDS = int(os.environ.get("CASHFLOW_IDLE_GRACE_SECONDS", "300"))
LEGACY_TAB_ID = "legacy"

DESK_PERMISSIONS = frozenset(
    {"watching", "prompt", "denied", "unsupported", "granted_not_watching", "error"}
)

LIVE_WORKING = "working"
LIVE_IDLE = "idle"
LIVE_LOCKED = "locked"
LIVE_UNVERIFIED = "unverified"
LIVE_PAUSED = "paused"
LIVE_SIGNED_OUT = "signed_out"
LIVE_OFFLINE = "offline"
LIVE_ONLINE = frozenset({LIVE_WORKING, LIVE_IDLE, LIVE_LOCKED, LIVE_UNVERIFIED, LIVE_PAUSED})

_STATE_TO_LIVE = {
    STATE_ACTIVE: LIVE_WORKING,
    STATE_IDLE: LIVE_IDLE,
    STATE_LOCKED: LIVE_LOCKED,
    STATE_UNKNOWN: LIVE_UNVERIFIED,
    STATE_PAUSED: LIVE_PAUSED,
}


def _as_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment: Any) -> str | None:
    return _as_aware(moment).isoformat() if isinstance(moment, datetime) else None


def normalize_ping(
    *,
    source: str,
    state: str | None,
    visible: bool | None,
    presence: bool = False,
    closed: bool = False,
) -> dict[str, Any]:
    """Old portal bundles send no state. They only pinged while the person was active."""
    if closed:
        return {"closed": True, "state": None, "visible": False}
    key = (state or "").strip().lower()
    if key not in STATES:
        key = STATE_ACTIVE
    if source == SOURCE_EXTENSION:
        return {"closed": False, "state": key, "visible": False}
    if key == STATE_PAUSED:
        key = STATE_UNKNOWN
    shown = (not presence) if visible is None else bool(visible)
    return {"closed": False, "state": key, "visible": shown}


def stream_gap(last_at: Any, now: datetime) -> int:
    """Seconds since this stream's last ping. A silence over the cap books nothing."""
    if not isinstance(last_at, datetime):
        return 0
    delta = (_as_aware(now) - _as_aware(last_at)).total_seconds()
    if delta <= 0 or delta > STREAM_GAP_SECONDS:
        return 0
    return int(delta)


def is_fresh(last_at: Any, now: datetime, window: int = FRESH_SECONDS) -> bool:
    if not isinstance(last_at, datetime):
        return False
    delta = (_as_aware(now) - _as_aware(last_at)).total_seconds()
    return -FRESH_SECONDS <= delta <= window


def extension_drives_desk(row: dict[str, Any] | None, now: datetime) -> bool:
    """A live, unpaused extension owns desk and idle time. The tab then books portal time only."""
    if not row:
        return False
    return is_fresh(row.get("ext_last_at"), now) and row.get("ext_state") != STATE_PAUSED


def book_ping(
    *,
    source: str,
    state: str,
    visible: bool,
    gap: int,
    away: bool,
    extension_live: bool,
) -> dict[str, int]:
    booked = {"desk": 0, "idle": 0, "unverified": 0, "portal": 0}
    if away or gap <= 0:
        return booked
    if source == SOURCE_EXTENSION:
        if state == STATE_ACTIVE:
            booked["desk"] = gap
        elif state in (STATE_IDLE, STATE_LOCKED):
            booked["idle"] = gap
        return booked
    if visible and state == STATE_ACTIVE:
        booked["portal"] = gap
    if extension_live:
        return booked
    if state == STATE_ACTIVE:
        booked["desk"] = gap
    elif state in (STATE_IDLE, STATE_LOCKED):
        booked["idle"] = gap
    elif state == STATE_UNKNOWN:
        booked["unverified"] = gap
    return booked


def prune_tabs(tabs: dict[str, Any] | None, now: datetime) -> dict[str, str]:
    kept: dict[str, str] = {}
    for tab_id, seen in (tabs or {}).items():
        try:
            stamp = _as_aware(datetime.fromisoformat(str(seen)))
        except ValueError:
            continue
        if (_as_aware(now) - stamp).total_seconds() <= TAB_OPEN_SECONDS:
            kept[str(tab_id)] = stamp.isoformat()
    return kept


def live_status(row: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """What the board shows right now. The extension wins while it is live."""
    moment = _as_aware(now)
    row = row or {}
    ext_at = row.get("ext_last_at")
    tab_at = row.get("tab_last_at")
    if is_fresh(ext_at, moment) and row.get("ext_state") in _STATE_TO_LIVE:
        status = _STATE_TO_LIVE[str(row["ext_state"])]
        return {
            "status": status,
            "since": _iso(row.get("ext_state_since")) or _iso(ext_at),
            "source": SOURCE_EXTENSION,
            "online": True,
        }
    if is_fresh(tab_at, moment) and row.get("tab_state") in _STATE_TO_LIVE:
        status = _STATE_TO_LIVE[str(row["tab_state"])]
        return {
            "status": status,
            "since": _iso(row.get("tab_state_since")) or _iso(tab_at),
            "source": SOURCE_TAB,
            "online": True,
        }
    seen = [_as_aware(stamp) for stamp in (ext_at, tab_at) if isinstance(stamp, datetime)]
    last_seen = max(seen) if seen else None
    if row.get("tab_state") == "closed" and isinstance(row.get("tab_closed_at"), datetime):
        closed_at = _as_aware(row["tab_closed_at"])
        if last_seen is None or closed_at >= last_seen:
            return {
                "status": LIVE_SIGNED_OUT,
                "since": closed_at.isoformat(),
                "source": SOURCE_TAB,
                "online": False,
            }
    return {
        "status": LIVE_OFFLINE,
        "since": last_seen.isoformat() if last_seen else None,
        "source": None,
        "online": False,
    }


def tracker_health(row: dict[str, Any] | None, desk_permission: str | None) -> dict[str, Any]:
    row = row or {}
    extension = None
    if isinstance(row.get("ext_last_at"), datetime):
        extension = {
            "last_at": _iso(row.get("ext_last_at")),
            "state": row.get("ext_state"),
            "version": row.get("ext_version"),
        }
    tab = None
    if isinstance(row.get("tab_last_at"), datetime) or desk_permission:
        tab = {
            "last_at": _iso(row.get("tab_last_at")),
            "permission": desk_permission,
            "permission_at": _iso(row.get("tab_permission_at")),
        }
    return {"extension": extension, "tab": tab}


def last_seen_at(row: dict[str, Any] | None) -> datetime | None:
    row = row or {}
    seen = [
        _as_aware(stamp)
        for stamp in (row.get("ext_last_at"), row.get("tab_last_at"))
        if isinstance(stamp, datetime)
    ]
    return max(seen) if seen else None


def presence_rows(
    conn: psycopg.Connection,
    user_ids: list[str] | None,
) -> dict[str, dict[str, Any]]:
    if user_ids is not None and not user_ids:
        return {}
    where = "WHERE user_id = ANY(%s::uuid[])" if user_ids is not None else ""
    params: tuple[Any, ...] = (user_ids,) if user_ids is not None else ()
    rows = client.fetchall(
        conn,
        f"""
        SELECT user_id, tab_state, tab_state_since, tab_last_at, tab_page, tab_open,
               tab_closed_at, tab_permission_at, ext_state, ext_state_since,
               ext_last_at, ext_version
        FROM ops.user_presence
        {where}
        """,
        params,
    )
    return {str(row["user_id"]): row for row in rows}


def _summary(booked: dict[str, int]) -> str:
    return ",".join(f"{key}:{value}" for key, value in booked.items() if value) or "none"


def _touch_slice(
    conn: psycopg.Connection,
    user_id: str,
    moment: datetime,
    booked: dict[str, int],
    path: str | None,
) -> str:
    last = client.fetchone(
        conn,
        """
        SELECT slice_id, last_ping_at, page_path
        FROM ops.user_activity_slice
        WHERE user_id = %s::uuid
        ORDER BY last_ping_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    extend = False
    if last and isinstance(last.get("last_ping_at"), datetime):
        delta = (moment - _as_aware(last["last_ping_at"])).total_seconds()
        same_page = path is None or not last.get("page_path") or last.get("page_path") == path
        extend = 0 <= delta <= SLICE_GAP_SECONDS and same_page
    if extend and last:
        client.execute(
            conn,
            """
            UPDATE ops.user_activity_slice
            SET last_ping_at = %s,
                seconds_active = seconds_active + %s,
                seconds_desk = seconds_desk + %s,
                seconds_idle = seconds_idle + %s,
                seconds_unverified = seconds_unverified + %s,
                page_path = COALESCE(page_path, %s)
            WHERE slice_id = %s
            """,
            (
                moment,
                booked["portal"],
                booked["desk"],
                booked["idle"],
                booked["unverified"],
                path,
                last["slice_id"],
            ),
        )
        return "extend"
    client.execute(
        conn,
        """
        INSERT INTO ops.user_activity_slice (
            slice_id, user_id, started_at, last_ping_at,
            seconds_active, seconds_desk, seconds_idle, seconds_unverified, page_path
        )
        VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(),
            user_id,
            moment,
            moment,
            booked["portal"],
            booked["desk"],
            booked["idle"],
            booked["unverified"],
            path,
        ),
    )
    return "open"


def _save_presence(conn: psycopg.Connection, user_id: str, merged: dict[str, Any]) -> None:
    client.execute(
        conn,
        """
        INSERT INTO ops.user_presence (
            user_id, tab_state, tab_state_since, tab_last_at, tab_page, tab_open,
            tab_closed_at, tab_permission_at, ext_state, ext_state_since,
            ext_last_at, ext_version, updated_at
        )
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            tab_state = EXCLUDED.tab_state,
            tab_state_since = EXCLUDED.tab_state_since,
            tab_last_at = EXCLUDED.tab_last_at,
            tab_page = EXCLUDED.tab_page,
            tab_open = EXCLUDED.tab_open,
            tab_closed_at = EXCLUDED.tab_closed_at,
            tab_permission_at = EXCLUDED.tab_permission_at,
            ext_state = EXCLUDED.ext_state,
            ext_state_since = EXCLUDED.ext_state_since,
            ext_last_at = EXCLUDED.ext_last_at,
            ext_version = EXCLUDED.ext_version,
            updated_at = EXCLUDED.updated_at
        """,
        (
            user_id,
            merged.get("tab_state"),
            merged.get("tab_state_since"),
            merged.get("tab_last_at"),
            merged.get("tab_page"),
            Jsonb(merged.get("tab_open") or {}),
            merged.get("tab_closed_at"),
            merged.get("tab_permission_at"),
            merged.get("ext_state"),
            merged.get("ext_state_since"),
            merged.get("ext_last_at"),
            merged.get("ext_version"),
            merged.get("updated_at"),
        ),
    )


def _log_ping(
    conn: psycopg.Connection,
    user_id: str,
    moment: datetime,
    *,
    source: str,
    state: str | None,
    tab_id: str | None,
    visible: bool | None,
    client_at: datetime | None,
    booked: str,
) -> None:
    client.execute(
        conn,
        """
        INSERT INTO ops.presence_ping_log (
            user_id, at, source, state, tab_id, visible, client_at, booked
        )
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
        """,
        (user_id, moment, source, state, tab_id, visible, client_at, booked),
    )
    client.execute(
        conn,
        """
        DELETE FROM ops.presence_ping_log
        WHERE user_id = %s::uuid AND at < %s
        """,
        (user_id, moment - LOG_KEEP),
    )


def record_ping(
    conn: psycopg.Connection,
    user_id: str,
    *,
    source: str = SOURCE_TAB,
    state: str | None = None,
    visible: bool | None = None,
    tab_id: str | None = None,
    page_path: str | None = None,
    presence: bool = False,
    closed: bool = False,
    desk_permission: str | None = None,
    client_at: datetime | None = None,
    version: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    from cashflow_db.repository import user_away

    moment = _as_aware(now or datetime.now(timezone.utc))
    if source not in SOURCES:
        raise ValueError("unknown presence source")
    client.execute(conn, "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"presence:{user_id}",))
    row = client.fetchone(
        conn,
        """
        SELECT u.desk_permission, p.tab_state, p.tab_state_since, p.tab_last_at, p.tab_page,
               p.tab_open, p.tab_closed_at, p.tab_permission_at, p.ext_state,
               p.ext_state_since, p.ext_last_at, p.ext_version
        FROM auth.app_user u
        LEFT JOIN ops.user_presence p ON p.user_id = u.user_id
        WHERE u.user_id = %s::uuid
        """,
        (user_id,),
    ) or {}
    merged = dict(row)
    merged.pop("desk_permission", None)
    merged["updated_at"] = moment
    tab_key = (tab_id or "").strip()[:64] or LEGACY_TAB_ID
    ping = normalize_ping(
        source=source, state=state, visible=visible, presence=presence, closed=closed
    )
    grace = {"idle_grace_seconds": IDLE_GRACE_SECONDS}

    if source == SOURCE_TAB:
        permission = (desk_permission or "").strip().lower()
        if permission in DESK_PERMISSIONS and permission != row.get("desk_permission"):
            client.execute(
                conn,
                "UPDATE auth.app_user SET desk_permission = %s WHERE user_id = %s::uuid",
                (permission, user_id),
            )
            merged["tab_permission_at"] = moment

    if ping["closed"]:
        tabs = prune_tabs(row.get("tab_open"), moment)
        tabs.pop(tab_key, None)
        merged["tab_open"] = tabs
        if not tabs:
            merged["tab_state"] = "closed"
            merged["tab_state_since"] = moment
            merged["tab_closed_at"] = moment
        _save_presence(conn, user_id, merged)
        _log_ping(
            conn,
            user_id,
            moment,
            source=source,
            state="closed",
            tab_id=tab_key,
            visible=False,
            client_at=client_at,
            booked="none",
        )
        return {"ok": True, "counted": False, "action": "close", **grace}

    user_away.close_stale_away(conn, user_id=user_id, now=moment)
    away = client.fetchone(
        conn,
        """
        SELECT kind
        FROM ops.user_away
        WHERE user_id = %s::uuid AND ended_at IS NULL
        LIMIT 1
        """,
        (user_id,),
    )
    prefix = "ext" if source == SOURCE_EXTENSION else "tab"
    gap = stream_gap(row.get(f"{prefix}_last_at"), moment)
    booked = book_ping(
        source=source,
        state=ping["state"],
        visible=ping["visible"],
        gap=gap,
        away=bool(away),
        extension_live=source == SOURCE_TAB and extension_drives_desk(row, moment),
    )
    if row.get(f"{prefix}_state") != ping["state"] or not row.get(f"{prefix}_state_since"):
        merged[f"{prefix}_state_since"] = moment
    merged[f"{prefix}_state"] = ping["state"]
    merged[f"{prefix}_last_at"] = moment
    path = None
    if source == SOURCE_TAB:
        tabs = prune_tabs(row.get("tab_open"), moment)
        tabs[tab_key] = moment.isoformat()
        merged["tab_open"] = tabs
        if ping["visible"]:
            path = (page_path or "").strip()[:200] or None
            if path:
                merged["tab_page"] = path
    else:
        merged["ext_version"] = (version or "").strip()[:32] or row.get("ext_version")

    action = "skip"
    if not away and ping["state"] != STATE_PAUSED:
        action = _touch_slice(conn, user_id, moment, booked, path)
    _save_presence(conn, user_id, merged)
    _log_ping(
        conn,
        user_id,
        moment,
        source=source,
        state=("away" if away else ping["state"]),
        tab_id=tab_key if source == SOURCE_TAB else None,
        visible=ping["visible"] if source == SOURCE_TAB else None,
        client_at=client_at,
        booked=_summary(booked),
    )
    return {
        "ok": True,
        "counted": action != "skip",
        "action": action,
        "away": bool(away),
        "add_seconds": booked["portal"],
        "add_desk_seconds": booked["desk"],
        "add_idle_seconds": booked["idle"],
        "add_unverified_seconds": booked["unverified"],
        **grace,
    }


def ping_log(
    conn: psycopg.Connection,
    user_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT at, source, state, tab_id, visible, client_at, booked
        FROM ops.presence_ping_log
        WHERE user_id = %s::uuid
        ORDER BY at DESC
        LIMIT %s
        """,
        (user_id, max(1, min(int(limit), 500))),
    )
