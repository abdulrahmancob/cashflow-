"""Guards for super-admin user edits (no live DB required)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from psycopg.errors import CheckViolation, UniqueViolation

from cashflow_ops.auth_api import (
    UserUpdateBody,
    blank_to_none,
    filter_roles,
    sub_admin_blocked_from_super,
    update_user,
    user_write_error,
    would_deactivate_self,
    would_remove_last_super_admin,
)
from cashflow_ops.security import AuthUser


def test_filter_roles_drops_unknown():
    assert filter_roles(["super_admin", "nope", "finance"]) == ["super_admin", "finance"]
    assert filter_roles(["collector"]) == ["collector"]
    assert filter_roles(["ops_admin"]) == ["ops_admin"]
    assert filter_roles(["sub_admin"]) == ["sub_admin"]
    assert filter_roles(["analytics_viewer"]) == ["analytics_viewer"]
    assert filter_roles(["desk"]) == ["desk"]
    assert filter_roles([]) == []
    assert filter_roles(None) == []


def test_blank_to_none():
    assert blank_to_none(None) is None
    assert blank_to_none("") is None
    assert blank_to_none("  ") is None
    assert blank_to_none(" secret ") == "secret"


def test_update_body_empty_password_keeps_current():
    body = UserUpdateBody(password="")
    assert blank_to_none(body.password) is None
    body = UserUpdateBody(password="abcdef")
    assert blank_to_none(body.password) == "abcdef"


def test_would_deactivate_self():
    assert would_deactivate_self("a", "a", False) is True
    assert would_deactivate_self("a", "b", False) is False
    assert would_deactivate_self("a", "a", True) is False
    assert would_deactivate_self("a", "a", None) is False


def test_last_super_admin_blocked_on_deactivate_or_role_strip():
    base = dict(
        target_currently_active=True,
        target_current_roles=["super_admin", "finance"],
        active_super_admin_count=1,
    )
    assert (
        would_remove_last_super_admin(
            **base, new_is_active=False, new_roles=None
        )
        is True
    )
    assert (
        would_remove_last_super_admin(
            **base, new_is_active=None, new_roles=["finance"]
        )
        is True
    )
    assert (
        would_remove_last_super_admin(
            **base, new_is_active=None, new_roles=None
        )
        is False
    )


def test_last_super_admin_allows_when_another_remains():
    assert (
        would_remove_last_super_admin(
            target_currently_active=True,
            target_current_roles=["super_admin"],
            new_is_active=False,
            new_roles=None,
            active_super_admin_count=2,
        )
        is False
    )


def test_non_super_or_inactive_not_guarded():
    assert (
        would_remove_last_super_admin(
            target_currently_active=True,
            target_current_roles=["finance"],
            new_is_active=False,
            new_roles=["finance"],
            active_super_admin_count=1,
        )
        is False
    )
    assert (
        would_remove_last_super_admin(
            target_currently_active=False,
            target_current_roles=["super_admin"],
            new_is_active=False,
            new_roles=["finance"],
            active_super_admin_count=1,
        )
        is False
    )


def test_sub_admin_blocked_from_granting_or_touching_super():
    assert (
        sub_admin_blocked_from_super(
            actor_is_super=False,
            new_roles=["sub_admin", "super_admin"],
        )
        is True
    )
    assert (
        sub_admin_blocked_from_super(
            actor_is_super=False,
            target_roles=["super_admin"],
            new_roles=["finance"],
        )
        is True
    )
    assert (
        sub_admin_blocked_from_super(
            actor_is_super=False,
            target_roles=["finance"],
            new_roles=["finance", "ops_admin"],
        )
        is False
    )
    assert (
        sub_admin_blocked_from_super(
            actor_is_super=True,
            target_roles=["super_admin"],
            new_roles=["super_admin", "finance"],
        )
        is False
    )


def test_user_write_error_maps_unique_and_other_integrity():
    unique = user_write_error(UniqueViolation("dup"))
    assert unique is not None
    assert unique.status_code == 409
    assert unique.detail == "Username already exists"
    other = user_write_error(CheckViolation("bad"))
    assert other is not None
    assert other.status_code == 400
    assert user_write_error(RuntimeError("x")) is None


def _actor() -> AuthUser:
    return AuthUser(
        user_id="actor-1",
        username="admin",
        display_name="Admin",
        roles=["super_admin"],
    )


def _install_user_store(monkeypatch, *, on_update):
    row = {
        "user_id": "71727e08-39ab-4a87-9336-93e85c1f2d53",
        "username": "billing7@cobsolution.com",
        "email": "billing7@cobsolution.com",
        "display_name": "Ahmed",
        "is_active": True,
    }

    class Store:
        def get_user_by_id(self, _conn, _user_id):
            return dict(row)

        def get_user_roles(self, _conn, _user_id):
            return ["finance"]

        def count_active_super_admins(self, _conn):
            return 2

        def get_user_by_username(self, _conn, _username):
            return None

        def update_user(self, _conn, _user_id, **kwargs):
            on_update()
            if kwargs.get("display_name"):
                row["display_name"] = kwargs["display_name"]

    class Conn:
        def __init__(self):
            self.savepoints = 0

        def transaction(self):
            self.savepoints += 1
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    conn = Conn()

    @contextmanager
    def fake_connection():
        yield conn

    import cashflow_db.repository as repo

    monkeypatch.setattr(repo, "connection", fake_connection)
    monkeypatch.setattr(repo, "auth_users", Store())
    return conn


def test_update_user_unique_violation_is_409(monkeypatch):
    def boom():
        raise UniqueViolation("duplicate key")

    _install_user_store(monkeypatch, on_update=boom)
    body = UserUpdateBody(
        username="billing7@cobsolution.com",
        display_name="Ahmed",
        email="billing7@cobsolution.com",
        roles=["finance"],
        is_active=True,
    )
    with pytest.raises(HTTPException) as caught:
        update_user("71727e08-39ab-4a87-9336-93e85c1f2d53", body, _actor())
    assert caught.value.status_code == 409
    assert caught.value.detail == "Username already exists"


def test_update_user_activity_failure_still_saves(monkeypatch):
    conn = _install_user_store(monkeypatch, on_update=lambda: None)

    def fail_log(*_args, **_kwargs):
        raise RuntimeError("portal_activity missing")

    monkeypatch.setattr("cashflow_ops.activity_api.log_write", fail_log)
    body = UserUpdateBody(
        username="billing7@cobsolution.com",
        display_name="Ahmed Daker",
        email="billing7@cobsolution.com",
        roles=["finance"],
        is_active=True,
    )
    saved = update_user("71727e08-39ab-4a87-9336-93e85c1f2d53", body, _actor())
    assert saved["display_name"] == "Ahmed Daker"
    assert saved["roles"] == ["finance"]
    assert conn.savepoints == 1


def test_system_login_helper_blocks_system_account():
    from cashflow_db.services.bootstrap_admin import is_system_login
    from cashflow_ops.auth_api import login

    assert is_system_login("system")
    assert is_system_login("system", "System")
    assert "is_system_login" in login.__code__.co_names
