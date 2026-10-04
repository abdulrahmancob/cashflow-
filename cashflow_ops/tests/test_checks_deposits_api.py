"""Checks & Deposits API permissions and version conflicts (no live DB)."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_ops.checks_deposits_api import router
from cashflow_ops.security import (
    AuthUser,
    get_current_user,
    get_resource_perms,
    get_tracker_perms,
)

USER_ID = "11111111-1111-1111-1111-111111111111"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        user_id=USER_ID,
        username="jane@example.com",
        display_name="Jane",
        roles=list(roles),
    )


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _connection():
    yield _Conn()


def _app(user: AuthUser) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_elevated_and_ops_admin_perms_do_not_hit_the_database():
    admin = get_resource_perms(_user("sub_admin"), "checks_deposits")
    assert admin == {
        "can_view": True,
        "can_edit": True,
        "can_upload": True,
        "can_admin": True,
    }
    ops = get_resource_perms(_user("ops_admin"), "checks_deposits")
    assert ops["can_view"] and ops["can_edit"] and ops["can_upload"]
    assert ops["can_admin"] is False
    assert get_tracker_perms(_user("super_admin"))["can_admin"] is True


def test_rows_forbidden_without_grant(monkeypatch):
    monkeypatch.setattr(
        "cashflow_ops.security.get_resource_perms",
        lambda user, key: {
            "can_view": False,
            "can_edit": False,
            "can_upload": False,
            "can_admin": False,
        },
    )
    res = _app(_user("posting_team")).get("/api/checks-deposits/rows")
    assert res.status_code == 403


def test_patch_version_conflict(monkeypatch):
    monkeypatch.setattr("cashflow_db.repository.connection", _connection)
    monkeypatch.setattr(
        "cashflow_db.repository.checks_deposits.get_row",
        lambda conn, row_id, **kwargs: {"row_id": row_id, "version": 2, "check_number": "1"},
    )
    monkeypatch.setattr(
        "cashflow_db.repository.checks_deposits.update_row",
        lambda *args, **kwargs: {"__conflict__": True, "current": {"row_id": "r", "version": 2}},
    )
    res = _app(_user("ops_admin")).patch(
        "/api/checks-deposits/rows/22222222-2222-2222-2222-222222222222",
        json={"version": 1, "payer": "New"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["message"] == "Version conflict"


def test_create_rejects_deposit_before_2026():
    res = _app(_user("ops_admin")).post(
        "/api/checks-deposits/rows",
        json={"amount": "10.00", "deposit_date": "2025-06-01", "payer": "Old"},
    )
    assert res.status_code == 422
