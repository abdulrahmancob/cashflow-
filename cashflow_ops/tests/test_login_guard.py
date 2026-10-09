"""Login lockout: five failures lock the account, success resets, and failures survive the error."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_db.config import SQL_DIR
from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import login_guard as lg
from cashflow_ops import auth_api
from cashflow_ops.auth_api import router

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


def _rows(*flags: bool, step_minutes: int = 1):
    return [{"at": NOW - timedelta(minutes=i * step_minutes), "ok": ok} for i, ok in enumerate(flags)]


def test_migration_registered():
    assert "080_login_attempts.sql" in MIGRATIONS
    assert "auth.login_attempt" in (SQL_DIR / "080_login_attempts.sql").read_text(encoding="utf-8")


def test_five_failures_lock_and_success_resets():
    assert lg.lock_seconds(_rows(False, False, False, False), 0, NOW) == 0
    locked = lg.lock_seconds(_rows(False, False, False, False, False), 0, NOW)
    assert 0 < locked <= 15 * 60
    assert lg.lock_seconds(_rows(False, False, True, False, False, False), 0, NOW) == 0
    old = _rows(False, False, False, False, False, step_minutes=4)
    assert lg.lock_seconds(old, 0, NOW) == 0


def test_address_with_many_failures_is_locked():
    assert lg.lock_seconds([], lg.IP_FAILURES - 1, NOW) == 0
    assert lg.lock_seconds([], lg.IP_FAILURES, NOW) == 15 * 60


class FakeConn:
    pass


def _client(monkeypatch, *, lock=0, user=None, password_ok=False):
    events: list = []

    @contextmanager
    def _connection():
        conn = FakeConn()
        try:
            yield conn
            events.append("commit")
        except Exception:
            events.append("rollback")
            raise

    monkeypatch.setattr("cashflow_db.repository.connection", _connection)
    monkeypatch.setattr("cashflow_db.repository.login_guard.check", lambda *a, **k: lock)
    monkeypatch.setattr(
        "cashflow_db.repository.login_guard.record",
        lambda conn, username, ip, ok, now: events.append(("record", username, ip, ok)),
    )
    monkeypatch.setattr("cashflow_db.repository.auth_users.get_user_by_username", lambda conn, u: user)
    monkeypatch.setattr("cashflow_db.repository.auth_users.get_user_roles", lambda conn, uid: ["red_agent"])
    monkeypatch.setattr("cashflow_db.repository.auth_users.touch_last_login", lambda *a, **k: None)
    monkeypatch.setattr("cashflow_db.repository.auth_users.record_login", lambda *a, **k: None)
    monkeypatch.setattr(auth_api, "verify_password", lambda pw, h: password_ok)
    burned: list = []
    monkeypatch.setattr(auth_api, "_burn_password_check", lambda pw: burned.append(pw))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app), events, burned


def test_failed_login_is_recorded_and_committed(monkeypatch):
    user = {"user_id": "u1", "username": "a@x.com", "display_name": "A", "is_active": True,
            "password_hash": "h"}
    client, events, burned = _client(monkeypatch, user=user, password_ok=False)
    res = client.post("/api/auth/login", json={"username": "A@x.com", "password": "bad"},
                      headers={"X-Real-IP": "102.1.2.3"})
    assert res.status_code == 401
    assert ("record", "A@x.com", "102.1.2.3", False) in events
    assert events[-1] == "commit"
    assert burned == []


def test_unknown_user_costs_the_same_and_is_recorded(monkeypatch):
    client, events, burned = _client(monkeypatch, user=None)
    res = client.post("/api/auth/login", json={"username": "ghost@x.com", "password": "pw"})
    assert res.status_code == 401
    assert burned == ["pw"]
    assert any(e[0] == "record" and e[3] is False for e in events if isinstance(e, tuple))


def test_locked_account_gets_429_without_checking_the_password(monkeypatch):
    client, events, burned = _client(monkeypatch, lock=600)
    res = client.post("/api/auth/login", json={"username": "a@x.com", "password": "pw"})
    assert res.status_code == 429
    assert "10 minutes" in res.json()["detail"]
    assert not any(isinstance(e, tuple) for e in events)


def test_success_is_recorded_and_signs_in(monkeypatch):
    user = {"user_id": "u1", "username": "a@x.com", "display_name": "A", "is_active": True,
            "password_hash": "h"}
    client, events, _burned = _client(monkeypatch, user=user, password_ok=True)
    res = client.post("/api/auth/login", json={"username": "a@x.com", "password": "good"})
    assert res.status_code == 200
    assert ("record", "a@x.com", "testclient", True) in events


def test_oversized_fields_are_rejected(monkeypatch):
    client, _events, _burned = _client(monkeypatch)
    res = client.post("/api/auth/login", json={"username": "a" * 300, "password": "pw"})
    assert res.status_code == 422
