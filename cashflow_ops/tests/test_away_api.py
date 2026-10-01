"""Away API role gates (no live DB)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_ops.away_api import BOARD_ROLES, router
from cashflow_ops.security import AuthUser, get_current_user

ROOT = Path(__file__).resolve().parents[2]
USER_ID = "11111111-1111-1111-1111-111111111111"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        user_id=USER_ID,
        username="jane@example.com",
        display_name="Jane",
        roles=list(roles),
    )


def _app(user: AuthUser, monkeypatch, *, payload=None):
    @contextmanager
    def _connection():
        yield object()

    body = payload or {
        "open": None,
        "warning": "none",
        "break_seconds": 0,
        "prayer_count": 0,
        "people": [],
    }

    monkeypatch.setattr("cashflow_db.repository.connection", _connection)
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.my_away",
        lambda *a, **k: body,
    )
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.start_away",
        lambda *a, **k: body,
    )
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.end_away",
        lambda *a, **k: body,
    )
    monkeypatch.setattr(
        "cashflow_db.repository.user_away.away_board",
        lambda *a, **k: body,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def test_board_is_admin_ops_and_second_submission_lead(monkeypatch):
    assert set(BOARD_ROLES) == {
        "super_admin",
        "sub_admin",
        "ops_admin",
        "second_submission_lead",
    }
    for role in ("posting_team", "collector", "second_submission", "finance", "analytics_viewer", "desk"):
        client = TestClient(_app(_user(role), monkeypatch))
        res = client.get("/api/away/board")
        assert res.status_code == 403, role
    for role in ("super_admin", "sub_admin", "ops_admin", "second_submission_lead"):
        client = TestClient(_app(_user(role), monkeypatch))
        res = client.get("/api/away/board")
        assert res.status_code == 200, role


def test_lead_board_is_team_scoped_and_admin_is_not(monkeypatch):
    captured: dict = {}

    def _board(*_a, **kwargs):
        captured.update(kwargs)
        return {"people": [], "live": [], "days": []}

    lead = TestClient(_app(_user("second_submission_lead"), monkeypatch))
    monkeypatch.setattr("cashflow_db.repository.user_away.away_board", _board)
    assert lead.get("/api/away/board").status_code == 200
    assert captured["role_keys"] == ("second_submission", "second_submission_lead")

    captured.clear()
    admin = TestClient(_app(_user("ops_admin", "second_submission_lead"), monkeypatch))
    monkeypatch.setattr("cashflow_db.repository.user_away.away_board", _board)
    assert admin.get("/api/away/board").status_code == 200
    assert captured["role_keys"] is None


def test_start_maps_rejection_to_400(monkeypatch):
    client = TestClient(_app(_user("posting_team"), monkeypatch))

    def _reject(*_a, **_k):
        raise ValueError("prayer cannot be longer than 10 minutes")

    monkeypatch.setattr("cashflow_db.repository.user_away.start_away", _reject)
    res = client.post("/api/away/start", json={"kind": "prayer", "planned_minutes": 15})
    assert res.status_code == 400
    assert "10 minutes" in res.json()["detail"]


def test_ui_away_board_roles_and_header_control():
    layout = (ROOT / "rcm_portal" / "src" / "components" / "Layout.tsx").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "rcm_portal" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "AwayControl" in layout
    nav = layout.split("to: '/away'", 1)[1][:400]
    assert "super_admin" in nav
    assert "sub_admin" in nav
    assert "ops_admin" in nav
    assert "second_submission_lead" in nav
    nav_roles = nav.split("},", 1)[0].replace("second_submission_lead", "")
    assert "posting_team" not in nav_roles
    assert "second_submission" not in nav_roles
    route = app.split('path="/away"', 1)[1][:500]
    assert "super_admin" in route
    assert "ops_admin" in route
    assert "second_submission_lead" in route
    assert "AwayBoardPage" in route
    board = (ROOT / "rcm_portal" / "src" / "pages" / "AwayBoard.tsx").read_text(encoding="utf-8")
    assert "offline_since" in board
    assert "Offline" in board
    my_day = (ROOT / "rcm_portal" / "src" / "pages" / "MyDay.tsx").read_text(encoding="utf-8")
    assert "offline_since" not in my_day
    heartbeat = (ROOT / "rcm_portal" / "src" / "auth" / "useActivityHeartbeat.ts").read_text(
        encoding="utf-8"
    )
    assert "isAwayIdle" in heartbeat
    assert "presence: true" in heartbeat
    assert "beforeunload" in heartbeat
    assert "sendBeacon" in heartbeat
    assert "closed: true" in heartbeat
    assert "deviceKeepsPresence" in heartbeat
    assert "DeskPresenceButton" in layout
    prompt = (ROOT / "rcm_portal" / "src" / "components" / "DeskPresenceButton.tsx").read_text(
        encoding="utf-8"
    )
    assert "enableDeskIdle" in prompt
    assert "Allow" in prompt
    assert "Chrome or Edge" in prompt
    page = (ROOT / "rcm_portal" / "src" / "pages" / "AwayBoard.tsx").read_text(
        encoding="utf-8"
    )
    assert 'type="date"' in page
    assert "Away now" in page
    assert "Everyone is at their desk" in page
    assert "Offline" in page
    assert "person.online" in page
    assert "At computer" in page
    assert "Idle" in page
    assert "Search people" in page
    assert "Logged in" in page
    assert "No login" in page
    assert "No one matches" in page
    assert "board.live" in page or "live.map" in page
