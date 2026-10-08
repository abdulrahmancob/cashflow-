"""Desk tracker devices: one random token per paired browser, stored only as a hash."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import psycopg

from cashflow_db.repository import client

MIN_PING_GAP = timedelta(seconds=5)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_device(
    conn: psycopg.Connection,
    user_id: str,
    *,
    label: str | None = None,
    user_agent: str | None = None,
) -> dict[str, str]:
    """The token is returned once, here. The database only ever sees its hash."""
    token = secrets.token_urlsafe(32)
    device_id = uuid4()
    client.execute(
        conn,
        """
        INSERT INTO auth.desk_device (device_id, user_id, token_hash, label, user_agent)
        VALUES (%s, %s::uuid, %s, %s, %s)
        """,
        (
            device_id,
            user_id,
            hash_token(token),
            ((label or "").strip()[:80] or None),
            ((user_agent or "").strip()[:300] or None),
        ),
    )
    return {"device_id": str(device_id), "token": token}


def list_devices(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT device_id, label, user_agent, created_at, last_seen_at
        FROM auth.desk_device
        WHERE user_id = %s::uuid AND revoked_at IS NULL
        ORDER BY created_at DESC
        """,
        (user_id,),
    )


def device_owner(conn: psycopg.Connection, device_id: str) -> str | None:
    row = client.fetchone(
        conn,
        "SELECT user_id FROM auth.desk_device WHERE device_id = %s::uuid AND revoked_at IS NULL",
        (device_id,),
    )
    return str(row["user_id"]) if row else None


def revoke_device(conn: psycopg.Connection, device_id: str, *, now: datetime | None = None) -> None:
    client.execute(
        conn,
        """
        UPDATE auth.desk_device
        SET revoked_at = %s
        WHERE device_id = %s::uuid AND revoked_at IS NULL
        """,
        (now or datetime.now(timezone.utc), device_id),
    )


def device_for_token(conn: psycopg.Connection, token: str) -> dict[str, Any] | None:
    """A live device of an active person, or None. Revoked or deactivated means no access."""
    if not token:
        return None
    return client.fetchone(
        conn,
        """
        SELECT d.device_id, d.user_id, d.last_seen_at, u.display_name
        FROM auth.desk_device d
        JOIN auth.app_user u ON u.user_id = d.user_id
        WHERE d.token_hash = %s
          AND d.revoked_at IS NULL
          AND u.is_active
        """,
        (hash_token(token),),
    )


def too_soon(last_seen_at: Any, now: datetime) -> bool:
    if not isinstance(last_seen_at, datetime):
        return False
    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    return timedelta(0) <= now - seen < MIN_PING_GAP


def touch_device(conn: psycopg.Connection, device_id: str, now: datetime) -> None:
    client.execute(
        conn,
        "UPDATE auth.desk_device SET last_seen_at = %s WHERE device_id = %s::uuid",
        (now, device_id),
    )
