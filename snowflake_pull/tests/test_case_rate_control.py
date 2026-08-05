"""Unit tests for WebPT-safe adaptive rate control + daily snapshots."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from snowflake_pull.case_optimizer import (
    DynamicThroughputOptimizer,
    OptimizerConfig,
    RuntimeMetrics,
    reject_integrity_weakening,
)
from snowflake_pull.case_rate_control import (
    AdaptiveRateController,
    append_daily_snapshot,
)


def test_403_spike_throttles_and_reduces_pressure() -> None:
    ctrl = AdaptiveRateController()
    ctrl.knobs.pdf_concurrency = 6
    ctrl.knobs.inter_case_delay_sec = 0.0

    # Seed some traffic then spike 403s
    for _ in range(5):
        ctrl.record(200, 0.2)
    for _ in range(10):
        ctrl.record(403, 0.1, kind="edoc")

    assert ctrl.state == "throttled"
    assert ctrl.knobs.pdf_concurrency < 6
    # Delay ladder: heavy/extreme pressure maps to 20/40/90 — never below cooling step
    assert ctrl.knobs.inter_case_delay_sec in (20.0, 40.0, 90.0)
    assert ctrl.throttle_score() >= 0.15


def test_probe_up_respects_ceiling_eight() -> None:
    from snowflake_pull.case_rate_control import PDF_CONCURRENCY_CEILING

    assert PDF_CONCURRENCY_CEILING == 8
    ctrl = AdaptiveRateController()
    ctrl.knobs.pdf_concurrency = 7
    ctrl.state = "healthy"
    now = time.time()
    ctrl.healthy_since = now - (12 * 60)
    ctrl.last_down_at = now - (12 * 60)
    ctrl.last_up_probe_at = 0.0
    for _ in range(20):
        ctrl.record(200, 0.1)
    assert ctrl.knobs.pdf_concurrency == 8
    # Cannot exceed ceiling
    ctrl.last_up_probe_at = 0.0
    ctrl.last_down_at = now - (20 * 60)
    ctrl.healthy_since = now - (20 * 60)
    ctrl._probe_up(time.time())
    assert ctrl.knobs.pdf_concurrency == 8


def test_edoc_storm_defers_edocs() -> None:
    ctrl = AdaptiveRateController()
    ctrl.knobs.edoc_enabled = True
    for _ in range(3):
        ctrl.record(403, 0.1, kind="edoc")
    assert ctrl.knobs.edoc_enabled is False
    assert ctrl.edoc_deferred_until > time.time()


def test_healthy_probe_up_slow_no_oscillation() -> None:
    ctrl = AdaptiveRateController()
    ctrl.knobs.pdf_concurrency = 1
    ctrl.knobs.inter_case_delay_sec = 30.0
    ctrl.state = "healthy"
    now = time.time()
    ctrl.healthy_since = now - (31 * 60)
    ctrl.last_down_at = now - (10 * 60)
    ctrl.last_up_probe_at = 0.0

    # Fill window with healthy traffic
    for _ in range(20):
        ctrl.record(200, 0.1)

    assert ctrl.state == "healthy"
    assert ctrl.knobs.pdf_concurrency == 2  # +1 only

    # Immediate second probe must not jump 1→3 / double-step
    before = ctrl.knobs.pdf_concurrency
    ctrl._probe_up(time.time())
    assert ctrl.knobs.pdf_concurrency == before  # blocked by MIN_UP_PROBE_SEC


def test_optimizer_respects_throttle_no_raise(tmp_path: Path) -> None:
    opt = DynamicThroughputOptimizer(tmp_path)
    opt.config.pdf_concurrency = 2
    opt.last_metric = 40.0
    metrics = RuntimeMetrics(pdf_concurrency=2)
    metrics.window_cases = 10
    metrics.window_started_at = time.time() - 60

    decision = opt.maybe_tick(
        metrics,
        force=True,
        throttle={
            "state": "throttled",
            "throttle_score": 0.4,
            "knobs": {
                "pdf_concurrency": 1,
                "inter_case_delay_sec": 45.0,
                "edoc_enabled": False,
            },
        },
    )
    assert decision is not None
    assert decision["reason"] == "throttle_backoff"
    assert opt.config.pdf_concurrency == 1
    assert opt.config.inter_case_delay_sec == 45.0
    assert opt.config.edoc_enabled is False


def test_daily_snapshot_appends_without_clobber(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    body = "# Production Validation Report\n\n## Existing\n\nkeep-me\n"
    md = reports / "production_validation_report.md"
    md.write_text(body, encoding="utf-8")

    append_daily_snapshot(
        reports,
        payload={
            "queued_units": 100,
            "queued_cases": 50,
            "completed_cases": 10,
            "avg_cases_per_hour": 40.0,
            "peak_cases_per_hour": 95.0,
            "retry_rate": 0.05,
            "auth_renewals": 1,
            "throttle_events_24h": 12,
            "throttle_state": "cooling",
            "eta_hours": 20.0,
            "case_mismatch": 0,
            "download_empty": 0,
            "case_open_failed": 2,
        },
    )
    text = md.read_text(encoding="utf-8")
    assert "keep-me" in text
    assert "Daily Progress Snapshot" in text
    assert "cooling" in text
    jsonl = reports / "daily_snapshots.jsonl"
    assert jsonl.is_file()
    rows = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l]
    assert len(rows) == 1
    assert rows[0]["queued_cases"] == 50


def test_golden_rule_rejects_include_all_and_skip_s1() -> None:
    bad = OptimizerConfig(include_all_cases=True, require_s1_verify=True)
    assert "include_all_cases=True forbidden" in reject_integrity_weakening(bad)
    bad2 = OptimizerConfig(include_all_cases=False, require_s1_verify=False)
    assert "skipping S1 verify forbidden" in reject_integrity_weakening(bad2)
    good = OptimizerConfig(include_all_cases=False, require_s1_verify=True)
    assert reject_integrity_weakening(good) == []
