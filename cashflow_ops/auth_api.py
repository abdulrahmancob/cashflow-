"""Auth + user management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_ANALYTICS_VIEWER,
    ROLE_COLLECTOR,
    ROLE_DESK,
    ROLE_MEDICAL_AUDIT,
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_SUBMISSION,
    ROLE_SUPER,
    AuthUser,
    clear_session_cookie,
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    set_session_cookie,
    verify_password,
)

router = APIRouter(tags=["auth"])

ALLOWED_ROLES = {
    ROLE_SUPER,
    "finance",
    "posting_team",
    ROLE_SUBMISSION,
    ROLE_COLLECTOR,
    ROLE_OPS_ADMIN,
    ROLE_SUB_ADMIN,
    ROLE_SECOND_SUBMISSION,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_ANALYTICS_VIEWER,
    ROLE_MEDICAL_AUDIT,
    ROLE_DESK,
}

USER_MANAGE_ROLES = (ROLE_SUPER, ROLE_SUB_ADMIN)
SUB_ADMIN_SUPER_BLOCK = "Sub admins cannot grant or change super admin accounts"


def filter_roles(roles: list[str] | None) -> list[str]:
    return [r for r in (roles or []) if r in ALLOWED_ROLES]


def would_deactivate_self(actor_id: str, target_id: str, is_active: bool | None) -> bool:
    return is_active is False and actor_id == target_id


def would_remove_last_super_admin(
    *,
    target_currently_active: bool,
    target_current_roles: list[str],
    new_is_active: bool | None,
    new_roles: list[str] | None,
    active_super_admin_count: int,
) -> bool:
    """True if this update would leave zero active super_admins."""
    if ROLE_SUPER not in target_current_roles or not target_currently_active:
        return False
    will_be_active = target_currently_active if new_is_active is None else new_is_active
    will_have_super = (
        ROLE_SUPER in target_current_roles if new_roles is None else ROLE_SUPER in new_roles
    )
    if will_be_active and will_have_super:
        return False
    return active_super_admin_count <= 1


def sub_admin_blocked_from_super(
    *,
    actor_is_super: bool,
    target_roles: list[str] | None = None,
    new_roles: list[str] | None = None,
) -> bool:
    """Sub-admin cannot grant super_admin or mutate super_admin users."""
    if actor_is_super:
        return False
    if target_roles is not None and ROLE_SUPER in target_roles:
        return True
    if new_roles is not None and ROLE_SUPER in new_roles:
        return True
    return False


def blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def user_write_error(exc: BaseException) -> HTTPException | None:
    """Map integrity errors to client statuses. Other exceptions stay unmapped."""
    from psycopg import errors as pg_errors

    if isinstance(exc, pg_errors.UniqueViolation):
        return HTTPException(status_code=409, detail="Username already exists")
    if isinstance(exc, pg_errors.IntegrityError):
        return HTTPException(status_code=400, detail="Could not save user")
    return None


def log_user_change(conn: Any, actor: AuthUser, **kwargs: Any) -> None:
    """Record the activity row in a savepoint so a log failure cannot abort the save."""
    from cashflow_ops.activity_api import log_write

    try:
        with conn.transaction():
            log_write(conn, actor, **kwargs)
    except Exception:  # noqa: BLE001 — activity log must not block the user save
        return


class LoginBody(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class UserCreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = None
    roles: list[str] = Field(default_factory=list)
    is_active: bool = True


class UserUpdateBody(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=200)
    display_name: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, max_length=200)
    roles: list[str] | None = None
    is_active: bool | None = None


def _update_user_row(
    user_id: str,
    body: UserUpdateBody,
    actor: AuthUser,
    roles: list[str] | None,
    new_password: str | None,
    new_username: str | None,
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        existing = auth_users.get_user_by_id(conn, user_id)
        if not existing:
            raise HTTPException(status_code=404, detail="User not found")
        current_roles = auth_users.get_user_roles(conn, user_id)
        if sub_admin_blocked_from_super(
            actor_is_super=actor.is_super_admin,
            target_roles=current_roles,
            new_roles=roles,
        ):
            raise HTTPException(status_code=403, detail=SUB_ADMIN_SUPER_BLOCK)
        if would_remove_last_super_admin(
            target_currently_active=bool(existing.get("is_active")),
            target_current_roles=current_roles,
            new_is_active=body.is_active,
            new_roles=roles,
            active_super_admin_count=auth_users.count_active_super_admins(conn),
        ):
            raise HTTPException(
                status_code=400,
                detail="Cannot remove or deactivate the last super admin",
            )
        if new_username is not None:
            taken = auth_users.get_user_by_username(conn, new_username)
            if taken and str(taken["user_id"]) != user_id:
                raise HTTPException(status_code=409, detail="Username already exists")
        display_name = None
        if body.display_name is not None:
            display_name = body.display_name.strip()
            if not display_name:
                raise HTTPException(status_code=400, detail="Display name is required")
        auth_users.update_user(
            conn,
            user_id,
            username=new_username,
            email=body.email,
            display_name=display_name,
            password_hash=hash_password(new_password) if new_password else None,
            is_active=body.is_active,
            roles=roles,
        )
        row = auth_users.get_user_by_id(conn, user_id)
        assert row
        rlist = auth_users.get_user_roles(conn, user_id)
        from cashflow_db.repository import portal_activity

        before = {
            "username": existing.get("username"),
            "email": existing.get("email"),
            "display_name": existing.get("display_name"),
            "is_active": existing.get("is_active"),
            "roles": current_roles,
        }
        after = {
            "username": row.get("username"),
            "email": row.get("email"),
            "display_name": row.get("display_name"),
            "is_active": row.get("is_active"),
            "roles": rlist,
        }
        extra = {"password_reset": True} if new_password else None
        fallback = "Reset password" if new_password else "Updated user"
        log_user_change(
            conn,
            actor,
            action="updated",
            area="users",
            entity_type="user",
            entity_id=user_id,
            entity_label=portal_activity.user_label(after),
            before=before,
            after=after,
            keys=["username", "email", "display_name", "is_active", "roles"],
            extra=extra,
            fallback=fallback,
            force=bool(new_password),
        )
        return _public_user(row, rlist)


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
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        user = auth_users.get_user_by_username(conn, body.username.strip())
        if not user or not user.get("is_active"):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        from cashflow_db.services.bootstrap_admin import is_system_login

        if is_system_login(user.get("username"), user.get("display_name")):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        roles = auth_users.get_user_roles(conn, str(user["user_id"]))
        auth_users.touch_last_login(conn, str(user["user_id"]))
        auth_users.record_login(
            conn,
            str(user["user_id"]),
            user_agent=request.headers.get("user-agent"),
        )
    token = create_access_token(
        user_id=str(user["user_id"]),
        username=user["username"],
        roles=roles,
        display_name=user["display_name"],
    )
    set_session_cookie(response, request, token, remember=body.remember_me)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _public_user(user, roles),
    }


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    clear_session_cookie(response, request)
    return {"ok": True}


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
def roles(_: AuthUser = Depends(require_roles(*USER_MANAGE_ROLES))) -> list[dict[str, Any]]:
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
def list_users(_: AuthUser = Depends(require_roles(*USER_MANAGE_ROLES))) -> list[dict[str, Any]]:
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        rows = auth_users.list_users(conn)
    return [_public_user(r) for r in rows]


@router.post("/auth/users")
def create_user(
    body: UserCreateBody,
    actor: AuthUser = Depends(require_roles(*USER_MANAGE_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection

    roles = filter_roles(body.roles)
    if not roles:
        raise HTTPException(status_code=400, detail="At least one valid role required")
    if sub_admin_blocked_from_super(actor_is_super=actor.is_super_admin, new_roles=roles):
        raise HTTPException(status_code=403, detail=SUB_ADMIN_SUPER_BLOCK)
    username = body.username.strip()
    email = blank_to_none(body.email) or username
    try:
        with connection() as conn:
            if auth_users.get_user_by_username(conn, username):
                raise HTTPException(status_code=409, detail="Username already exists")
            uid = auth_users.create_user(
                conn,
                username=username,
                password_hash=hash_password(body.password),
                display_name=body.display_name.strip(),
                email=email,
                roles=roles,
                is_active=body.is_active,
            )
            row = auth_users.get_user_by_id(conn, uid)
            assert row
            from cashflow_db.repository import portal_activity
            from cashflow_ops.activity_api import log_write

            public = _public_user(row, roles)
            log_write(
                conn,
                actor,
                action="created",
                area="users",
                entity_type="user",
                entity_id=str(uid),
                entity_label=portal_activity.user_label(public),
                after=public,
                fallback=f"Created user {public.get('display_name') or username}",
            )
            return public
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/auth/users/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdateBody,
    actor: AuthUser = Depends(require_roles(*USER_MANAGE_ROLES)),
) -> dict[str, Any]:
    roles = None
    if body.roles is not None:
        roles = filter_roles(body.roles)
        if not roles:
            raise HTTPException(status_code=400, detail="At least one valid role required")
    new_password = blank_to_none(body.password)
    if new_password is not None and len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    new_username = blank_to_none(body.username)
    if would_deactivate_self(actor.user_id, user_id, body.is_active):
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    try:
        return _update_user_row(user_id, body, actor, roles, new_password, new_username)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — map integrity errors; re-raise the rest
        mapped = user_write_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise
