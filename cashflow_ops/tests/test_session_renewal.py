"""Active people keep their session; idle or unattended computers still sign out."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from cashflow_ops.security import (
    JWT_TTL_SECONDS,
    SESSION_COOKIE_NAME,
    AuthUser,
    create_access_token,
    decode_access_token,
    renew_session_if_due,
)

USER = AuthUser(user_id="u1", username="agent@example.com", display_name="Agent", roles=["red_agent"])


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/renew")
    def renew(request: Request, response: Response) -> dict[str, bool]:
        return {"renewed": renew_session_if_due(request, response, USER)}

    return TestClient(app)


def _token(ttl: int, remember: bool = False) -> str:
    return create_access_token(
        user_id=USER.user_id,
        username=USER.username,
        roles=USER.roles,
        display_name=USER.display_name,
        ttl_seconds=ttl,
        remember=remember,
    )


def test_renews_only_past_half_life_and_keeps_remember():
    client = _client()
    fresh = client.post("/renew", cookies={SESSION_COOKIE_NAME: _token(JWT_TTL_SECONDS)})
    assert fresh.json() == {"renewed": False}
    assert SESSION_COOKIE_NAME not in fresh.cookies

    aging = client.post("/renew", cookies={SESSION_COOKIE_NAME: _token(60, remember=True)})
    assert aging.json() == {"renewed": True}
    renewed = decode_access_token(aging.cookies[SESSION_COOKIE_NAME])
    assert renewed["rem"] is True
    assert renewed["exp"] - renewed["iat"] == JWT_TTL_SECONDS
    assert "Max-Age" in aging.headers["set-cookie"]

    session_only = client.post("/renew", cookies={SESSION_COOKIE_NAME: _token(60)})
    assert session_only.json() == {"renewed": True}
    assert "Max-Age" not in session_only.headers["set-cookie"]


def test_bearer_and_missing_or_expired_cookies_are_not_renewed():
    client = _client()
    assert client.post("/renew").json() == {"renewed": False}
    bearer = client.post(
        "/renew",
        headers={"Authorization": f"Bearer {_token(60)}"},
        cookies={SESSION_COOKIE_NAME: _token(60)},
    )
    assert bearer.json() == {"renewed": False}
    expired = client.post("/renew", cookies={SESSION_COOKIE_NAME: _token(-10)})
    assert expired.json() == {"renewed": False}
