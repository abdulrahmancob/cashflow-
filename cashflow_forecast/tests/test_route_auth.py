"""Every /api route is closed without a session unless it is explicitly public."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cashflow_forecast import api
from cashflow_ops.security import AuthUser, create_access_token


def _routes() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in api.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        path = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            found.append((method, path))
    return found


ROUTES = _routes()
CLOSED = [(method, path) for method, path in ROUTES if not api.is_public_path(path)]


def test_public_list_is_short_and_known():
    public = sorted({path for _method, path in ROUTES if api.is_public_path(path)})
    assert public == [
        "/api/auth/login",
        "/api/auth/logout",
        "/api/desk/ping",
        "/api/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/desk/ping",
        "/api/v1/health",
    ]
    assert len(CLOSED) > 100


@pytest.mark.parametrize(("method", "path"), CLOSED)
def test_route_rejects_missing_and_forged_sessions(method, path):
    client = TestClient(api.app, raise_server_exceptions=False)
    missing = client.request(method, path, json={})
    assert missing.status_code == 401, (method, path, missing.status_code)
    forged = client.request(method, path, json={}, cookies={"rcm_session": "forged.token.value"})
    assert forged.status_code == 401, (method, path, forged.status_code)


def test_finance_data_needs_a_finance_role(monkeypatch):
    agent = AuthUser(user_id="u1", username="agent", display_name="Agent", roles=["red_agent"])
    monkeypatch.setattr("cashflow_ops.security.auth_user_from_token", lambda token: agent)
    token = create_access_token(user_id="u1", username="agent", roles=["red_agent"], display_name="Agent")
    client = TestClient(api.app, raise_server_exceptions=False)
    for path in ("/api/cash/overview", "/api/exec/scorecard", "/api/day-ahead", "/api/v1/kpi", "/api/unbanked.csv"):
        res = client.get(path, cookies={"rcm_session": token})
        assert res.status_code == 403, (path, res.status_code)


def test_health_reveals_nothing():
    res = TestClient(api.app).get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_path_rules():
    assert api.is_public_path("/alive")
    assert api.is_public_path("/assets/index.js")
    assert api.is_public_path("/api/v1/desk/ping")
    assert not api.is_public_path("/api/cash/overview")
    assert not api.is_public_path("/api/auth/me")
    assert api.is_finance_path("/api/v1/exec/scorecard")
    assert api.is_finance_path("/api/unbanked.csv")
    assert not api.is_finance_path("/api/away/board")
    assert not api.is_finance_path("/api/cashflow-something")
