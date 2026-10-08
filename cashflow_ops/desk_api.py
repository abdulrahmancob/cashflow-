"""Desk tracker extension: pairing from the portal, and device-token pings."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cashflow_ops.security import AuthUser, get_current_user

router = APIRouter(prefix="/desk", tags=["desk"])

DEVICE_SCHEME = "device"


def _ser(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return obj


class DeviceBody(BaseModel):
    label: str | None = Field(default=None, max_length=80)


class DevicePing(BaseModel):
    state: str = Field(min_length=1, max_length=16)
    version: str | None = Field(default=None, max_length=32)
    client_at: datetime | None = None


def device_token(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    scheme, _, token = raw.partition(" ")
    if scheme.strip().lower() != DEVICE_SCHEME or not token.strip():
        raise HTTPException(status_code=401, detail="device token required")
    return token.strip()


@router.post("/devices")
def pair_device(
    body: DeviceBody,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, desk_devices

    with connection() as conn:
        return desk_devices.create_device(
            conn,
            user.user_id,
            label=body.label,
            user_agent=request.headers.get("user-agent"),
        )


@router.get("/devices")
def my_devices(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    from cashflow_db.repository import connection, desk_devices

    with connection() as conn:
        return _ser({"devices": desk_devices.list_devices(conn, user.user_id)})


@router.delete("/devices/{device_id}")
def unpair_device(
    device_id: UUID,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, bool]:
    """The owner, or a lead or admin whose board includes the owner."""
    from cashflow_db.repository import connection, desk_devices, user_away

    with connection() as conn:
        owner = desk_devices.device_owner(conn, str(device_id))
        if owner is None:
            raise HTTPException(status_code=404, detail="device not found")
        if owner != user.user_id:
            try:
                allowed = user_away.person_in_board_scope(conn, user.roles, owner)
            except PermissionError:
                allowed = False
            if not allowed:
                raise HTTPException(status_code=404, detail="device not found")
        desk_devices.revoke_device(conn, str(device_id))
    return {"ok": True}


@router.post("/ping")
def device_ping(body: DevicePing, request: Request) -> dict[str, Any]:
    """Device-token only. This token opens nothing else in the portal."""
    from cashflow_db.repository import connection, desk_devices, presence

    token = device_token(request)
    now = datetime.now(timezone.utc)
    with connection() as conn:
        device = desk_devices.device_for_token(conn, token)
        if not device:
            raise HTTPException(status_code=401, detail="device not paired")
        if desk_devices.too_soon(device.get("last_seen_at"), now):
            raise HTTPException(status_code=429, detail="too many pings")
        desk_devices.touch_device(conn, str(device["device_id"]), now)
        result = presence.record_ping(
            conn,
            str(device["user_id"]),
            source=presence.SOURCE_EXTENSION,
            state=body.state,
            version=body.version,
            client_at=body.client_at,
            now=now,
        )
    return {
        "ok": True,
        "display_name": device.get("display_name") or "",
        "away": bool(result.get("away")),
        "idle_grace_seconds": result.get("idle_grace_seconds", presence.IDLE_GRACE_SECONDS),
    }
