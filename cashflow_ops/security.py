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

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.environ.get("CASHFLOW_JWT_SECRET", "dev-change-me-cashflow-jwt")
JWT_TTL_SECONDS = int(os.environ.get("CASHFLOW_JWT_TTL_SECONDS", "43200"))  # 12h
PBKDF2_ITERATIONS = 260_000

_bearer = HTTPBearer(auto_error=False)

ROLE_SUPER = "super_admin"
ROLE_FINANCE = "finance"
ROLE_POSTING = "posting_team"


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
    def is_finance(self) -> bool:
        return ROLE_FINANCE in self.roles or self.is_super_admin

    @property
    def is_posting(self) -> bool:
        return ROLE_POSTING in self.roles or self.is_super_admin


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_access_token(creds.credentials)
    return AuthUser(
        user_id=str(payload["sub"]),
        username=str(payload.get("username") or ""),
        display_name=str(payload.get("display_name") or ""),
        roles=list(payload.get("roles") or []),
    )


def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser | None:
    if creds is None or not creds.credentials:
        return None
    try:
        payload = decode_access_token(creds.credentials)
    except HTTPException:
        return None
    return AuthUser(
        user_id=str(payload["sub"]),
        username=str(payload.get("username") or ""),
        display_name=str(payload.get("display_name") or ""),
        roles=list(payload.get("roles") or []),
    )


def require_roles(*role_keys: str) -> Callable[..., AuthUser]:
    def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.is_super_admin:
            return user
        if not user.has_role(*role_keys):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _dep


TRACKER_RESOURCE = "transaction_tracker"


def get_tracker_perms(user: AuthUser) -> dict[str, bool]:
    """Return can_view/edit/upload/admin for transaction_tracker."""
    if user.is_super_admin:
        return {
            "can_view": True,
            "can_edit": True,
            "can_upload": True,
            "can_admin": True,
        }
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        grant = tracker.get_grant(conn, user.user_id, resource_key=TRACKER_RESOURCE)
    return {
        "can_view": bool(grant.get("can_view")),
        "can_edit": bool(grant.get("can_edit")),
        "can_upload": bool(grant.get("can_upload")),
        "can_admin": bool(grant.get("can_admin")),
    }


def require_tracker_perm(perm: str) -> Callable[..., AuthUser]:
    """perm: view | edit | upload | admin"""
    key = {
        "view": "can_view",
        "edit": "can_edit",
        "upload": "can_upload",
        "admin": "can_admin",
    }.get(perm)
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
