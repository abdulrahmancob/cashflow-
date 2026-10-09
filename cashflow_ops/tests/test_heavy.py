"""Heavy downloads are capped, uploads are size-checked, and the API caps statements."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from cashflow_ops import heavy


@pytest.fixture(autouse=True)
def _fresh_slots(monkeypatch):
    import threading

    monkeypatch.setattr(heavy, "_slots", threading.BoundedSemaphore(heavy.MAX_HEAVY))
    monkeypatch.setattr(heavy, "_running", set())


def test_one_per_person_and_three_overall():
    heavy.acquire("a")
    with pytest.raises(HTTPException) as own:
        heavy.acquire("a")
    assert own.value.status_code == 429 and own.value.detail == heavy.OWN_DETAIL
    heavy.acquire("b")
    heavy.acquire("c")
    with pytest.raises(HTTPException) as busy:
        heavy.acquire("d")
    assert busy.value.detail == heavy.BUSY_DETAIL
    heavy.release("a")
    heavy.acquire("d")
    heavy.release("x")
    for user in ("b", "c", "d"):
        heavy.release(user)
    assert heavy._running == set()


def test_guard_releases_even_when_the_handler_fails():
    from cashflow_ops.security import AuthUser

    user = AuthUser(user_id="u1", username="u", display_name="U", roles=[])
    gen = heavy.heavy_guard(user)
    next(gen)
    assert "u1" in heavy._running
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("boom"))
    assert "u1" not in heavy._running


def _guarded(router) -> set[str]:
    found = set()
    for route in router.routes:
        if isinstance(route, APIRoute) and any(d.dependency is heavy.heavy_guard for d in route.dependencies):
            found.add(route.path)
    return found


def test_exports_and_upload_previews_are_guarded():
    from cashflow_ops import (
        away_api,
        checks_deposits_api,
        cpt_audit_api,
        eligibility_api,
        tracker_api,
        work_analytics_api,
    )

    assert {
        "/eligibility/items/export",
        "/eligibility/secondary/export",
        "/eligibility/deductible/export",
        "/eligibility/pr100/export",
        "/eligibility/workload/export",
    } <= _guarded(eligibility_api.router)
    assert any(p.endswith("/items/export") for p in _guarded(cpt_audit_api.router))
    assert any(p.endswith("/export") for p in _guarded(tracker_api.router))
    assert any(p.endswith("/upload/preview") for p in _guarded(tracker_api.router))
    assert any(p.endswith("/upload/preview") for p in _guarded(checks_deposits_api.router))
    assert any(p.endswith("/export") for p in _guarded(work_analytics_api.router))
    assert any(p.endswith("/board/export") for p in _guarded(away_api.router))


def test_upload_previews_run_off_the_event_loop_and_cap_size():
    import inspect

    from cashflow_ops import checks_deposits_api, tracker_api

    for module in (tracker_api, checks_deposits_api):
        assert not inspect.iscoroutinefunction(module.upload_preview)
        assert module.MAX_UPLOAD_BYTES == 20 * 1024 * 1024
        assert "MAX_UPLOAD_BYTES + 1" in inspect.getsource(module.upload_preview)


def test_export_jobs_are_capped(monkeypatch):
    from cashflow_ops import eligibility_api as el

    monkeypatch.setattr(el, "_export_jobs", {})
    monkeypatch.setattr(el.threading, "Thread", lambda **k: type("T", (), {"start": lambda self: None})())
    el._launch_export_job("u1", {"queue": "sheet"})
    with pytest.raises(HTTPException) as own:
        el._launch_export_job("u1", {"queue": "sheet"})
    assert own.value.status_code == 429
    el._launch_export_job("u2", {"queue": "sheet"})
    el._launch_export_job("u3", {"queue": "sheet"})
    with pytest.raises(HTTPException) as busy:
        el._launch_export_job("u4", {"queue": "sheet"})
    assert busy.value.status_code == 429
    for job in el._export_jobs.values():
        job["started_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    el._launch_export_job("u4", {"queue": "sheet"})


def test_statement_limits_only_when_the_process_asks(monkeypatch):
    from cashflow_db import db

    monkeypatch.delenv("CASHFLOW_PG_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("CASHFLOW_PG_IDLE_TX_TIMEOUT_MS", raising=False)
    assert db.session_limits() == {}
    monkeypatch.setenv("CASHFLOW_PG_STATEMENT_TIMEOUT_MS", "300000")
    monkeypatch.setenv("CASHFLOW_PG_IDLE_TX_TIMEOUT_MS", "120000")
    limits = db.session_limits()
    assert "statement_timeout=300000" in limits["options"]
    assert "idle_in_transaction_session_timeout=120000" in limits["options"]
    assert limits["connect_timeout"] == 10
