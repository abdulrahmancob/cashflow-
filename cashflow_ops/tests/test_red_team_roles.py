"""red_agent opens My day only; redteam_leader opens the away board scoped to red agents."""

from __future__ import annotations

import re
from pathlib import Path

from cashflow_db.repository.user_away import board_scope_roles, users_in_board_scope
from cashflow_ops.activity_api import VIEW_ROLES as ACTIVITY_VIEW
from cashflow_ops.auth_api import ALLOWED_ROLES, USER_MANAGE_ROLES
from cashflow_ops.away_api import BOARD_ROLES
from cashflow_ops.billing_analysis_api import VIEW_ROLES as BILLING_VIEW
from cashflow_ops.collection_api import VIEW_ROLES as COLLECTION_VIEW
from cashflow_ops.cpt_audit_api import QUEUE_VIEW
from cashflow_ops.cpt_guide_api import VIEW_ROLES as CPT_VIEW
from cashflow_ops.eligibility_api import EDIT_ROLES, PR_ROLES, VIEW_ROLES
from cashflow_ops.security import ROLE_RED_AGENT, ROLE_REDTEAM_LEADER
from cashflow_ops.work_analytics_api import VIEW_ROLES as ANALYTICS_VIEW


ROOT = Path(__file__).resolve().parents[2]
WORK_ROLE_SETS = (
    USER_MANAGE_ROLES,
    VIEW_ROLES,
    EDIT_ROLES,
    PR_ROLES,
    COLLECTION_VIEW,
    CPT_VIEW,
    QUEUE_VIEW,
    BILLING_VIEW,
    ACTIVITY_VIEW,
    ANALYTICS_VIEW,
)


def _block_after(text: str, marker: str, n: int = 500) -> str:
    return text.split(marker, 1)[1][:n]


def test_red_roles_are_allowed_and_off_work_queues():
    assert ROLE_RED_AGENT == "red_agent"
    assert ROLE_REDTEAM_LEADER == "redteam_leader"
    assert {"red_agent", "redteam_leader"} <= ALLOWED_ROLES
    for roles in WORK_ROLE_SETS:
        assert "red_agent" not in roles
        assert "redteam_leader" not in roles
    assert "red_agent" not in BOARD_ROLES
    assert "redteam_leader" in BOARD_ROLES


def test_leader_scope_is_red_agents_only():
    assert board_scope_roles(["redteam_leader"]) == ("red_agent",)
    users = [
        {"user_id": "a", "is_active": True, "roles": ["red_agent"]},
        {"user_id": "b", "is_active": True, "roles": ["second_submission"]},
        {"user_id": "c", "is_active": True, "roles": ["redteam_leader"]},
        {"user_id": "d", "is_active": False, "roles": ["red_agent"]},
    ]
    kept = users_in_board_scope(users, board_scope_roles(["redteam_leader"]))
    assert [row["user_id"] for row in kept] == ["a"]


def test_red_team_logins_are_created_by_the_migration():
    body = (ROOT / "cashflow_db" / "sql" / "077_red_team_roles.sql").read_text(encoding="utf-8")
    seeds = re.findall(r"\('([^']+)', '([^']+)', '([^']+)', '([^']+)'\)", body)
    by_user = {username: (pw_hash, role) for username, pw_hash, _name, role in seeds}
    assert sum(1 for _h, role in by_user.values() if role == "red_agent") == 9
    assert by_user["mohamed.shalaby@cobsolution.com"][1] == "redteam_leader"
    for username, (pw_hash, _role) in by_user.items():
        assert username == username.lower()
        assert pw_hash.startswith("pbkdf2_sha256$260000$")
    assert "ON CONFLICT (username) DO NOTHING" in body


def test_ui_red_agent_my_day_and_leader_away_board_only():
    layout = (ROOT / "rcm_portal" / "src" / "components" / "Layout.tsx").read_text(encoding="utf-8")
    app = (ROOT / "rcm_portal" / "src" / "App.tsx").read_text(encoding="utf-8")
    users = (ROOT / "rcm_portal" / "src" / "pages" / "Users.tsx").read_text(encoding="utf-8")
    client = (ROOT / "rcm_portal" / "src" / "api" / "client.ts").read_text(encoding="utf-8")

    my_day_nav = _block_after(layout, "to: '/my-day'", 800).split("},", 1)[0]
    assert "'red_agent'" in my_day_nav
    assert "redteam_leader" not in my_day_nav
    away_nav = _block_after(layout, "to: '/away'").split("},", 1)[0]
    assert "'redteam_leader'" in away_nav
    assert "red_agent" not in away_nav
    for marker in ("to: '/eligibility'", "to: '/second-submission'", "to: '/collection'", "to: '/analytics'"):
        block = _block_after(layout, marker).split("},", 1)[0]
        assert "red_agent" not in block, marker
        assert "redteam_leader" not in block, marker

    home = app.split("const ADMIN_HOME", 1)[1].split("export default", 1)[0]
    assert "'redteam_leader'" in home
    away_route = _block_after(app, 'path="/away"').split("</Protected>", 1)[0]
    assert "'redteam_leader'" in away_route
    assert "red_agent" not in away_route

    assert "Red Agent" in users
    assert "Red Team Leader" in users
    assert "'red_agent'" in client
    assert "'redteam_leader'" in client
