"""Away sessions: a daily break pool, prayer slots, and meetings."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

from cashflow_db.repository import client

CAIRO_TZ = ZoneInfo("Africa/Cairo")

KIND_BREAK = "break"
KIND_PRAYER = "prayer"
KIND_MEETING = "meeting"
KINDS = (KIND_BREAK, KIND_PRAYER, KIND_MEETING)

BREAK_BUDGET_SECONDS = 45 * 60
ONLINE_SECONDS = 150
PRAYER_MAX_SECONDS = 10 * 60
PRAYER_LIMIT = 3
WARN_WITHIN_SECONDS = 5 * 60
PRAYER_WARN_WITHIN_SECONDS = 2 * 60
MEETING_MAX_SECONDS = 24 * 60 * 60
ADMIN_BOARD_ROLES = frozenset({"super_admin", "sub_admin", "ops_admin"})
SS_BOARD_ROLES = ("second_submission", "second_submission_lead")


def cairo_today(moment: datetime | None = None) -> date:
    now = _as_aware(moment or datetime.now(timezone.utc))
    return now.astimezone(CAIRO_TZ).date()


def _as_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def elapsed_seconds(
    started_at: datetime,
    ended_at: datetime | None,
    now: datetime,
) -> int:
    start = _as_aware(started_at)
    end = _as_aware(ended_at or now)
    return max(0, int((end - start).total_seconds()))


def validate_start(
    *,
    kind: str,
    planned_minutes: int | None,
    with_whom: str | None,
    has_open: bool,
    prayer_count: int,
) -> dict[str, Any]:
    key = (kind or "").strip().lower()
    if key not in KINDS:
        raise ValueError("kind must be break, prayer, or meeting")
    if has_open:
        raise ValueError("finish the current break before starting another")
    whom = " ".join((with_whom or "").split())
    if key == KIND_BREAK:
        return {"kind": key, "planned_seconds": None, "with_whom": None}
    if key == KIND_PRAYER:
        if int(prayer_count) >= PRAYER_LIMIT:
            raise ValueError("only 3 prayers per day")
        return {"kind": key, "planned_seconds": PRAYER_MAX_SECONDS, "with_whom": None}
    if planned_minutes is None or int(planned_minutes) < 1:
        raise ValueError("meeting needs a duration")
    minutes = int(planned_minutes)
    if minutes * 60 > MEETING_MAX_SECONDS:
        raise ValueError("meeting duration is too long")
    if not whom:
        raise ValueError("meeting needs who it is with")
    return {"kind": key, "planned_seconds": minutes * 60, "with_whom": whom[:200]}


def warning_level(
    *,
    open_kind: str | None,
    open_elapsed: int,
    open_planned: int | None,
    break_seconds_closed: int,
) -> str:
    """none, soon, or over. Red only while a session is still open."""
    if not open_kind:
        return "none"
    if open_kind == KIND_BREAK:
        remaining = BREAK_BUDGET_SECONDS - (int(break_seconds_closed) + int(open_elapsed))
        if remaining <= 0:
            return "over"
        if remaining <= WARN_WITHIN_SECONDS:
            return "soon"
        return "none"
    planned = int(open_planned or 0)
    if planned <= 0:
        return "none"
    remaining = planned - int(open_elapsed)
    if remaining <= 0:
        return "over"
    threshold = (
        PRAYER_WARN_WITHIN_SECONDS if open_kind == KIND_PRAYER else WARN_WITHIN_SECONDS
    )
    if remaining <= threshold:
        return "soon"
    return "none"


def _row_seconds(row: dict[str, Any], now: datetime) -> int:
    started = row.get("started_at")
    if not isinstance(started, datetime):
        return 0
    ended = row.get("ended_at")
    if ended is not None and not isinstance(ended, datetime):
        return 0
    return elapsed_seconds(started, ended if isinstance(ended, datetime) else None, now)


def _public_session(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "away_id": str(row["away_id"]),
        "kind": row["kind"],
        "started_at": _as_aware(row["started_at"]).isoformat(),
        "ended_at": (
            _as_aware(row["ended_at"]).isoformat() if row.get("ended_at") else None
        ),
        "planned_seconds": row.get("planned_seconds"),
        "with_whom": row.get("with_whom"),
        "elapsed_seconds": _row_seconds(row, now),
        "work_day": row["work_day"].isoformat()
        if isinstance(row.get("work_day"), date)
        else str(row.get("work_day") or ""),
    }


def _open_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if row.get("ended_at") is None:
            return row
    return None


def summarize_day(rows: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    moment = _as_aware(now or datetime.now(timezone.utc))
    closed_break = 0
    prayer_count = 0
    meetings: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for row in rows:
        public = _public_session(row, moment)
        sessions.append(public)
        kind = row.get("kind")
        if kind == KIND_BREAK and row.get("ended_at") is not None:
            closed_break += public["elapsed_seconds"]
        elif kind == KIND_PRAYER:
            prayer_count += 1
        elif kind == KIND_MEETING:
            meetings.append(public)
    open_row = _open_row(rows)
    open_public = _public_session(open_row, moment) if open_row else None
    open_elapsed = int(open_public["elapsed_seconds"]) if open_public else 0
    if open_public and open_public["kind"] == KIND_BREAK:
        break_seconds = closed_break + open_elapsed
    else:
        break_seconds = closed_break
    level = warning_level(
        open_kind=open_public["kind"] if open_public else None,
        open_elapsed=open_elapsed,
        open_planned=open_public.get("planned_seconds") if open_public else None,
        break_seconds_closed=closed_break,
    )
    return {
        "open": open_public,
        "break_seconds": break_seconds,
        "break_seconds_closed": closed_break,
        "break_budget_seconds": BREAK_BUDGET_SECONDS,
        "break_remaining_seconds": max(0, BREAK_BUDGET_SECONDS - break_seconds),
        "prayer_count": prayer_count,
        "prayer_limit": PRAYER_LIMIT,
        "prayer_max_seconds": PRAYER_MAX_SECONDS,
        "warn_within_seconds": WARN_WITHIN_SECONDS,
        "prayer_warn_within_seconds": PRAYER_WARN_WITHIN_SECONDS,
        "meetings": meetings,
        "sessions": sessions,
        "warning": level,
        "work_day": cairo_today(moment).isoformat(),
    }


def _day_rows(
    conn: psycopg.Connection,
    user_id: str,
    work_day: date,
) -> list[dict[str, Any]]:
    """Today's sessions, plus an open session that started on an earlier day."""
    return client.fetchall(
        conn,
        """
        SELECT away_id, user_id, kind, started_at, ended_at,
               planned_seconds, with_whom, work_day
        FROM ops.user_away
        WHERE user_id = %s::uuid
          AND (work_day = %s OR ended_at IS NULL)
        ORDER BY started_at
        """,
        (user_id, work_day),
    )


