"""Auth + user management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_SUPER,
    AuthUser,
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    seed_portal_users,
    verify_password,
)

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class UserCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = None
    roles: list[str] = Field(default_factory=list)
    is_active: bool = True


class UserUpdateBody(BaseModel):
    display_name: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=200)
    roles: list[str] | None = None
    is_active: bool | None = None


def _public_user(row: dict[str, Any], roles: list[str] | None = None) -> dict[str, Any]:
    return {
        "user_id": str(row["user_id"]),
        "username": row["username"],
        "email": row.get("email"),
        "display_name": row["display_name"],
        "is_active": row["is_active"],
        "roles": roles if roles is not None else row.get("roles") or [],
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
    }


@router.post("/auth/login")
def login(body: LoginBody) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    try:
        seed_portal_users()
    except Exception:  # noqa: BLE001
        pass
    with connection() as conn:
        user = auth_users.get_user_by_username(conn, body.username.strip())
        if not user or not user.get("is_active"):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        roles = auth_users.get_user_roles(conn, str(user["user_id"]))
        auth_users.touch_last_login(conn, str(user["user_id"]))
    token = create_access_token(
        user_id=str(user["user_id"]),
        username=user["username"],
        roles=roles,
        display_name=user["display_name"],
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _public_user(user, roles),
    }


@router.get("/auth/me")
def me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        row = auth_users.get_user_by_id(conn, user.user_id)
        if not row or not row.get("is_active"):
            raise HTTPException(status_code=401, detail="User inactive")
        roles = auth_users.get_user_roles(conn, user.user_id)
    return _public_user(row, roles)


@router.get("/auth/roles")
def roles(_: AuthUser = Depends(require_roles(ROLE_SUPER))) -> list[dict[str, Any]]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        rows = auth_users.list_roles(conn)
    return [
        {
            "role_id": str(r["role_id"]),
            "role_key": r["role_key"],
            "display_name": r["display_name"],
        }
        for r in rows
    ]


@router.get("/auth/users")
def list_users(_: AuthUser = Depends(require_roles(ROLE_SUPER))) -> list[dict[str, Any]]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        rows = auth_users.list_users(conn)
    return [_public_user(r) for r in rows]


@router.post("/auth/users")
def create_user(
    body: UserCreateBody,
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    allowed = {ROLE_SUPER, "finance", "posting_team"}
    roles = [r for r in body.roles if r in allowed]
    if not roles:
        raise HTTPException(status_code=400, detail="At least one valid role required")
    try:
        with connection() as conn:
            if auth_users.get_user_by_username(conn, body.username.strip()):
                raise HTTPException(status_code=409, detail="Username already exists")
            uid = auth_users.create_user(
                conn,
                username=body.username.strip(),
                password_hash=hash_password(body.password),
                display_name=body.display_name.strip(),
                email=body.email,
                roles=roles,
                is_active=body.is_active,
            )
            row = auth_users.get_user_by_id(conn, uid)
            assert row
            return _public_user(row, roles)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/auth/users/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdateBody,
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    allowed = {ROLE_SUPER, "finance", "posting_team"}
    roles = None
    if body.roles is not None:
        roles = [r for r in body.roles if r in allowed]
        if not roles:
            raise HTTPException(status_code=400, detail="At least one valid role required")
    with connection() as conn:
        if not auth_users.get_user_by_id(conn, user_id):
            raise HTTPException(status_code=404, detail="User not found")
        auth_users.update_user(
            conn,
            user_id,
            email=body.email,
            display_name=body.display_name,
            password_hash=hash_password(body.password) if body.password else None,
            is_active=body.is_active,
            roles=roles,
        )
        row = auth_users.get_user_by_id(conn, user_id)
        assert row
        rlist = auth_users.get_user_roles(conn, user_id)
        return _public_user(row, rlist)
