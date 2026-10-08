"""Away sessions: a daily break pool, prayer slots, and meetings."""

from __future__ import annotations

import calendar
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
RED_BOARD_ROLES = ("red_agent",)


def cairo_today(moment: datetime | None = None) -> date:
    now = _as_aware(moment or datetime.now(timezone.utc))
    return now.astimezone(CAIRO_TZ).date()


def cairo_day_bounds(day: date) -> tuple[datetime, datetime]:
    """A Cairo calendar day as a half-open timestamp range, so indexes on the column apply."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=CAIRO_TZ)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=CAIRO_TZ)
    return start, end


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
        "auto_closed": bool(row.get("auto_closed")),
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
               planned_seconds, with_whom, work_day, auto_closed
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
    close_stale_away(conn, user_id=user_id, now=moment)
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
    close_stale_away(conn, user_id=user_id, now=moment)
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


STALE_BREAK_GRACE_SECONDS = 30 * 60
STALE_MEETING_GRACE_SECONDS = 60 * 60


def stale_cap(row: dict[str, Any]) -> datetime | None:
    """When a forgotten session is closed: past its limit, and never past its own day."""
    started = row.get("started_at")
    if not isinstance(started, datetime):
        return None
    start = _as_aware(started)
    kind = row.get("kind")
    if kind == KIND_BREAK:
        cap = start + timedelta(seconds=BREAK_BUDGET_SECONDS + STALE_BREAK_GRACE_SECONDS)
    elif kind == KIND_PRAYER:
        cap = start + timedelta(seconds=2 * PRAYER_MAX_SECONDS)
    else:
        planned = int(row.get("planned_seconds") or 0)
        cap = start + timedelta(seconds=planned + STALE_MEETING_GRACE_SECONDS)
    day = _as_date(row.get("work_day")) or start.astimezone(CAIRO_TZ).date()
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=CAIRO_TZ)
    return max(start, min(cap, day_end))


def close_stale_away(
    conn: psycopg.Connection,
    *,
    user_id: str | None = None,
    user_ids: list[str] | None = None,
    now: datetime | None = None,
) -> int:
    """End forgotten sessions at their cap so nobody stays away into the next day."""
    moment = _as_aware(now or datetime.now(timezone.utc))
    ids = [user_id] if user_id else user_ids
    if ids is not None and not ids:
        return 0
    extra, params = _id_clause(ids)
    rows = client.fetchall(
        conn,
        f"""
        SELECT away_id, kind, started_at, planned_seconds, work_day
        FROM ops.user_away
        WHERE ended_at IS NULL{extra}
        """,
        params,
    )
    closed = 0
    for row in rows:
        cap = stale_cap(row)
        if cap is None or cap > moment:
            continue
        client.execute(
            conn,
            """
            UPDATE ops.user_away
            SET ended_at = %s, auto_closed = true
            WHERE away_id = %s AND ended_at IS NULL
            """,
            (cap, row["away_id"]),
        )
        closed += 1
    return closed


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
        "desk_permission": user.get("desk_permission") or None,
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
    logins: list[dict[str, Any]] | None = None,
    presence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Selected-day cards stay on that day. Live is whoever is away right now."""
    from cashflow_db.repository import presence as live_presence

    moment = _as_aware(now)
    is_today = selected == today
    present = set(online_ids or set())
    seen_at = dict(last_pings or {})
    if presence is not None:
        for user in users:
            key = str(user["user_id"])
            seen = live_presence.last_seen_at(presence.get(key))
            if seen is not None:
                seen_at[key] = seen
            if live_presence.live_status(presence.get(key), moment)["online"]:
                present.add(key)
    logged_in: dict[str, str] = {}
    for row in logins or []:
        stamp = row.get("logged_in_at")
        uid = row.get("user_id")
        if uid is None or not isinstance(stamp, datetime):
            continue
        logged_in[str(uid)] = _as_aware(stamp).isoformat()
    desk_by_user = {
        str(row["user_id"]): {
            "seconds_desk": int(row.get("seconds_desk") or 0),
            "seconds_idle": int(row.get("seconds_idle") or 0),
            "seconds_unverified": int(row.get("seconds_unverified") or 0),
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
        portal_open = is_today and uid in present
        live = None
        if presence is not None and is_today:
            live = live_presence.live_status(presence.get(uid), moment)
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
                "seconds_unverified": desk_by_user.get(uid, {}).get("seconds_unverified", 0),
                "live_status": live["status"] if live else None,
                "live_since": live["since"] if live else None,
                "live_source": live["source"] if live else None,
                "tracker": live_presence.tracker_health(
                    (presence or {}).get(uid), user.get("desk_permission") or None
                ),
                "logged_in_at": logged_in.get(uid),
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
    live_rows = []
    for row in open_sessions:
        user = active.get(str(row.get("user_id")))
        if not user or row.get("ended_at") is not None:
            continue
        public = _public_session(row, moment)
        live_rows.append(
            {
                "user_id": str(user["user_id"]),
                "display_name": _user_fields(user)["display_name"],
                "kind": public["kind"],
                "elapsed_seconds": public["elapsed_seconds"],
                "with_whom": public["with_whom"],
                "started_at": public["started_at"],
            }
        )
    live_rows.sort(key=lambda item: str(item["display_name"]).casefold())
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
        "live": live_rows,
        "days": days,
    }


def board_scope_roles(viewer_roles: list[str] | None) -> tuple[str, ...] | None:
    """None means the whole company. A lead without an admin role sees their team."""
    keys = set(viewer_roles or [])
    if keys & ADMIN_BOARD_ROLES:
        return None
    if "second_submission_lead" in keys:
        return SS_BOARD_ROLES
    if "redteam_leader" in keys:
        return RED_BOARD_ROLES
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


def person_in_board_scope(
    conn: psycopg.Connection,
    viewer_roles: list[str] | None,
    target_id: str,
) -> bool:
    from cashflow_db.repository.auth_users import list_users

    scope = board_scope_roles(viewer_roles)
    return any(
        str(row["user_id"]) == str(target_id)
        for row in users_in_board_scope(list_users(conn), scope)
    )


def my_presence(
    conn: psycopg.Connection,
    user_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What the board shows for this person right now, and which tracker feeds it."""
    from cashflow_db.repository import presence as live_presence

    moment = _as_aware(now or datetime.now(timezone.utc))
    row = live_presence.presence_rows(conn, [user_id]).get(str(user_id))
    permission = client.fetchone(
        conn,
        "SELECT desk_permission FROM auth.app_user WHERE user_id = %s::uuid",
        (user_id,),
    )
    live = live_presence.live_status(row, moment)
    return {
        **live,
        "tracker": live_presence.tracker_health(
            row, (permission or {}).get("desk_permission") or None
        ),
        "idle_grace_seconds": live_presence.IDLE_GRACE_SECONDS,
    }


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
    from cashflow_db.repository import presence as live_presence

    users = users_in_board_scope(list_users(conn), role_keys)
    scoped_ids = [str(row["user_id"]) for row in users]
    close_stale_away(conn, user_ids=scoped_ids, now=moment)
    day_start, day_end = cairo_day_bounds(selected)
    day_extra, day_params = _id_clause(scoped_ids)
    day_sessions = client.fetchall(
        conn,
        f"""
        SELECT away_id, user_id, kind, started_at, ended_at,
               planned_seconds, with_whom, work_day, auto_closed
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
               planned_seconds, with_whom, work_day, auto_closed
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
    ping_extra, ping_params = _id_clause(scoped_ids)
    pings = client.fetchall(
        conn,
        f"""
        SELECT user_id, max(last_ping_at) AS last_ping_at
        FROM ops.user_activity_slice
        WHERE last_ping_at >= %s{ping_extra}
        GROUP BY user_id
        """,
        (moment - timedelta(days=1), *ping_params),
    )
    activity_extra, activity_params = _id_clause(scoped_ids)
    activity = client.fetchall(
        conn,
        f"""
        SELECT user_id,
               COALESCE(SUM(seconds_desk), 0)::int AS seconds_desk,
               COALESCE(SUM(seconds_idle), 0)::int AS seconds_idle,
               COALESCE(SUM(seconds_unverified), 0)::int AS seconds_unverified
        FROM ops.user_activity_slice
        WHERE started_at >= %s AND started_at < %s{activity_extra}
        GROUP BY user_id
        """,
        (day_start, day_end, *activity_params),
    )
    login_extra, login_params = _id_clause(scoped_ids)
    logins = client.fetchall(
        conn,
        f"""
        SELECT user_id, min(logged_in_at) AS logged_in_at
        FROM auth.login_event
        WHERE logged_in_at >= %s AND logged_in_at < %s{login_extra}
        GROUP BY user_id
        """,
        (day_start, day_end, *login_params),
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
        logins=logins,
        presence=live_presence.presence_rows(conn, scoped_ids),
    )


PAGE_LABELS = {
    "/eligibility": "Eligibility",
    "/second-submission": "Second submission",
    "/patient-responsibility": "Patient responsibility",
    "/collection": "Collection",
    "/analytics": "Analytics",
    "/billing-analysis": "Billing analysis",
    "/cpt-guide": "CPT guide",
    "/cpt-audit": "CPT audit",
    "/tracker": "Tracker",
    "/checks-deposits": "Checks and deposits",
    "/my-day": "My day",
    "/away": "Away board",
    "/activity": "Activity",
    "/users": "Users",
    "/database": "Database",
    "/platform": "Platform",
    "/login": "Login",
}

AREA_LABELS = {
    "eligibility": "Eligibility",
    "collection": "Collection",
    "second_submission": "Second submission",
    "tfl": "TFL",
    "cpt_guide": "CPT guide",
    "cpt_audit": "CPT audit",
    "tracker": "Tracker",
    "users": "Users",
}

KIND_LABELS = {
    KIND_BREAK: "Break",
    KIND_PRAYER: "Prayer",
    KIND_MEETING: "Meeting",
}

_DAY_COLUMNS = (
    ("date", "Date"),
    ("name", "Name"),
    ("username", "Username"),
    ("roles", "Roles"),
    ("first_login", "First login"),
    ("last_login", "Last login"),
    ("logins", "Logins"),
    ("hours_desk", "At computer (hours)"),
    ("hours_idle", "Idle (hours)"),
    ("hours_unverified", "Unverified (hours)"),
    ("hours_portal", "On portal (hours)"),
    ("break_minutes", "Break (minutes)"),
    ("break_budget_minutes", "Break budget (minutes)"),
    ("over_break", "Over break"),
    ("prayers", "Prayers"),
    ("meetings", "Meetings"),
    ("meeting_minutes", "Meeting (minutes)"),
    ("pages", "Pages"),
)
_SESSION_COLUMNS = (
    ("date", "Date"),
    ("name", "Name"),
    ("username", "Username"),
    ("kind", "Kind"),
    ("started", "Started"),
    ("ended", "Ended"),
    ("minutes", "Minutes"),
    ("planned_minutes", "Planned (minutes)"),
    ("with_whom", "With whom"),
    ("still_open", "Still open"),
    ("auto_closed", "Auto-closed"),
)
_WORK_COLUMNS = (
    ("date", "Date"),
    ("name", "Name"),
    ("username", "Username"),
    ("page", "Page"),
    ("started", "From"),
    ("ended", "To"),
    ("hours_desk", "At computer (hours)"),
    ("hours_idle", "Idle (hours)"),
    ("hours_unverified", "Unverified (hours)"),
    ("hours_portal", "On portal (hours)"),
)
_CHANGE_COLUMNS = (
    ("date", "Date"),
    ("time", "Time"),
    ("name", "Name"),
    ("username", "Username"),
    ("area", "Area"),
    ("action", "Action"),
    ("item", "Item"),
    ("summary", "Summary"),
)
_MONTH_COLUMNS = (
    ("month", "Month"),
    ("period_from", "From"),
    ("period_to", "To"),
    ("name", "Name"),
    ("username", "Username"),
    ("roles", "Roles"),
    ("days_in_period", "Days in period"),
    ("days_present", "Days present"),
    ("days_absent", "Days absent"),
    ("hours_desk", "At computer (hours)"),
    ("hours_idle", "Idle (hours)"),
    ("hours_unverified", "Unverified (hours)"),
    ("hours_portal", "On portal (hours)"),
    ("hours_desk_average", "Average at computer (hours)"),
    ("break_minutes", "Break (minutes)"),
    ("days_over_break", "Days over break"),
    ("prayers", "Prayers"),
    ("meetings", "Meetings"),
    ("meeting_minutes", "Meeting (minutes)"),
    ("top_page", "Top page"),
    ("top_page_hours", "Top page (hours)"),
)


def page_label(path: str | None) -> str:
    raw = (path or "").split("?", 1)[0].strip()
    if len(raw) > 1:
        raw = raw.rstrip("/")
    if not raw:
        return "Unknown"
    if raw.startswith("/finance"):
        return "Finance"
    return PAGE_LABELS.get(raw, raw)


def _area_label(area: str | None) -> str:
    key = (area or "").strip()
    if not key:
        return ""
    if key in AREA_LABELS:
        return AREA_LABELS[key]
    return " ".join(part.capitalize() for part in key.replace("-", " ").replace("_", " ").split())


def _hours(seconds: int) -> float:
    return round(int(seconds) / 3600, 2)


def _minutes(seconds: int) -> float:
    return round(int(seconds) / 60, 1)


def _cairo_stamp(moment: datetime) -> str:
    return _as_aware(moment).astimezone(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")


def _cairo_clock(moment: datetime) -> str:
    return _as_aware(moment).astimezone(CAIRO_TZ).strftime("%H:%M")


def _cairo_day(moment: datetime) -> date:
    return _as_aware(moment).astimezone(CAIRO_TZ).date()


def _yes(flag: bool) -> str:
    return "Yes" if flag else "No"


def _people_index(users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for user in users:
        fields = _user_fields(user)
        indexed[fields["user_id"]] = {
            "user_id": fields["user_id"],
            "name": fields["display_name"],
            "username": fields["username"],
            "roles": ", ".join(fields["roles"]),
        }
    return indexed


def _month_period(year: int, month: int, today: date) -> tuple[date, date]:
    start = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    end = min(last, today) if (year, month) == (today.year, today.month) else last
    if end < start:
        end = start
    return start, end


def _pages_line(rows: list[dict[str, Any]]) -> str:
    totals: dict[str, float] = {}
    for row in rows:
        hours = float(row.get("hours_desk") or 0)
        if hours <= 0:
            continue
        label = str(row.get("page") or "Unknown")
        totals[label] = round(totals.get(label, 0) + hours, 2)
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{label} {hours:.2f}h" for label, hours in ranked)


def _top_page(rows: list[dict[str, Any]]) -> tuple[str, float]:
    desk: dict[str, float] = {}
    portal: dict[str, float] = {}
    for row in rows:
        label = str(row.get("page") or "Unknown")
        desk[label] = round(desk.get(label, 0) + float(row.get("hours_desk") or 0), 2)
        portal[label] = round(portal.get(label, 0) + float(row.get("hours_portal") or 0), 2)
    totals = desk if any(value > 0 for value in desk.values()) else portal
    if not totals:
        return "", 0
    label, hours = min(totals.items(), key=lambda item: (-item[1], item[0]))
    return label, hours


def assemble_board_export(
    users: list[dict[str, Any]],
    *,
    sessions: list[dict[str, Any]],
    slices: list[dict[str, Any]],
    logins: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """One row set for every Cairo day, plus a month rollup of those rows."""
    moment = _as_aware(now or datetime.now(timezone.utc))
    today = cairo_today(moment)
    people = _people_index(users)
    work: list[dict[str, Any]] = []
    for row in slices:
        person = people.get(str(row.get("user_id")))
        started = row.get("started_at")
        if person is None or not isinstance(started, datetime):
            continue
        desk = int(row.get("seconds_desk") or 0)
        idle = int(row.get("seconds_idle") or 0)
        portal = int(row.get("seconds_active") or 0)
        unverified = int(row.get("seconds_unverified") or 0)
        if desk == 0 and idle == 0 and portal == 0 and unverified == 0:
            continue
        ended = row.get("last_ping_at")
        work.append(
            {
                **person,
                "date": _cairo_day(started).isoformat(),
                "page": page_label(row.get("page_path")),
                "started": _cairo_stamp(started),
                "ended": _cairo_stamp(ended) if isinstance(ended, datetime) else "",
                "hours_desk": _hours(desk),
                "hours_idle": _hours(idle),
                "hours_unverified": _hours(unverified),
                "hours_portal": _hours(portal),
            }
        )
    work_by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    work_by_month: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in work:
        work_by_day.setdefault((row["user_id"], row["date"]), []).append(row)
        work_by_month.setdefault((row["user_id"], row["date"][:7]), []).append(row)

    logins_by_day: dict[tuple[str, str], list[datetime]] = {}
    for row in logins:
        person_id = str(row.get("user_id") or "")
        stamp = row.get("logged_in_at")
        if person_id not in people or not isinstance(stamp, datetime):
            continue
        logins_by_day.setdefault((person_id, _cairo_day(stamp).isoformat()), []).append(stamp)

    sessions_by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    session_rows: list[dict[str, Any]] = []
    for row in sessions:
        person = people.get(str(row.get("user_id")))
        day = _as_date(row.get("work_day"))
        started = row.get("started_at")
        if person is None or day is None or not isinstance(started, datetime):
            continue
        day_key = day.isoformat()
        sessions_by_day.setdefault((person["user_id"], day_key), []).append(row)
        ended = row.get("ended_at")
        still_open = not isinstance(ended, datetime)
        planned = row.get("planned_seconds")
        session_rows.append(
            {
                **person,
                "date": day_key,
                "kind": KIND_LABELS.get(str(row.get("kind")), str(row.get("kind") or "")),
                "started": _cairo_stamp(started),
                "ended": "" if still_open else _cairo_stamp(ended),
                "minutes": _minutes(_row_seconds(row, moment)),
                "planned_minutes": "" if planned in (None, "") else _minutes(int(planned)),
                "with_whom": row.get("with_whom") or "",
                "still_open": _yes(still_open),
                "auto_closed": _yes(bool(row.get("auto_closed"))),
            }
        )

    day_keys = set(logins_by_day) | set(sessions_by_day)
    day_keys.update((row["user_id"], row["date"]) for row in work)
    days: list[dict[str, Any]] = []
    for person_id, day_key in day_keys:
        person = people[person_id]
        stamps = logins_by_day.get((person_id, day_key), [])
        summary = summarize_day(sessions_by_day.get((person_id, day_key), []), now=moment)
        day_work = work_by_day.get((person_id, day_key), [])
        meetings = summary["meetings"]
        days.append(
            {
                **person,
                "date": day_key,
                "first_login": _cairo_clock(min(stamps)) if stamps else "",
                "last_login": _cairo_clock(max(stamps)) if stamps else "",
                "logins": len(stamps),
                "hours_desk": round(sum(row["hours_desk"] for row in day_work), 2),
                "hours_idle": round(sum(row["hours_idle"] for row in day_work), 2),
                "hours_unverified": round(sum(row["hours_unverified"] for row in day_work), 2),
                "hours_portal": round(sum(row["hours_portal"] for row in day_work), 2),
                "break_minutes": _minutes(int(summary["break_seconds"])),
                "break_budget_minutes": _minutes(BREAK_BUDGET_SECONDS),
                "over_break": _yes(int(summary["break_seconds"]) > BREAK_BUDGET_SECONDS),
                "prayers": int(summary["prayer_count"]),
                "meetings": len(meetings),
                "meeting_minutes": round(
                    sum(_minutes(int(item["elapsed_seconds"])) for item in meetings),
                    1,
                ),
                "pages": _pages_line(day_work),
            }
        )

    change_rows: list[dict[str, Any]] = []
    for row in changes:
        person = people.get(str(row.get("user_id") or row.get("actor_user_id") or ""))
        stamp = row.get("occurred_at")
        if person is None or not isinstance(stamp, datetime):
            continue
        local = _as_aware(stamp).astimezone(CAIRO_TZ)
        action = str(row.get("action") or "")
        change_rows.append(
            {
                **person,
                "date": local.date().isoformat(),
                "time": local.strftime("%H:%M"),
                "area": _area_label(row.get("area")),
                "action": action.replace("_", " ").capitalize(),
                "item": row.get("entity_label") or "",
                "summary": row.get("summary") or "",
            }
        )

    monthly: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in days:
        grouped.setdefault((row["user_id"], row["date"][:7]), []).append(row)
    for (person_id, month), rows in grouped.items():
        person = people[person_id]
        year_text, month_text = month.split("-", 1)
        start, end = _month_period(int(year_text), int(month_text), today)
        in_period = [row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()]
        if not in_period:
            continue
        present = len(in_period)
        period_days = (end - start).days + 1
        desk = round(sum(row["hours_desk"] for row in in_period), 2)
        month_work = [
            row
            for row in work_by_month.get((person_id, month), [])
            if start.isoformat() <= row["date"] <= end.isoformat()
        ]
        top_page, top_hours = _top_page(month_work)
        monthly.append(
            {
                **person,
                "month": month,
                "period_from": start.isoformat(),
                "period_to": end.isoformat(),
                "days_in_period": period_days,
                "days_present": present,
                "days_absent": max(0, period_days - present),
                "hours_desk": desk,
                "hours_idle": round(sum(row["hours_idle"] for row in in_period), 2),
                "hours_unverified": round(sum(row["hours_unverified"] for row in in_period), 2),
                "hours_portal": round(sum(row["hours_portal"] for row in in_period), 2),
                "hours_desk_average": round(desk / present, 2) if present else 0,
                "break_minutes": round(sum(row["break_minutes"] for row in in_period), 1),
                "days_over_break": sum(1 for row in in_period if row["over_break"] == "Yes"),
                "prayers": sum(int(row["prayers"]) for row in in_period),
                "meetings": sum(int(row["meetings"]) for row in in_period),
                "meeting_minutes": round(sum(row["meeting_minutes"] for row in in_period), 1),
                "top_page": top_page,
                "top_page_hours": top_hours,
            }
        )

    days.sort(key=lambda row: str(row["name"]).casefold())
    days.sort(key=lambda row: row["date"], reverse=True)
    session_rows.sort(key=lambda row: (row["date"], row["started"]), reverse=True)
    work.sort(key=lambda row: (row["name"].casefold(), row["started"]))
    work.sort(key=lambda row: row["date"], reverse=True)
    change_rows.sort(key=lambda row: (row["date"], row["time"]), reverse=True)
    monthly.sort(key=lambda row: str(row["name"]).casefold())
    monthly.sort(key=lambda row: row["month"], reverse=True)
    return {
        "days": days,
        "sessions": session_rows,
        "work": work,
        "changes": change_rows,
        "monthly": monthly,
    }


def export_tables(
    payload: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, list[str], list[list[Any]]]]:
    specs = (
        ("Days", _DAY_COLUMNS, payload.get("days") or []),
        ("Sessions", _SESSION_COLUMNS, payload.get("sessions") or []),
        ("Work", _WORK_COLUMNS, payload.get("work") or []),
        ("Changes", _CHANGE_COLUMNS, payload.get("changes") or []),
        ("Monthly", _MONTH_COLUMNS, payload.get("monthly") or []),
    )
    tables: list[tuple[str, list[str], list[list[Any]]]] = []
    for title, columns, rows in specs:
        headers = [label for _, label in columns]
        body = [[row.get(key, "") for key, _ in columns] for row in rows]
        tables.append((title, headers, body))
    return tables


def away_board_export(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    role_keys: tuple[str, ...] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    from cashflow_db.repository.auth_users import list_users

    moment = _as_aware(now or datetime.now(timezone.utc))
    users = users_in_board_scope(list_users(conn), role_keys)
    ids = [str(row["user_id"]) for row in users]
    if not ids:
        return assemble_board_export(
            users,
            sessions=[],
            slices=[],
            logins=[],
            changes=[],
            now=moment,
        )
    sessions = client.fetchall(
        conn,
        """
        SELECT away_id, user_id, kind, started_at, ended_at,
               planned_seconds, with_whom, work_day, auto_closed
        FROM ops.user_away
        WHERE user_id = ANY(%s::uuid[])
        ORDER BY started_at
        """,
        (ids,),
    )
    slices = client.fetchall(
        conn,
        """
        SELECT user_id, started_at, last_ping_at, seconds_desk, seconds_idle,
               seconds_unverified, seconds_active, page_path
        FROM ops.user_activity_slice
        WHERE user_id = ANY(%s::uuid[])
        """,
        (ids,),
    )
    logins = client.fetchall(
        conn,
        """
        SELECT user_id, logged_in_at
        FROM auth.login_event
        WHERE user_id = ANY(%s::uuid[])
        """,
        (ids,),
    )
    changes = client.fetchall(
        conn,
        """
        SELECT actor_user_id AS user_id, occurred_at, action, area,
               entity_label, summary
        FROM ops.portal_activity
        WHERE actor_user_id = ANY(%s::uuid[])
        ORDER BY occurred_at
        """,
        (ids,),
    )
    return assemble_board_export(
        users,
        sessions=sessions,
        slices=slices,
        logins=logins,
        changes=changes,
        now=moment,
    )
