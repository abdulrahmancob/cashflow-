"""Portal auth users and roles."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from cashflow_db.repository import client


def list_roles(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        "SELECT role_id, role_key, display_name FROM auth.role ORDER BY role_key",
    )


def get_user_by_username(conn: psycopg.Connection, username: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        """
        SELECT user_id, username, email, password_hash, display_name,
               is_active, created_at, updated_at, last_login_at
        FROM auth.app_user
        WHERE lower(username) = lower(%s)
        """,
        (username,),
    )


def get_user_by_id(conn: psycopg.Connection, user_id: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        """
        SELECT user_id, username, email, password_hash, display_name,
               is_active, created_at, updated_at, last_login_at
        FROM auth.app_user
        WHERE user_id = %s::uuid
        """,
        (user_id,),
    )


def get_user_roles(conn: psycopg.Connection, user_id: str) -> list[str]:
    rows = client.fetchall(
        conn,
        """
        SELECT r.role_key
        FROM auth.user_role ur
        JOIN auth.role r ON r.role_id = ur.role_id
        WHERE ur.user_id = %s::uuid
        ORDER BY r.role_key
        """,
        (user_id,),
    )
    return [str(r["role_key"]) for r in rows]


def list_users(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = client.fetchall(
        conn,
        """
        SELECT u.user_id, u.username, u.email, u.display_name, u.is_active,
               u.created_at, u.updated_at, u.last_login_at,
               COALESCE(
                   array_agg(r.role_key ORDER BY r.role_key)
                   FILTER (WHERE r.role_key IS NOT NULL),
                   '{}'
               ) AS roles
        FROM auth.app_user u
        LEFT JOIN auth.user_role ur ON ur.user_id = u.user_id
        LEFT JOIN auth.role r ON r.role_id = ur.role_id
        GROUP BY u.user_id
        ORDER BY u.username
        """,
    )
    for row in rows:
        roles = row.get("roles") or []
        if isinstance(roles, str):
            roles = [roles] if roles else []
        row["roles"] = list(roles)
    return rows


def create_user(
    conn: psycopg.Connection,
    *,
    username: str,
    password_hash: str,
    display_name: str,
    email: str | None = None,
    roles: list[str] | None = None,
    is_active: bool = True,
) -> str:
    row = client.fetchone(
        conn,
        """
        INSERT INTO auth.app_user (username, email, password_hash, display_name, is_active)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING user_id
        """,
        (username, email, password_hash, display_name, is_active),
    )
    assert row
    user_id = str(row["user_id"])
    if roles:
        set_user_roles(conn, user_id, roles)
    return user_id


def update_user(
    conn: psycopg.Connection,
    user_id: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    password_hash: str | None = None,
    is_active: bool | None = None,
    roles: list[str] | None = None,
) -> None:
    fields: list[str] = ["updated_at = now()"]
    params: list[Any] = []
    if email is not None:
        fields.append("email = %s")
        params.append(email)
    if display_name is not None:
        fields.append("display_name = %s")
        params.append(display_name)
    if password_hash is not None:
        fields.append("password_hash = %s")
        params.append(password_hash)
    if is_active is not None:
        fields.append("is_active = %s")
        params.append(is_active)
    params.append(user_id)
    client.execute(
        conn,
        f"UPDATE auth.app_user SET {', '.join(fields)} WHERE user_id = %s::uuid",
        params,
    )
    if roles is not None:
        set_user_roles(conn, user_id, roles)


def set_user_roles(conn: psycopg.Connection, user_id: str, roles: list[str]) -> None:
    client.execute(conn, "DELETE FROM auth.user_role WHERE user_id = %s::uuid", (user_id,))
    for key in roles:
        client.execute(
            conn,
            """
            INSERT INTO auth.user_role (user_id, role_id)
            SELECT %s::uuid, role_id FROM auth.role WHERE role_key = %s
            ON CONFLICT DO NOTHING
            """,
            (user_id, key),
        )


def touch_last_login(conn: psycopg.Connection, user_id: str) -> None:
    client.execute(
        conn,
        """
        UPDATE auth.app_user
        SET last_login_at = %s
        WHERE user_id = %s::uuid
        """,
        (datetime.now(timezone.utc), user_id),
    )


def ensure_user(
    conn: psycopg.Connection,
    *,
    username: str,
    password_hash: str,
    display_name: str,
    email: str | None = None,
    roles: list[str] | None = None,
) -> tuple[str, bool]:
    """Create user if missing (by username). Does not overwrite existing password/roles.

    Returns (user_id, created).
    """
    existing = get_user_by_username(conn, username.strip())
    if existing:
        return str(existing["user_id"]), False
    uid = create_user(
        conn,
        username=username.strip(),
        password_hash=password_hash,
        display_name=display_name.strip(),
        email=(email or username).strip(),
        roles=roles or [],
    )
    return uid, True


def ensure_bootstrap_admin(
    conn: psycopg.Connection,
    *,
    username: str,
    password_hash: str,
    display_name: str = "Bootstrap Admin",
) -> str | None:
    """Idempotent super_admin ensure (create-if-missing)."""
    uid, created = ensure_user(
        conn,
        username=username,
        password_hash=password_hash,
        display_name=display_name,
        email=username,
        roles=["super_admin"],
    )
    return uid if created else None
