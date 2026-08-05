"""Unit tests for facility exhaust, delay ladder, retry policy, unified PDF wave."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from snowflake_pull.case_failure_classify import (
    classify_failure,
    is_recoverable,
    next_retry_state,
)
from snowflake_pull.case_optimizer import (
    OptimizerConfig,
    reject_integrity_weakening,
    sort_facilities_by_eta,
)
from snowflake_pull.case_pdf_benchmark import (
    decide_semaphore,
    offline_probe_rows,
    select_best_size,
)
from snowflake_pull.case_rate_control import (
    DELAY_LADDER,
    delay_for_pressure,
    snap_delay_to_ladder,
)
from snowflake_pull.case_unit_state import CaseUnitStateStore, make_case_unit_id


def test_facility_exhaust_largest_first() -> None:
    remaining = {"A": 10, "B": 50, "C": 5}
    ordered = sort_facilities_by_eta(
        remaining, {}, strategy="facility_exhaust", default_avg_sec=30.0
    )
    assert [f for f, _, _ in ordered] == ["B", "A", "C"]


def test_delay_ladder_steps() -> None:
    assert DELAY_LADDER == (0.0, 5.0, 10.0, 20.0, 40.0, 90.0)
    assert delay_for_pressure(0.0, "healthy") == 0.0
    assert delay_for_pressure(0.06, "cooling") == 10.0
    assert delay_for_pressure(0.12, "cooling") == 20.0
    assert delay_for_pressure(0.2, "throttled") == 40.0
    assert delay_for_pressure(0.55, "throttled") == 90.0
    assert snap_delay_to_ladder(80.0) == 90.0
    assert snap_delay_to_ladder(12.0) == 10.0
    assert snap_delay_to_ladder(0.0) == 0.0


def test_case_mismatch_never_recoverable() -> None:
    assert classify_failure(error_type="CaseMismatch") == "CaseMismatch"
    assert is_recoverable("CaseMismatch") is False
    assert is_recoverable("Timeout") is True
    assert is_recoverable("SocketError") is True
    assert next_retry_state(0) == "retry_1"
    assert next_retry_state(1) == "retry_2"
    assert next_retry_state(2) == "retry_3"
    assert next_retry_state(3) == "failed_terminal"


def test_retry_queue_and_mismatch_terminal(tmp_path: Path) -> None:
    store = CaseUnitStateStore(tmp_path / "case_units.sqlite")
    try:
        store.upsert_units(
            [
                {
                    "unit_id": make_case_unit_id("1", "100", "9", "2026-01-15"),
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "100",
                    "patient_id": "9",
                    "dos": "2026-01-15",
                }
            ]
        )
        g = store.claim_next_case_group(batch_id="b", claim_states=("queued",))
        assert g is not None
        store.transition_many(g.unit_ids, "retry_1", error_type="Timeout", force=True)
        assert store.counts_by_state(batch_id="b")["retry_1"] == 1
        g2 = store.claim_next_case_group(batch_id="b", claim_states=("retry_1",))
        assert g2 is not None
        store.transition_many(
            g2.unit_ids, "failed_terminal", error_type="CaseMismatch", force=True
        )
        assert store.claim_next_case_group(batch_id="b") is None
        assert store.counts_by_error_type(batch_id="b")["CaseMismatch"] == 1
    finally:
        store.close()


def test_golden_rule_immutable() -> None:
    assert reject_integrity_weakening(
        OptimizerConfig(include_all_cases=True, require_s1_verify=True)
    )
    assert reject_integrity_weakening(
        OptimizerConfig(include_all_cases=False, require_s1_verify=False)
    )
    assert (
        reject_integrity_weakening(
            OptimizerConfig(include_all_cases=False, require_s1_verify=True)
        )
        == []
    )


def test_bounded_gather_unified_wave() -> None:
    import sys

    scraper = Path(__file__).resolve().parents[2] / "webpt_edco_scraper"
    if str(scraper) not in sys.path:
        sys.path.insert(0, str(scraper))
    from case_download import bounded_gather as bg

    async def _job(i: int):
        await asyncio.sleep(0.001)
        return i

    async def _run():
        jobs = [lambda i=i: _job(i) for i in range(5)]
        return await bg(jobs)

    out = asyncio.run(_run())
    assert sorted(out) == [0, 1, 2, 3, 4]


def test_pdf_semaphore_benchmark_keep_rollback() -> None:
    keep = decide_semaphore(
        size=6, before_cph=40.0, after_cph=50.0, integrity_flat=True
    )
    assert keep["decision"] == "keep"
    roll = decide_semaphore(
        size=8, before_cph=50.0, after_cph=51.0, integrity_flat=True
    )
    assert roll["decision"] == "rollback"
    bad = decide_semaphore(
        size=8, before_cph=40.0, after_cph=80.0, integrity_flat=False
    )
    assert bad["decision"] == "rollback"
    rows = offline_probe_rows(baseline_cph=30.0)
    assert select_best_size(rows) in (3, 4, 5, 6, 8)