def my_away(
    conn: psycopg.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_aware(now or datetime.now(timezone.utc))
    rows = _day_rows(conn, user_id, cairo_today(moment))
    return summarize_day(rows, now=moment)


def start_away(
    conn: psycopg.Connection,
    user_id: str,
    *,
    kind: str,
    planned_minutes: int | None = None,
    with_whom: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_aware(now or datetime.now(timezone.utc))
    day = cairo_today(moment)
    rows = _day_rows(conn, user_id, day)
    spec = validate_start(
        kind=kind,
        planned_minutes=planned_minutes,
        with_whom=with_whom,
        has_open=_open_row(rows) is not None,
        prayer_count=sum(1 for row in rows if row.get("kind") == KIND_PRAYER),
    )
    away_id = uuid4()
    try:
        client.execute(
            conn,
            """
            INSERT INTO ops.user_away (
                away_id, user_id, kind, started_at, planned_seconds, with_whom, work_day
            )
            VALUES (%s, %s::uuid, %s, %s, %s, %s, %s)
            """,
            (
                away_id,
                user_id,
                spec["kind"],
                moment,
                spec["planned_seconds"],
                spec["with_whom"],
                day,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ValueError("finish the current break before starting another") from exc
    return my_away(conn, user_id, now=moment)


def end_away(
    conn: psycopg.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_aware(now or datetime.now(timezone.utc))
    row = client.fetchone(
        conn,
        """
        SELECT away_id
        FROM ops.user_away
        WHERE user_id = %s::uuid
          AND ended_at IS NULL
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if not row:
        raise ValueError("not away")
    client.execute(
        conn,
        """
        UPDATE ops.user_away
        SET ended_at = %s
        WHERE away_id = %s
          AND ended_at IS NULL
        """,
        (moment, row["away_id"]),
    )
    return my_away(conn, user_id, now=moment)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value[:10])
    return None


def filter_rows_for_day(rows: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """Sessions credited to this work day. An open session from another day stays out."""
    return [row for row in rows if _as_date(row.get("work_day")) == day]


def aggregate_away_days(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest day first. People is the count of distinct users who stepped away."""
    people: dict[date, set[str]] = {}
    for row in rows:
        day = _as_date(row.get("work_day"))
        uid = row.get("user_id")
        if day is None or uid is None:
            continue
        people.setdefault(day, set()).add(str(uid))
    return [
        {"work_day": day.isoformat(), "people": len(ids)}
        for day, ids in sorted(people.items(), reverse=True)
    ]


def _user_fields(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(user["user_id"]),
        "display_name": user.get("display_name") or user.get("username") or "",
        "username": user.get("username") or "",
        "roles": list(user.get("roles") or []),
    }


def offline_since_at(
    ping: datetime | None,
    *,
    now: datetime,
    online: bool,
    away: bool,
    is_today: bool,
) -> str | None:
    """Live gap after today's last ping, until the portal opens again."""
    if not is_today or online or away or ping is None:
        return None
    moment = _as_aware(now)
    seen = _as_aware(ping)
    if seen > moment or cairo_today(seen) != cairo_today(moment):
        return None
    return seen.isoformat()


def online_user_ids(
    pings: list[dict[str, Any]],
    now: datetime,
    *,
    window_seconds: int = ONLINE_SECONDS,
) -> set[str]:
    """Users whose portal heartbeat landed inside the open-tab window."""
    cutoff = _as_aware(now) - timedelta(seconds=window_seconds)
    online: set[str] = set()
    for row in pings:
        ping = row.get("last_ping_at")
        uid = row.get("user_id")
        if uid is None or not isinstance(ping, datetime):
            continue
        if _as_aware(ping) >= cutoff:
            online.add(str(uid))
    return online


def assemble_board(
    users: list[dict[str, Any]],
    *,
    day_sessions: list[dict[str, Any]],
    open_sessions: list[dict[str, Any]],
    day_counts: list[dict[str, Any]],
    selected: date,
    today: date,
    now: datetime,
    online_ids: set[str] | None = None,
    activity: list[dict[str, Any]] | None = None,
    last_pings: dict[str, datetime] | None = None,
) -> dict[str, Any]:
    """Selected-day cards stay on that day. Live is whoever is away right now."""
    moment = _as_aware(now)
    is_today = selected == today
    present = online_ids or set()
    seen_at = last_pings or {}
    desk_by_user = {
        str(row["user_id"]): {
            "seconds_desk": int(row.get("seconds_desk") or 0),
            "seconds_idle": int(row.get("seconds_idle") or 0),
        }
        for row in activity or []
    }
    scoped = filter_rows_for_day(day_sessions, selected)
    by_user: dict[str, list[dict[str, Any]]] = {}
    for row in scoped:
        by_user.setdefault(str(row["user_id"]), []).append(row)
    active = {str(user["user_id"]): user for user in users}
    people = []
    for user in users:
        uid = str(user["user_id"])
        summary = summarize_day(by_user.get(uid, []), now=moment)
        open_public = summary["open"]
        show_open = bool(is_today and open_public)
        if not show_open:
            summary["open"] = None
            summary["warning"] = "none"
        summary["work_day"] = selected.isoformat()
        away_now = bool(show_open)
        portal_open = uid in present
        people.append(
            {
                **_user_fields(user),
                "status": open_public["kind"] if away_now else "working",
                "online": portal_open,
                "offline_since": offline_since_at(
                    seen_at.get(uid),
                    now=moment,
                    online=portal_open,
                    away=away_now,
                    is_today=is_today,
                ),
                "seconds_desk": desk_by_user.get(uid, {}).get("seconds_desk", 0),
                "seconds_idle": desk_by_user.get(uid, {}).get("seconds_idle", 0),
                **summary,
            }
        )

    def _order(person: dict[str, Any]) -> tuple[Any, ...]:
        if is_today and person["status"] != "working":
            group = 0
        elif person["online"]:
            group = 1
        else:
            group = 2
        return (group, str(person["display_name"]).casefold())

    people.sort(key=_order)
    live = []
    for row in open_sessions:
        user = active.get(str(row.get("user_id")))
        if not user or row.get("ended_at") is not None:
            continue
        public = _public_session(row, moment)
        live.append(
            {
                "user_id": str(user["user_id"]),
                "display_name": _user_fields(user)["display_name"],
                "kind": public["kind"],
                "elapsed_seconds": public["elapsed_seconds"],
                "with_whom": public["with_whom"],
                "started_at": public["started_at"],
            }
        )
    live.sort(key=lambda item: str(item["display_name"]).casefold())
    days = []
    for row in day_counts:
        day = _as_date(row.get("work_day"))
        if day is None:
            continue
        days.append({"work_day": day.isoformat(), "people": int(row.get("people") or 0)})
    days.sort(key=lambda item: item["work_day"], reverse=True)
    return {
        "work_day": selected.isoformat(),
        "is_today": is_today,
        "break_budget_seconds": BREAK_BUDGET_SECONDS,
        "prayer_limit": PRAYER_LIMIT,
        "people": people,
        "live": live,
        "days": days,
    }


def board_scope_roles(viewer_roles: list[str] | None) -> tuple[str, ...] | None:
    """None means the whole company. A lead without an admin role sees their team."""
    keys = set(viewer_roles or [])
    if keys & ADMIN_BOARD_ROLES:
        return None
    if "second_submission_lead" in keys:
        return SS_BOARD_ROLES
    raise PermissionError("insufficient away board scope")


def users_in_board_scope(
    users: list[dict[str, Any]],
    role_keys: tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    active = [
        row
        for row in users
        if row.get("is_active")
        and not ADMIN_BOARD_ROLES.intersection(row.get("roles") or [])
    ]
    if role_keys is None:
        return active
    allowed = set(role_keys)
    return [row for row in active if allowed.intersection(row.get("roles") or [])]


def _id_clause(
    user_ids: list[str] | None,
    *,
    leading_and: bool = True,
) -> tuple[str, tuple[Any, ...]]:
    if user_ids is None:
        return "", ()
    prefix = " AND " if leading_and else ""
    return f"{prefix}user_id = ANY(%s::uuid[])", (user_ids,)


def away_board(
    conn: psycopg.Connection,
    *,
    day: date | None = None,
    now: datetime | None = None,
    role_keys: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    from cashflow_db.repository.auth_users import list_users

    moment = _as_aware(now or datetime.now(timezone.utc))
    today = cairo_today(moment)
    selected = day or today
    users = users_in_board_scope(list_users(conn), role_keys)
    scoped_ids = [str(row["user_id"]) for row in users]
    day_extra, day_params = _id_clause(scoped_ids)
    day_sessions = client.fetchall(
        conn,
        f"""
        SELECT away_id, user_id, kind, started_at, ended_at,
               planned_seconds, with_whom, work_day
        FROM ops.user_away
        WHERE work_day = %s{day_extra}
        ORDER BY started_at
        """,
        (selected, *day_params),
    )
    open_extra, open_params = _id_clause(scoped_ids)
    open_sessions = client.fetchall(
        conn,
        f"""
        SELECT away_id, user_id, kind, started_at, ended_at,
               planned_seconds, with_whom, work_day
        FROM ops.user_away
        WHERE ended_at IS NULL{open_extra}
        ORDER BY started_at
        """,
        open_params,
    )
    count_extra, count_params = _id_clause(scoped_ids)
    day_counts = client.fetchall(
        conn,
        f"""
        SELECT work_day, count(DISTINCT user_id)::int AS people
        FROM ops.user_away
        WHERE TRUE{count_extra}
        GROUP BY work_day
        ORDER BY work_day DESC
        """,
        count_params,
    )
    ping_extra, ping_params = _id_clause(scoped_ids, leading_and=False)
    ping_where = f"WHERE {ping_extra}" if ping_extra else ""
    pings = client.fetchall(
        conn,
        f"""
        SELECT user_id, max(last_ping_at) AS last_ping_at
        FROM ops.user_activity_slice
        {ping_where}
        GROUP BY user_id
        """,
        ping_params,
    )
    activity_extra, activity_params = _id_clause(scoped_ids)
    activity = client.fetchall(
        conn,
        f"""
        SELECT user_id,
               COALESCE(SUM(seconds_desk), 0)::int AS seconds_desk,
               COALESCE(SUM(seconds_idle), 0)::int AS seconds_idle
        FROM ops.user_activity_slice
        WHERE (started_at AT TIME ZONE 'Africa/Cairo')::date = %s{activity_extra}
        GROUP BY user_id
        """,
        (selected, *activity_params),
    )
    last_pings: dict[str, datetime] = {}
    for row in pings:
        ping = row.get("last_ping_at")
        uid = row.get("user_id")
        if uid is None or not isinstance(ping, datetime):
            continue
        last_pings[str(uid)] = _as_aware(ping)
    return assemble_board(
        users,
        day_sessions=day_sessions,
        open_sessions=open_sessions,
        day_counts=day_counts,
        selected=selected,
        today=today,
        now=moment,
        online_ids=online_user_ids(pings, moment),
        activity=activity,
        last_pings=last_pings,
    )
