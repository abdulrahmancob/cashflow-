"""Tests for measurement-gated speed control knobs."""

from __future__ import annotations

from pathlib import Path

from snowflake_pull.case_speed_control import (
    ADAPTIVE_BATCH_ENABLED,
    FacilityCacheEntry,
    SpeedController,
    s1_should_abort_request,
    write_batch_proof_note,
)
from snowflake_pull.case_unit_state import CaseUnitStateStore, make_case_unit_id
from snowflake_pull.scripts.run_case_download_worker import (
    CLAIM_LOCAL_ORDER,
    _remaining_any_by_facility,
)


def test_adaptive_batch_off_by_default(tmp_path: Path) -> None:
    assert ADAPTIVE_BATCH_ENABLED is False
    p = write_batch_proof_note(tmp_path)
    text = p.read_text(encoding="utf-8")
    assert "OFF" in text
    assert "Golden Rule" in text


def test_facility_cache_ttl(tmp_path: Path) -> None:
    sc = SpeedController(tmp_path)
    sc.put_facility_cache("42", jwt_remaining_sec=600.0, csrf="tok", ttl_sec=600)
    ent = sc.get_facility_cache("42")
    assert ent is not None
    assert ent.csrf == "tok"
    assert ent.is_valid()
    # Expired entry
    dead = FacilityCacheEntry(
        clinic_id="9", jwt_remaining_sec=10.0, last_refresh=1.0, expires_at=1.0
    )
    assert not dead.is_valid()


def test_discovery_rollback_on_regression(tmp_path: Path) -> None:
    sc = SpeedController(tmp_path)
    sc.mark_parallel_probe_ok()
    assert sc.state.discovery_parallel_ok is True
    for _ in range(5):
        sc.note_discovery_sec(10.0)
    assert sc.state.discovery_baseline_sec is not None
    # Force window of worse discovery
    for _ in range(8):
        sc.note_discovery_sec(12.0)
    assert sc.state.discovery_parallel_ok is False
    assert any(r.get("reason") == "discovery_regression" for r in sc.state.rollbacks)


def test_s1_abort_keeps_app_apis() -> None:
    assert s1_should_abort_request(
        "https://app.webpt.com/images/navBar/navTop.jpg", "image"
    )
    assert s1_should_abort_request(
        "https://app.webpt.com/js/lib/common.js", "script"
    )
    assert not s1_should_abort_request(
        "https://app.webpt.com/patientChartNote.php?ID=1&CaseID=2", "document"
    )
    assert not s1_should_abort_request(
        "https://gateway.webpt.com/graphql", "fetch"
    )
    assert s1_should_abort_request("https://cdn.pendo.io/x.js", "script")


def test_open_s1_rollback_on_regression(tmp_path: Path) -> None:
    sc = SpeedController(tmp_path)
    assert sc.should_use_s1_light_nav() is True
    for _ in range(5):
        sc.note_open_s1_sec(10.0)
    assert sc.state.open_s1_baseline_sec is not None
    for _ in range(8):
        sc.note_open_s1_sec(12.0)
    assert sc.should_use_s1_light_nav() is False
    assert any(r.get("reason") == "open_s1_regression" for r in sc.state.rollbacks)


def test_telemetry_abort_gate(tmp_path: Path) -> None:
    sc = SpeedController(tmp_path)
    for _ in range(20):
        sc.observe_telemetry(elapsed_sec=2.0, nbytes=100)
        sc.note_case_wall_for_telemetry(10.0)  # 20% share
    assert sc.should_abort_telemetry() is True


def test_facility_local_remaining_includes_retries(tmp_path: Path) -> None:
    store = CaseUnitStateStore(tmp_path / "u.sqlite")
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
                },
                {
                    "unit_id": make_case_unit_id("1", "200", "9", "2026-01-16"),
                    "batch_id": "b",
                    "facility_id": "1",
                    "case_id": "200",
                    "patient_id": "9",
                    "dos": "2026-01-16",
                },
                {
                    "unit_id": make_case_unit_id("2", "300", "9", "2026-01-17"),
                    "batch_id": "b",
                    "facility_id": "2",
                    "case_id": "300",
                    "patient_id": "9",
                    "dos": "2026-01-17",
                },
            ]
        )
        g = store.claim_next_case_group(batch_id="b", claim_states=("queued",))
        assert g is not None
        store.transition_many(g.unit_ids, "retry_1", error_type="Timeout", force=True)
        rem_any = _remaining_any_by_facility(store, batch_id="b")
        assert rem_any["1"] >= 2  # one retry + one still queued on clinic 1
        # Main before local retry: still-queued case 200 before retry of 100
        g2 = store.claim_next_case_group(
            batch_id="b",
            preferred_facility="1",
            claim_states=CLAIM_LOCAL_ORDER,
        )
        assert g2 is not None
        assert g2.facility_id == "1"
        assert g2.case_id == "200"
        store.transition_many(g2.unit_ids, "downloaded", force=True)
        # After main empty for clinic 1 → local retry of 100 (not clinic 2)
        g3 = store.claim_next_case_group(
            batch_id="b",
            preferred_facility="1",
            claim_states=CLAIM_LOCAL_ORDER,
        )
        assert g3 is not None
        assert g3.facility_id == "1"
        assert g3.case_id == g.case_id
    finally:
        store.close()
