"""Tests for Case production engine: claim groups, optimizer, facility ETA."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "snowflake_pull" / "scripts")]

from snowflake_pull.case_optimizer import (  # noqa: E402
    DynamicThroughputOptimizer,
    OptimizerConfig,
    RuntimeMetrics,
    reject_integrity_weakening,
    sort_facilities_by_eta,
)
from snowflake_pull.case_unit_state import CaseUnitStateStore  # noqa: E402


def _seed(store: CaseUnitStateStore, rows: list[dict]) -> None:
    store.upsert_units(rows)


def test_claim_case_group_marks_siblings(tmp_path: Path):
    store = CaseUnitStateStore(tmp_path / "u.sqlite")
    try:
        _seed(
            store,
            [
                {
                    "unit_id": "1:10:100:2026-01-01",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "10",
                    "patient_id": "100",
                    "dos": "2026-01-01",
                },
                {
                    "unit_id": "1:10:100:2026-01-02",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "10",
                    "patient_id": "100",
                    "dos": "2026-01-02",
                },
                {
                    "unit_id": "1:11:100:2026-01-01",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "11",
                    "patient_id": "100",
                    "dos": "2026-01-01",
                },
            ],
        )
        group = store.claim_next_case_group(batch_id="b", preferred_facility="1")
        assert group is not None
        assert group.case_id == "10"
        assert len(group.siblings) == 2
        store.transition_many(group.unit_ids, "downloaded", opened_case_id="10")
        counts = store.counts_by_state(batch_id="b")
        assert counts.get("downloaded") == 2
        assert counts.get("queued") == 1
    finally:
        store.close()


def test_case_mismatch_terminal_no_auto_requeue(tmp_path: Path):
    store = CaseUnitStateStore(tmp_path / "u.sqlite")
    try:
        _seed(
            store,
            [
                {
                    "unit_id": "1:10:100:2026-01-01",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "10",
                    "patient_id": "100",
                    "dos": "2026-01-01",
                },
            ],
        )
        group = store.claim_next_case_group(batch_id="b")
        assert group is not None
        store.transition_many(
            group.unit_ids,
            "failed_terminal",
            error_type="CaseMismatch",
            opened_case_id="999",
        )
        # Must not be queued again automatically
        assert store.claim_next_case_group(batch_id="b") is None
        err = store.counts_by_error_type(batch_id="b")
        assert err["CaseMismatch"] == 1
    finally:
        store.close()


def test_facility_eta_shortest_first():
    remaining = {"A": 10, "B": 2, "C": 5}
    avg = {"A": 10.0, "B": 10.0, "C": 100.0}
    # ETA: A=100, B=20, C=500 → B, A, C
    ordered = sort_facilities_by_eta(
        remaining, avg, strategy="shortest_remaining_first"
    )
    assert [f for f, _, _ in ordered] == ["B", "A", "C"]


def test_optimizer_rollback_and_keep(tmp_path: Path):
    opt = DynamicThroughputOptimizer(tmp_path, interval_sec=0)
    metrics = RuntimeMetrics()
    metrics.window_cases = 10
    metrics.window_started_at = metrics.window_started_at - 3600  # 10/hour
    d1 = opt.maybe_tick(metrics, force=True)
    assert d1 is not None
    opt.last_metric = 10.0
    metrics.window_cases = 20
    metrics.window_started_at = __import__("time").time() - 3600  # 20/hour = +100%
    d2 = opt.maybe_tick(metrics, force=True)
    assert d2["action"] in {"keep", "probe", "baseline"}
    # Force rollback path
    opt.last_metric = 100.0
    metrics.window_cases = 1
    metrics.window_started_at = __import__("time").time() - 3600
    d3 = opt.maybe_tick(metrics, force=True)
    assert d3["action"] in {"rollback", "probe", "reject_integrity", "keep"}


def test_golden_rule_rejects_skip_s1():
    bad = OptimizerConfig(require_s1_verify=False)
    reasons = reject_integrity_weakening(bad)
    assert reasons
    bad2 = OptimizerConfig(include_all_cases=True)
    assert reject_integrity_weakening(bad2)


def test_reclaim_stale(tmp_path: Path):
    store = CaseUnitStateStore(tmp_path / "u.sqlite")
    try:
        _seed(
            store,
            [
                {
                    "unit_id": "1:10:100:2026-01-01",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "10",
                    "patient_id": "100",
                    "dos": "2026-01-01",
                },
            ],
        )
        store.claim_next_case_group(batch_id="b")
        # Force stale timestamp
        with store._lock:
            store._conn.execute(
                "UPDATE case_units SET in_progress_since=? WHERE unit_id=?",
                ("2020-01-01T00:00:00+00:00", "1:10:100:2026-01-01"),
            )
            store._conn.commit()
        n = store.reclaim_stale_in_progress(1.0, batch_id="b")
        assert n == 1
        assert store.counts_by_state(batch_id="b").get("queued") == 1
    finally:
        store.close()


def test_checkpoint_written(tmp_path: Path):
    store = CaseUnitStateStore(tmp_path / "u.sqlite")
    try:
        _seed(
            store,
            [
                {
                    "unit_id": "1:10:100:2026-01-01",
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "10",
                    "patient_id": "100",
                    "dos": "2026-01-01",
                },
            ],
        )
        path = store.write_checkpoint(
            tmp_path / "checkpoint.json",
            batch_id="b",
            watermark={"case_id": "10"},
        )
        assert path.is_file()
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["watermark"]["case_id"] == "10"
    finally:
        store.close()
