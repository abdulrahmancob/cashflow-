"""Missing nightly forecast check (no DB)."""

from __future__ import annotations

from datetime import date

from cashflow_ops import forecast_health


def test_ensure_nightly_forecast_ok_when_success_exists(monkeypatch):
    monkeypatch.setattr(
        forecast_health,
        "latest_success_forecast",
        lambda as_of: {
            "forecast_run_id": "abc",
            "as_of_date": str(as_of),
            "status": "success",
            "created_at": "2026-08-25",
        },
    )
    out = forecast_health.ensure_nightly_forecast(date(2026, 8, 25), resume=False)
    assert out["ok"] is True
    assert out["forecast"]["forecast_run_id"] == "abc"


def test_ensure_nightly_forecast_alerts_when_missing(monkeypatch):
    emitted: dict = {}

    def fake_emit(run_id, **kwargs):
        emitted["run_id"] = run_id
        emitted.update(kwargs)
        return "alert-1"

    monkeypatch.setattr(forecast_health, "latest_success_forecast", lambda as_of: None)
    monkeypatch.setattr(
        "cashflow_ops.state.latest_pipeline_run",
        lambda as_of: {"run_id": "run-1", "status": "failed"},
    )
    monkeypatch.setattr("cashflow_ops.alerts.emit", fake_emit)
    monkeypatch.setattr(
        "cashflow_ops.alerts.notify_run",
        lambda run_id: emitted.__setitem__("notified", run_id),
    )

    out = forecast_health.ensure_nightly_forecast(date(2026, 8, 25), resume=False)
    assert out["ok"] is False
    assert out["run_id"] == "run-1"
    assert emitted["alert_key"] == "missing_nightly_forecast"
    assert emitted["notified"] == "run-1"


def _silence_alerts(monkeypatch) -> dict:
    emitted: dict = {}

    def fake_emit(run_id, **kwargs):
        emitted["run_id"] = run_id
        emitted.update(kwargs)
        return "alert-1"

    monkeypatch.setattr(forecast_health, "latest_success_forecast", lambda as_of: None)
    monkeypatch.setattr("cashflow_ops.alerts.emit", fake_emit)
    monkeypatch.setattr(
        "cashflow_ops.alerts.notify_run",
        lambda run_id: emitted.__setitem__("notified", run_id),
    )
    return emitted


def test_missing_forecast_resumes_worker_run_without_browsers(monkeypatch):
    calls: dict = {}

    def fake_resume(run_id, **kwargs):
        calls["resume"] = (run_id, kwargs)
        return run_id

    monkeypatch.setattr(forecast_health, "latest_success_forecast", lambda as_of: None)
    monkeypatch.setattr(
        "cashflow_ops.state.latest_pipeline_run",
        lambda as_of: {"run_id": "worker-1", "status": "failed", "meta": {}},
    )
    monkeypatch.setattr("cashflow_ops.engine.resume_run", fake_resume)
    _silence_alerts(monkeypatch)

    out = forecast_health.ensure_nightly_forecast(date(2026, 8, 25))
    assert calls["resume"] == ("worker-1", {"skip_scrapers": True})
    assert out["resumed"] is True
    assert out["started"] is False
    assert out["ok"] is False


def test_missing_forecast_starts_skip_scrapers_instead_of_scraper_run(monkeypatch):
    calls: dict = {}

    def fake_start(**kwargs):
        calls["start"] = kwargs
        return "worker-new"

    def fake_resume(*args, **kwargs):
        calls["resume"] = (args, kwargs)
        return "should-not-resume"

    seen = {"n": 0}

    def forecast(as_of):
        seen["n"] += 1
        if seen["n"] == 1:
            return None
        return {
            "forecast_run_id": "fc-1",
            "as_of_date": str(as_of),
            "status": "success",
            "created_at": "2026-08-25T01:00:00+00:00",
        }

    monkeypatch.setattr(forecast_health, "latest_success_forecast", forecast)
    monkeypatch.setattr(
        "cashflow_ops.state.latest_pipeline_run",
        lambda as_of: {
            "run_id": "scraper-1",
            "status": "failed",
            "meta": {"stop_after": "acquire"},
        },
    )
    monkeypatch.setattr("cashflow_ops.engine.start_run", fake_start)
    monkeypatch.setattr("cashflow_ops.engine.resume_run", fake_resume)

    out = forecast_health.ensure_nightly_forecast(date(2026, 8, 25))
    assert "resume" not in calls
    assert calls["start"]["skip_scrapers"] is True
    assert calls["start"]["as_of_date"] == date(2026, 8, 25)
    assert out["ok"] is True
    assert out["started"] is True
    assert out["run_id"] == "worker-new"
