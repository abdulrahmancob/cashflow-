"""Load test accounts: a fixed set, switched on with tokens, switched off after."""

from __future__ import annotations

from contextlib import contextmanager

from cashflow_ops import loadtest_users as lt
from cashflow_ops.security import decode_access_token


def test_accounts_are_mostly_desk_with_a_fifth_posting():
    accounts = lt.plan_accounts(50)
    assert len(accounts) == 50
    assert accounts[0]["username"] == "loadtest-01@internal.invalid"
    assert sum(1 for a in accounts if a["kind"] == "posting") == 10
    assert {a["role"] for a in accounts} == {"desk", "posting_team"}
    assert all(a["role"] == "posting_team" for a in accounts[-10:])
    assert lt.plan_accounts(1)[0]["kind"] == "posting"


def test_activate_creates_missing_accounts_and_mints_short_tokens(monkeypatch):
    existing = {"loadtest-01@internal.invalid": {"user_id": "u-existing"}}
    created: list[dict] = []
    updated: list[tuple] = []

    @contextmanager
    def _connection():
        yield object()

    def _create(conn, **kwargs):
        created.append(kwargs)
        return f"u-new-{len(created)}"

    monkeypatch.setattr(lt, "connection", _connection)
    monkeypatch.setattr(lt.auth_users, "get_user_by_username", lambda conn, name: existing.get(name))
    monkeypatch.setattr(lt.auth_users, "create_user", _create)
    monkeypatch.setattr(lt.auth_users, "update_user", lambda conn, uid, **k: updated.append((uid, k)))
    monkeypatch.setattr(lt.auth_users, "set_user_roles", lambda conn, uid, roles: None)
    monkeypatch.setattr(lt, "hash_password", lambda pw: "hashed")

    tokens = lt.activate(5)
    assert len(tokens) == 5
    assert updated == [("u-existing", {"is_active": True})]
    assert len(created) == 4 and all(c["password_hash"] == "hashed" for c in created)
    payload = decode_access_token(tokens[0]["token"])
    assert payload["sub"] == "u-existing"
    assert payload["exp"] - payload["iat"] == lt.TOKEN_TTL_SECONDS
    assert tokens[-1]["kind"] == "posting"


def test_deactivate_only_touches_load_test_accounts(monkeypatch):
    seen: dict = {}

    @contextmanager
    def _connection():
        yield object()

    def _fetchall(conn, sql, params=()):
        seen["sql"] = sql
        return [{"user_id": "a"}, {"user_id": "b"}]

    monkeypatch.setattr(lt, "connection", _connection)
    monkeypatch.setattr(lt.client, "fetchall", _fetchall)
    assert lt.deactivate() == 2
    assert "loadtest-%%@internal.invalid" in seen["sql"]
    assert "is_active = false" in seen["sql"]
