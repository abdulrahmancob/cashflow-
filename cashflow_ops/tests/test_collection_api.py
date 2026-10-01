"""Collection lookup API role gates (no live DB)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cashflow_ops.collection_api import EDIT_ROLES, VIEW_ROLES, router
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


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _app(user: AuthUser, monkeypatch, *, items=None, created=None):
    @contextmanager
    def _connection():
        yield _Conn()

    monkeypatch.setattr("cashflow_db.repository.connection", _connection)
    monkeypatch.setattr(
        "cashflow_db.repository.collection.list_lookups",
        lambda *a, **k: items if items is not None else [],
    )
    monkeypatch.setattr(
        "cashflow_db.repository.collection.lookups_by_kind",
        lambda *a, **k: {
            "denial_reason": items or [],
            "root_cause": [],
            "collection_status": [],
        },
    )
    monkeypatch.setattr(
        "cashflow_db.repository.collection.create_lookup",
        lambda *a, **k: created
        or {
            "lookup_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "kind": k.get("kind") or "denial_reason",
            "label": k.get("label") or "Auth Absent",
        },
    )
    monkeypatch.setattr(
        "cashflow_db.repository.collection.get_lookup",
        lambda *a, **k: created,
    )
    monkeypatch.setattr(
        "cashflow_db.repository.collection.update_lookup",
        lambda *a, **k: created or {"lookup_id": a[1], "label": "x"},
    )
    monkeypatch.setattr(
        "cashflow_db.repository.collection.delete_lookup",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "cashflow_db.repository.portal_activity.record_from_diff",
        lambda *a, **k: None,
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def test_view_roles_include_collectors():
    assert "collector" in VIEW_ROLES
    assert "ops_admin" in VIEW_ROLES
    assert "collector" not in EDIT_ROLES
    assert "finance" not in EDIT_ROLES
    assert "ops_admin" in EDIT_ROLES
    assert "super_admin" in EDIT_ROLES
    assert "sub_admin" in EDIT_ROLES


def test_collector_can_read_not_write(monkeypatch):
    client = TestClient(_app(_user("collector"), monkeypatch, items=[]))
    assert client.get("/api/collection/lookups").status_code == 200
    assert (
        client.post(
            "/api/collection/lookups",
            json={"kind": "denial_reason", "label": "New reason"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/collection/lookups/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            json={"label": "Edited"},
        ).status_code
        == 403
    )
    assert (
        client.delete("/api/collection/lookups/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa").status_code
        == 403
    )


def test_ops_admin_can_add_lookup(monkeypatch):
    client = TestClient(_app(_user("ops_admin"), monkeypatch))
    res = client.post(
        "/api/collection/lookups",
        json={"kind": "denial_reason", "label": "New reason"},
    )
    assert res.status_code == 200
    assert res.json()["item"]["label"] == "New reason"


def test_ui_lookups_tab_is_ops_admin_only():
    page = (ROOT / "rcm_portal" / "src" / "pages" / "Collection.tsx").read_text(
        encoding="utf-8"
    )
    assert "hasRole('ops_admin', 'super_admin', 'sub_admin')" in page
    assert "Lookups" in page


ITEM_ID = "22222222-2222-2222-2222-222222222222"
COLLECTOR_ID = "33333333-3333-3333-3333-333333333333"
POSTING_ID = "44444444-4444-4444-4444-444444444444"


def _elig_app(
    user: AuthUser,
    monkeypatch,
    *,
    members: set[str] | None = None,
    assignee_roles: list[str] | None = None,
    assignee_active: bool = True,
):
    calls: list[dict] = []

    @contextmanager
    def _connection():
        yield _Conn()

    monkeypatch.setattr("cashflow_db.repository.connection", _connection)

    def _members(_conn, ids):
        allowed = members if members is not None else {ITEM_ID}
        return {item_id for item_id in ids if item_id in allowed}

    def _assign(_conn, work_item_id, *, actor_id, assignee_id, reason_key=None, reason_text=None):
        calls.append(
            {
                "work_item_id": work_item_id,
                "assignee_id": assignee_id,
                "actor_id": actor_id,
            }
        )
        return {
            "work_item_id": work_item_id,
            "assigned_to": assignee_id,
            "assigned_to_name": "Collector",
            "patient_name": "Pat",
            "source_visit_status": "denied",
        }

    monkeypatch.setattr("cashflow_db.repository.eligibility.collection_member_ids", _members)
    monkeypatch.setattr(
        "cashflow_db.repository.eligibility.get_work_item",
        lambda _conn, work_item_id: {
            "work_item_id": work_item_id,
            "patient_name": "Pat",
            "source_visit_status": "denied",
        },
    )
    monkeypatch.setattr("cashflow_db.repository.eligibility.assign_work_item", _assign)
    monkeypatch.setattr(
        "cashflow_db.repository.auth_users.get_user_by_id",
        lambda _conn, user_id: {
            "user_id": user_id,
            "is_active": assignee_active,
            "display_name": "Collector",
        },
    )
    monkeypatch.setattr(
        "cashflow_db.repository.auth_users.get_user_roles",
        lambda _conn, _user_id: list(assignee_roles if assignee_roles is not None else ["collector"]),
    )
    monkeypatch.setattr(
        "cashflow_db.repository.portal_activity.record_from_diff",
        lambda *a, **k: None,
    )

    from cashflow_ops.eligibility_api import router as elig_router

    app = FastAPI()
    app.include_router(elig_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), calls


def test_ops_admin_can_bulk_assign_collection(monkeypatch):
    client, calls = _elig_app(_user("ops_admin"), monkeypatch)
    res = client.post(
        "/api/eligibility/items/assign-bulk",
        json={"work_item_ids": [ITEM_ID], "assigned_to": COLLECTOR_ID},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 1
    assert calls == [
        {
            "work_item_id": ITEM_ID,
            "assignee_id": COLLECTOR_ID,
            "actor_id": USER_ID,
        }
    ]


def test_sub_admin_can_bulk_assign_collection(monkeypatch):
    client, calls = _elig_app(_user("sub_admin"), monkeypatch)
    res = client.post(
        "/api/eligibility/items/assign-bulk",
        json={"work_item_ids": [ITEM_ID], "assigned_to": COLLECTOR_ID},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 1
    assert calls == [
        {
            "work_item_id": ITEM_ID,
            "assignee_id": COLLECTOR_ID,
            "actor_id": USER_ID,
        }
    ]


def test_collector_cannot_bulk_assign(monkeypatch):
    client, calls = _elig_app(_user("collector"), monkeypatch)
    res = client.post(
        "/api/eligibility/items/assign-bulk",
        json={"work_item_ids": [ITEM_ID], "assigned_to": COLLECTOR_ID},
    )
    assert res.status_code == 403
    assert calls == []


def test_bulk_assign_rejects_non_collector(monkeypatch):
    client, calls = _elig_app(
        _user("ops_admin"),
        monkeypatch,
        assignee_roles=["posting_team"],
    )
    res = client.post(
        "/api/eligibility/items/assign-bulk",
        json={"work_item_ids": [ITEM_ID], "assigned_to": POSTING_ID},
    )
    assert res.status_code == 400
    assert calls == []


def test_bulk_assign_rejects_non_collection_visit(monkeypatch):
    client, calls = _elig_app(_user("ops_admin"), monkeypatch, members=set())
    res = client.post(
        "/api/eligibility/items/assign-bulk",
        json={"work_item_ids": [ITEM_ID], "assigned_to": COLLECTOR_ID},
    )
    assert res.status_code == 400
    assert calls == []


def test_collection_export_includes_assignee():
    from cashflow_db.repository.eligibility import COLLECTION_EXPORT_COLUMNS

    assert ("assigned_to_code", "Assignee") in COLLECTION_EXPORT_COLUMNS


def test_ui_collection_assignee_is_ops_admin_bulk():
    page = (ROOT / "rcm_portal" / "src" / "pages" / "CollectionQueue.tsx").read_text(
        encoding="utf-8"
    )
    assert ">Assignee<" in page
    assert "hasRole('ops_admin', 'sub_admin')" in page
    assert "/api/eligibility/items/assign-bulk" in page
    assert "/api/eligibility/items/assign-filter" in page
    assert "Assign all in this filter" in page
    assert "posting-users?role=collector" in page


def test_ops_admin_can_assign_filter(monkeypatch):
    client, _calls = _elig_app(_user("ops_admin"), monkeypatch)
    seen: dict = {}

    def _assign(_conn, **kwargs):
        seen.update(kwargs)
        return 12

    monkeypatch.setattr(
        "cashflow_db.repository.eligibility.assign_matching_work_items",
        _assign,
    )
    res = client.post(
        "/api/eligibility/items/assign-filter",
        json={
            "assigned_to": COLLECTOR_ID,
            "month": ["2026-09"],
            "insurance": ["Aetna"],
            "bucket": "denied",
        },
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 12
    assert seen["assignee_id"] == COLLECTOR_ID
    assert seen["actor_id"] == USER_ID
    assert seen["month"] == ["2026-09"]
    assert seen["insurance"] == ["Aetna"]
    assert seen["bucket"] == "denied"


def test_collector_cannot_assign_filter(monkeypatch):
    client, _calls = _elig_app(_user("collector"), monkeypatch)
    called: list[int] = []
    monkeypatch.setattr(
        "cashflow_db.repository.eligibility.assign_matching_work_items",
        lambda *_a, **_k: called.append(1) or 0,
    )
    res = client.post(
        "/api/eligibility/items/assign-filter",
        json={"assigned_to": COLLECTOR_ID, "bucket": "denied"},
    )
    assert res.status_code == 403
    assert called == []


def test_assign_filter_rejects_non_collector(monkeypatch):
    client, _calls = _elig_app(
        _user("ops_admin"),
        monkeypatch,
        assignee_roles=["posting_team"],
    )
    called: list[int] = []
    monkeypatch.setattr(
        "cashflow_db.repository.eligibility.assign_matching_work_items",
        lambda *_a, **_k: called.append(1) or 0,
    )
    res = client.post(
        "/api/eligibility/items/assign-filter",
        json={"assigned_to": POSTING_ID, "bucket": "denied"},
    )
    assert res.status_code == 400
    assert called == []
