"""PIU views the work pages read-only; Client Success and Product Owner open My day only."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_db.repository.user_away import board_team
from cashflow_db.repository.work_analytics import SCOPE_OPS, viewer_scope
from cashflow_db.services.bootstrap_admin import verify_password
from cashflow_ops import collection_api, cpt_audit_api, cpt_guide_api, eligibility_api
from cashflow_ops import work_analytics_api
from cashflow_ops.auth_api import ALLOWED_ROLES
from cashflow_ops.security import (
    ROLE_CLIENT_SUCCESS,
    ROLE_PIU,
    ROLE_PRODUCT_OWNER,
    AuthUser,
    get_current_user,
)

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "cashflow_db" / "sql" / "081_team_roles.sql"
NEW_ROLES = ("piu", "client_success", "product_owner")
WRITE_TUPLES = (
    eligibility_api.VIEW_ROLES,
    eligibility_api.EDIT_ROLES,
    eligibility_api.PR_ROLES,
    eligibility_api.TFL_EDIT_ROLES,
    collection_api.EDIT_ROLES,
    cpt_guide_api.EDIT_ROLES,
    cpt_audit_api.QUEUE_EDIT,
)
READ_TUPLES = (
    eligibility_api.READ_ROLES,
    eligibility_api.PR_READ_ROLES,
    eligibility_api.META_ROLES,
    eligibility_api.PR_PAGE_ROLES,
    collection_api.VIEW_ROLES,
    cpt_guide_api.VIEW_ROLES,
    cpt_audit_api.QUEUE_VIEW,
    work_analytics_api.VIEW_ROLES,
)
ROUTERS = (
    eligibility_api.router,
    collection_api.router,
    cpt_guide_api.router,
    cpt_audit_api.router,
    work_analytics_api.router,
)
# Login-only routes every signed-in person uses (presence, My day badges), and the export-job
# download, which only serves a job the caller started.
OPEN_TO_ALL = {
    "/api/analytics/heartbeat",
    "/api/analytics/my-today",
    "/api/analytics/my-assignments",
    "/api/eligibility/items/export-job/{job_id}",
}
# Export jobs are POSTs but only read the queue.
READ_POSTS = {"/api/eligibility/items/export-job"}
SAMPLE_ID = "11111111-1111-1111-1111-111111111111"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        user_id=SAMPLE_ID,
        username="viewer@example.com",
        display_name="Viewer",
        roles=list(roles),
    )


def _client(user: AuthUser, monkeypatch) -> TestClient:
    @contextmanager
    def _no_db():
        raise RuntimeError("no database in tests")
        yield

    monkeypatch.setattr("cashflow_db.repository.connection", _no_db)
    app = FastAPI()
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _routes() -> list[tuple[str, str]]:
    out = []
    for router in ROUTERS:
        for route in router.routes:
            path = "/api" + route.path
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, path))
    return out


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", SAMPLE_ID, path)


def test_new_roles_are_allowed_and_piu_is_read_only():
    assert (ROLE_PIU, ROLE_CLIENT_SUCCESS, ROLE_PRODUCT_OWNER) == NEW_ROLES
    assert set(NEW_ROLES) <= ALLOWED_ROLES
    for roles in WRITE_TUPLES:
        for role in NEW_ROLES:
            assert role not in roles
    for roles in READ_TUPLES:
        assert "piu" in roles
        assert "client_success" not in roles
        assert "product_owner" not in roles
    assert viewer_scope(["piu"]) == SCOPE_OPS
    with pytest.raises(PermissionError):
        viewer_scope(["client_success"])


def test_piu_gets_403_on_every_write_and_passes_every_read_gate(monkeypatch):
    client = _client(_user("piu"), monkeypatch)
    for method, path in _routes():
        if path in OPEN_TO_ALL:
            continue
        res = client.request(method, _url(path), json={})
        if method == "GET" or path in READ_POSTS:
            if path.endswith(("/insights", "/generate/status")):
                assert res.status_code == 403, (method, path)
            else:
                assert res.status_code != 403, (method, path, res.status_code)
        else:
            assert res.status_code == 403, (method, path, res.status_code)


@pytest.mark.parametrize("role", ["client_success", "product_owner"])
def test_my_day_roles_are_kept_off_the_work_pages(role, monkeypatch):
    client = _client(_user(role), monkeypatch)
    for method, path in _routes():
        if path in OPEN_TO_ALL:
            continue
        res = client.request(method, _url(path), json={})
        assert res.status_code == 403, (method, path, res.status_code)


def test_team_logins_are_created_by_the_migration():
    body = SQL.read_text(encoding="utf-8")
    seeds = re.findall(r"\('([^']+)', '([^']+)', '([^']+)', '([^']+)'\)", body)
    by_user = {username: (pw_hash, name, role) for username, pw_hash, name, role in seeds}
    expected = {
        "donia@cobsolution.com": ("Donia@12321", "piu"),
        "finance7@cobsolution.com": ("Nabi@12321", "piu"),
        "basma@cobsolution.com": ("Aref@12321", "piu"),
        "ahmed.emad@cobsolution.com": ("Emad@12321", "client_success"),
        "nour.galal@cobsolution.com": ("Galal@12321", "client_success"),
        "amina.sayed@cobsolution.com": ("Sayed@12321", "client_success"),
        "mohamed.galal@cobsolution.com": ("Galal@12321", "product_owner"),
        "reem.mohamed@cobsolution.com": ("Mohamed@12321", "product_owner"),
        "sara.omar@cobsolution.com": ("Omar@12321", "product_owner"),
    }
    assert set(by_user) == set(expected)
    for username, (password, role) in expected.items():
        pw_hash, _name, seeded_role = by_user[username]
        assert username == username.lower()
        assert seeded_role == role
        assert verify_password(password, pw_hash), username
    lowered = body.lower()
    for role in NEW_ROLES:
        assert f"('{role}'," in body
    assert "on conflict (role_key) do nothing" in lowered
    assert "on conflict (username) do nothing" in lowered
    assert "lower(u.username) = lower(s.username)" in lowered
    assert "add constraint" not in lowered
    assert "delete from" not in lowered


def test_migration_is_registered_last():
    from cashflow_db.db import MIGRATIONS

    assert MIGRATIONS[-1] == "081_team_roles.sql"


def test_board_groups_the_new_teams():
    assert board_team(["piu"]) == "piu"
    assert board_team(["client_success"]) == "client_success"
    assert board_team(["product_owner"]) == "product_owner"


def test_ui_routes_nav_and_view_only_flag():
    layout = (ROOT / "rcm_portal" / "src" / "components" / "Layout.tsx").read_text(encoding="utf-8")
    app = (ROOT / "rcm_portal" / "src" / "App.tsx").read_text(encoding="utf-8")
    auth = (ROOT / "rcm_portal" / "src" / "auth" / "AuthContext.tsx").read_text(encoding="utf-8")

    def block(text: str, marker: str, end: str, n: int = 900) -> str:
        return text.split(marker, 1)[1][:n].split(end, 1)[0]

    my_day = block(layout, "to: '/my-day'", "},")
    for role in NEW_ROLES:
        assert f"'{role}'" in my_day
    for path in (
        "/eligibility",
        "/analytics",
        "/second-submission",
        "/patient-responsibility",
        "/collection",
        "/cpt-guide",
        "/cpt-audit",
    ):
        nav = block(layout, f"to: '{path}'", "},")
        route = block(app, f'path="{path}"', "</Protected>")
        assert "'piu'" in nav, path
        assert "'piu'" in route, path
        for role in ("client_success", "product_owner"):
            assert role not in nav, path
            assert role not in route, path
    home = app.split("const ADMIN_HOME", 1)[1].split("\n", 1)[0]
    for role in NEW_ROLES:
        assert role not in home
    assert "viewOnly" in auth
