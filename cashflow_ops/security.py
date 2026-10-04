"""JWT auth helpers and FastAPI dependencies for the RCM portal."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.environ.get("CASHFLOW_JWT_SECRET", "dev-change-me-cashflow-jwt")
JWT_TTL_SECONDS = int(os.environ.get("CASHFLOW_JWT_TTL_SECONDS", "43200"))  # 12h
PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE_NAME = "rcm_session"

_bearer = HTTPBearer(auto_error=False)

ROLE_SUPER = "super_admin"
ROLE_FINANCE = "finance"
ROLE_POSTING = "posting_team"
ROLE_SUBMISSION = "submission"
ROLE_COLLECTOR = "collector"
ROLE_OPS_ADMIN = "ops_admin"
ROLE_SUB_ADMIN = "sub_admin"
ROLE_SECOND_SUBMISSION = "second_submission"
ROLE_SECOND_SUBMISSION_LEAD = "second_submission_lead"
ROLE_ANALYTICS_VIEWER = "analytics_viewer"
ROLE_MEDICAL_AUDIT = "medical_audit"


from cashflow_db.services.bootstrap_admin import (  # noqa: E402
    hash_password,
    seed_portal_users,
    verify_password,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_access_token(
    *,
    user_id: str,
    username: str,
    roles: list[str],
    display_name: str,
    ttl_seconds: int | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "display_name": display_name,
        "roles": roles,
        "iat": now,
        "exp": now + (ttl_seconds or JWT_TTL_SECONDS),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        h, p, s = token.split(".")
        expected = hmac.new(
            JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(s)):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(p))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        return payload
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


def cors_allow_origins(raw: str | None = None) -> list[str]:
    """Comma-separated browser origins. Empty means same-origin only."""
    value = os.environ.get("CASHFLOW_CORS_ORIGINS", "") if raw is None else raw
    return [part.strip() for part in value.split(",") if part.strip()]


def cookie_should_be_secure(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded:
        return forwarded == "https"
    return request.url.scheme == "https"


def extract_access_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = None,
) -> str | None:
    if creds is not None and creds.credentials:
        return creds.credentials
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token:
            return token
    cookie = (request.cookies.get(SESSION_COOKIE_NAME) or "").strip()
    return cookie or None


def set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    *,
    remember: bool = False,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=cookie_should_be_secure(request),
        samesite="lax",
        path="/",
        max_age=JWT_TTL_SECONDS if remember else None,
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=cookie_should_be_secure(request),
        httponly=True,
        samesite="lax",
    )


def load_live_auth_user(user_id: str) -> AuthUser:
    """Resolve an active user and current DB roles. Fails closed."""
    from cashflow_db.repository import auth_users, connection

    try:
        with connection() as conn:
            row = auth_users.get_user_by_id(conn, user_id)
            if not row or not row.get("is_active"):
                raise HTTPException(status_code=401, detail="User inactive")
            roles = auth_users.get_user_roles(conn, user_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="Authentication required") from exc
    return AuthUser(
        user_id=str(row["user_id"]),
        username=str(row.get("username") or ""),
        display_name=str(row.get("display_name") or ""),
        roles=list(roles),
    )


def auth_user_from_token(token: str) -> AuthUser:
    payload = decode_access_token(token)
    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return load_live_auth_user(user_id)


@dataclass
class AuthUser:
    user_id: str
    username: str
    display_name: str
    roles: list[str]

    def has_role(self, *keys: str) -> bool:
        return any(r in self.roles for r in keys)

    @property
    def is_super_admin(self) -> bool:
        return ROLE_SUPER in self.roles

    @property
    def is_sub_admin(self) -> bool:
        return ROLE_SUB_ADMIN in self.roles or self.is_super_admin

    @property
    def is_elevated_admin(self) -> bool:
        """super_admin or sub_admin. Not a global bypass for Platform/Database."""
        return self.is_super_admin or ROLE_SUB_ADMIN in self.roles

    @property
    def is_finance(self) -> bool:
        return ROLE_FINANCE in self.roles or self.is_elevated_admin

    @property
    def is_posting(self) -> bool:
        return ROLE_POSTING in self.roles or self.is_super_admin

    @property
    def is_ops_admin(self) -> bool:
        return ROLE_OPS_ADMIN in self.roles or self.is_super_admin


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    token = extract_access_token(request, creds)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth_user_from_token(token)


def get_optional_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser | None:
    token = extract_access_token(request, creds)
    if not token:
        return None
    try:
        return auth_user_from_token(token)
    except HTTPException:
        return None


def require_roles(*role_keys: str) -> Callable[..., AuthUser]:
    def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.is_super_admin:
            return user
        if not user.has_role(*role_keys):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _dep


TRACKER_RESOURCE = "transaction_tracker"
CHECKS_DEPOSITS_RESOURCE = "checks_deposits"

_PERM_KEYS = {
    "view": "can_view",
    "edit": "can_edit",
    "upload": "can_upload",
    "admin": "can_admin",
}


def get_resource_perms(user: AuthUser, resource_key: str) -> dict[str, bool]:
    """Return can_view/edit/upload/admin for a resource_grant key."""
    if user.is_elevated_admin:
        return {
            "can_view": True,
            "can_edit": True,
            "can_upload": True,
            "can_admin": True,
        }
    if user.has_role(ROLE_OPS_ADMIN):
        return {
            "can_view": True,
            "can_edit": True,
            "can_upload": True,
            "can_admin": False,
        }
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        grant = tracker.get_grant(conn, user.user_id, resource_key=resource_key)
    return {
        "can_view": bool(grant.get("can_view")),
        "can_edit": bool(grant.get("can_edit")),
        "can_upload": bool(grant.get("can_upload")),
        "can_admin": bool(grant.get("can_admin")),
    }


def require_resource_perm(resource_key: str, perm: str) -> Callable[..., AuthUser]:
    """perm: view | edit | upload | admin"""
    key = _PERM_KEYS.get(perm)
    if not key:
        raise ValueError(f"Unknown resource perm: {perm}")

    def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        perms = get_resource_perms(user, resource_key)
        if not perms.get(key):
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions for {resource_key}",
            )
        return user

    return _dep


def get_tracker_perms(user: AuthUser) -> dict[str, bool]:
    """Return can_view/edit/upload/admin for transaction_tracker."""
    return get_resource_perms(user, TRACKER_RESOURCE)


def require_tracker_perm(perm: str) -> Callable[..., AuthUser]:
    """perm: view | edit | upload | admin"""
    key = _PERM_KEYS.get(perm)
    if not key:
        raise ValueError(f"Unknown tracker perm: {perm}")

    def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        perms = get_tracker_perms(user)
        if not perms.get(key):
            raise HTTPException(status_code=403, detail="Insufficient tracker permissions")
        return user

    return _dep


def bootstrap_admin_if_needed() -> dict[str, Any] | None:
    """Idempotent seed of Docker portal users (admin / finance / posting)."""
    try:
        return seed_portal_users()
    except Exception:  # noqa: BLE001
        return None


def parse_uuid_list(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    out: list[str] = []
    for v in raw:
        try:
            out.append(str(UUID(v)))
        except Exception:  # noqa: BLE001
            continue
    return out or None
