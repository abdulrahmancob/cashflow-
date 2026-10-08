"""Desk tracker: pair from the portal, ping with a device token, revoke."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_db.config import SQL_DIR
from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import desk_devices
from cashflow_ops.desk_api import router
from cashflow_ops.security import AuthUser, get_current_user

OWNER = "11111111-1111-1111-1111-111111111111"
DEVICE = "44444444-4444-4444-4444-444444444444"


def _user(*roles: str, user_id: str = OWNER) -> AuthUser:
    return AuthUser(user_id=user_id, username="agent@example.com", display_name="Agent", roles=list(roles))


def _app(monkeypatch, user: AuthUser | None = None) -> TestClient:
    @contextmanager
    def _connection():
        yield object()

    monkeypatch.setattr("cashflow_db.repository.connection", _connection)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_migration_is_registered_and_keeps_only_hashes():
    assert "079_desk_devices.sql" in MIGRATIONS
    body = (SQL_DIR / "079_desk_devices.sql").read_text(encoding="utf-8")
    assert "token_hash text NOT NULL UNIQUE" in body
    assert " token text" not in body
    assert desk_devices.hash_token("abc") != "abc"
    assert len(desk_devices.hash_token("abc")) == 64


def test_pairing_returns_the_token_once(monkeypatch):
    seen: dict = {}

    def create(conn, user_id, **kwargs):
        seen.update(user_id=user_id, **kwargs)
        return {"device_id": DEVICE, "token": "secret-token"}

    client = _app(monkeypatch, _user("red_agent"))
    monkeypatch.setattr("cashflow_db.repository.desk_devices.create_device", create)
    res = client.post("/api/desk/devices", json={"label": "Home laptop"}, headers={"User-Agent": "Chrome"})
    assert res.status_code == 200
    assert res.json() == {"device_id": DEVICE, "token": "secret-token"}
    assert seen["user_id"] == OWNER and seen["label"] == "Home laptop"


def test_ping_needs_a_live_device_token(monkeypatch):
    client = _app(monkeypatch)
    assert client.post("/api/desk/ping", json={"state": "active"}).status_code == 401
    bearer = client.post(
        "/api/desk/ping", json={"state": "active"}, headers={"Authorization": "Bearer abc"}
    )
    assert bearer.status_code == 401
    monkeypatch.setattr("cashflow_db.repository.desk_devices.device_for_token", lambda conn, t: None)
    revoked = client.post(
        "/api/desk/ping", json={"state": "active"}, headers={"Authorization": "Device gone"}
    )
    assert revoked.status_code == 401


def test_ping_records_an_extension_stream_and_limits_rate(monkeypatch):
    recorded: dict = {}
    touched: list = []
    device = {"device_id": DEVICE, "user_id": OWNER, "last_seen_at": None, "display_name": "Agent"}
    client = _app(monkeypatch)
    monkeypatch.setattr("cashflow_db.repository.desk_devices.device_for_token", lambda conn, t: device)
    monkeypatch.setattr(
        "cashflow_db.repository.desk_devices.touch_device", lambda conn, did, now: touched.append(did)
    )

    def record(conn, user_id, **kwargs):
        recorded.update(user_id=user_id, **kwargs)
        return {"ok": True, "away": False, "idle_grace_seconds": 300}

    monkeypatch.setattr("cashflow_db.repository.presence.record_ping", record)
    res = client.post(
        "/api/desk/ping",
        json={"state": "locked", "version": "1.0.0"},
        headers={"Authorization": "Device good"},
    )
    assert res.status_code == 200
    assert res.json()["display_name"] == "Agent" and res.json()["idle_grace_seconds"] == 300
    assert recorded["user_id"] == OWNER and recorded["source"] == "extension"
    assert recorded["state"] == "locked" and recorded["version"] == "1.0.0"
    assert touched == [DEVICE]

    device["last_seen_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    fast = client.post("/api/desk/ping", json={"state": "active"}, headers={"Authorization": "Device good"})
    assert fast.status_code == 429


def test_revoke_by_owner_or_lead_in_scope_only(monkeypatch):
    revoked: list = []
    monkeypatch.setattr("cashflow_db.repository.desk_devices.device_owner", lambda conn, did: OWNER)
    monkeypatch.setattr(
        "cashflow_db.repository.desk_devices.revoke_device", lambda conn, did, **k: revoked.append(did)
    )
    owner = _app(monkeypatch, _user("red_agent"))
    assert owner.delete(f"/api/desk/devices/{DEVICE}").status_code == 200

    stranger = _app(monkeypatch, _user("red_agent", user_id="55555555-5555-5555-5555-555555555555"))
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.person_in_board_scope",
        lambda conn, roles, uid: (_ for _ in ()).throw(PermissionError("no board")),
    )
    assert stranger.delete(f"/api/desk/devices/{DEVICE}").status_code == 404

    lead = _app(monkeypatch, _user("redteam_leader", user_id="66666666-6666-6666-6666-666666666666"))
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.person_in_board_scope", lambda conn, roles, uid: uid == OWNER
    )
    assert lead.delete(f"/api/desk/devices/{DEVICE}").status_code == 200
    assert revoked == [DEVICE, DEVICE]


def test_device_token_does_not_open_portal_routes():
    from cashflow_ops import security

    source = (security.__file__ and open(security.__file__, encoding="utf-8").read()) or ""
    assert "Device" not in source.split("def extract_access_token", 1)[1].split("def ", 1)[0]
