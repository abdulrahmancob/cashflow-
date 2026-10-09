"""Login lockout: too many failures for one account, or from one address, pause sign-in."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

log = logging.getLogger(__name__)

from cashflow_db.repository import client

WINDOW = timedelta(minutes=15)
USER_FAILURES = 5
IP_FAILURES = 30
KEEP = timedelta(days=7)


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()[:254]


def failures_since_success(rows: list[dict[str, Any]]) -> list[datetime]:
    """Failure times, newest first, up to the most recent success."""
    failed: list[datetime] = []
    for row in rows:
        if row.get("ok"):
            break
        stamp = row.get("at")
        if isinstance(stamp, datetime):
            failed.append(_as_aware(stamp))
    return failed


def lock_seconds(
    user_rows: list[dict[str, Any]],
    ip_failures: int,
    now: datetime,
) -> int:
    """Seconds until sign-in opens again, or 0 when it is open."""
    moment = _as_aware(now)
    failed = failures_since_success(user_rows)
    wait = 0
    if len(failed) >= USER_FAILURES:
        opens = failed[USER_FAILURES - 1] + WINDOW
        wait = max(wait, int((opens - moment).total_seconds()))
    if ip_failures >= IP_FAILURES:
        wait = max(wait, int(WINDOW.total_seconds()))
    return max(0, wait)


def _table_missing() -> None:
    log.warning("auth.login_attempt is missing; login lockout is off until migrations run")


def check(conn: psycopg.Connection, username: str, ip: str | None, now: datetime) -> int:
    """Seconds left on a lockout. Sign-in stays open if the table is not there yet."""
    since = _as_aware(now) - WINDOW
    ip_failures = 0
    try:
        with conn.transaction():
            user_rows = client.fetchall(
                conn,
                """
                SELECT at, ok
                FROM auth.login_attempt
                WHERE username = %s AND at > %s
                ORDER BY at DESC
                LIMIT 50
                """,
                (normalize_username(username), since),
            )
            if ip:
                row = client.fetchone(
                    conn,
                    """
                    SELECT count(*)::int AS n
                    FROM auth.login_attempt
                    WHERE ip = %s AND NOT ok AND at > %s
                    """,
                    (ip, since),
                )
                ip_failures = int((row or {}).get("n") or 0)
    except psycopg.errors.UndefinedTable:
        _table_missing()
        return 0
    return lock_seconds(user_rows, ip_failures, now)


def record(
    conn: psycopg.Connection,
    username: str,
    ip: str | None,
    *,
    ok: bool,
    now: datetime,
) -> None:
    try:
        with conn.transaction():
            client.execute(
                conn,
                "INSERT INTO auth.login_attempt (username, ip, at, ok) VALUES (%s, %s, %s, %s)",
                (normalize_username(username), (ip or None), now, ok),
            )
            if ok:
                client.execute(
                    conn,
                    "DELETE FROM auth.login_attempt WHERE at < %s",
                    (_as_aware(now) - KEEP,),
                )
    except psycopg.errors.UndefinedTable:
        _table_missing()
