"""Activity log roles, write-path one-event, and Admin nav."""

from __future__ import annotations

import inspect
from pathlib import Path

from cashflow_ops import auth_api, eligibility_api
from cashflow_ops.activity_api import VIEW_ROLES
from cashflow_ops.security import ROLE_OPS_ADMIN, ROLE_SUB_ADMIN, ROLE_SUPER


ROOT = Path(__file__).resolve().parents[2]


def _block_after(text: str, marker: str, n: int = 500) -> str:
    return text.split(marker, 1)[1][:n]


def test_view_roles_are_super_sub_and_ops_admin():
    assert set(VIEW_ROLES) == {ROLE_SUPER, ROLE_SUB_ADMIN, ROLE_OPS_ADMIN}
    assert "collector" not in VIEW_ROLES
    assert "finance" not in VIEW_ROLES
    assert "posting_team" not in VIEW_ROLES


def test_eligibility_patch_logs_one_activity():
    src = inspect.getsource(eligibility_api.patch_item)
    assert src.count("_log_work_item(") == 1
    assert "keys = list(updates.keys())" in src


def test_generate_logs_one_sheet_event():
    src = inspect.getsource(eligibility_api._run_generate_job)
    assert "eligibility-sheet" in src
    assert "Generated from recon" in src
    assert "for visit" not in src.lower()


def test_user_update_does_not_log_password():
    src = inspect.getsource(auth_api._update_user_row)
    assert "password_reset" in src
    assert '"password":' not in src
    assert "body.password" not in src.split("after = {", 1)[1].split("}", 1)[0]


def test_ui_activity_for_ops_admin_not_platform_or_database():
    layout = (ROOT / "rcm_portal" / "src" / "components" / "Layout.tsx").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "rcm_portal" / "src" / "App.tsx").read_text(encoding="utf-8")
    page = (ROOT / "rcm_portal" / "src" / "pages" / "Activity.tsx").read_text(
        encoding="utf-8"
    )

    assert "to: '/activity'" in layout
    activity_nav = _block_after(layout, "to: '/activity'")
    assert "ops_admin" in activity_nav.split("},", 1)[0]
    assert "sub_admin" in activity_nav.split("},", 1)[0]
    assert "super_admin" in activity_nav.split("},", 1)[0]

    platform_nav = _block_after(layout, "to: '/platform'")
    assert "ops_admin" not in platform_nav.split("},", 1)[0]
    database_nav = _block_after(layout, "to: '/database'")
    assert "ops_admin" not in database_nav.split("},", 1)[0]

    activity_route = _block_after(app, 'path="/activity"')
    assert "ops_admin" in activity_route
    assert "sub_admin" in activity_route
    platform_route = _block_after(app, 'path="/platform"')
    assert "ops_admin" not in platform_route
    database_route = _block_after(app, 'path="/database"')
    assert "ops_admin" not in database_route

    assert "ActivityPage" in app
    assert "entity_label" in page
    assert "/activity" in page or "Activity" in page
