"""Login routes must be mounted when the API module loads.

One failed import used to be swallowed, so /api/auth/login returned 404
while /ready still returned 200.
"""

from __future__ import annotations

from fastapi import HTTPException
import pytest

from cashflow_forecast import api as forecast_api
from cashflow_forecast.api import _AUTH_ROUTES, app, missing_auth_routes, ready


def _paths() -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_auth_login_routes_are_registered():
    paths = _paths()
    missing = [path for path in _AUTH_ROUTES if path not in paths]
    assert missing == []
    assert missing_auth_routes() == []


def test_missing_auth_routes_names_the_gap():
    assert missing_auth_routes(set()) == list(_AUTH_ROUTES)


def test_ready_fails_when_auth_routes_are_missing(monkeypatch):
    monkeypatch.setattr(forecast_api, "missing_auth_routes", lambda: ["/api/auth/login"])
    with pytest.raises(HTTPException) as caught:
        ready()
    assert caught.value.status_code == 503
    detail = caught.value.detail
    assert detail["reason"] == "auth_routes_missing"
    assert detail["missing"] == ["/api/auth/login"]
    assert "errors" in detail
