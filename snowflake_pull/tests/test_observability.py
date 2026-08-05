"""Tests for coverage observability control plane."""

from __future__ import annotations

import json
import time
from pathlib import Path

from snowflake_pull.coverage_run import (
    finish_run,
    init_run,
    release_lock,
    resume_run,
)
from snowflake_pull.observability import ObsContext, correlation_id
from snowflake_pull.unit_state import UnitStateStore


def test_correlation_id_format():
    assert correlation_id(emr_id="1", dos="2026-06-01", facility_id="28029", sf_id_or_hash="abc") == (
        "1|2026-06-01|28029|abc"
    )


def test_obs_jsonl_and_heartbeat(tmp_path: Path):
    obs = ObsContext(
        tmp_path,
        run_id="r1",
        script="test",
        stage="download",
        heartbeat_interval_s=0.2,
        stall_seconds=0.5,
        stall_abort_seconds=10,
        online=True,
    )
    obs.start_heartbeat()
    obs.mark_success(operation="chart_download", emr_id="1", dos="2026-06-01")
    obs.emit(
        "decision",
        operation="chart_download",
        outcome="success",
        decision="chart_downloaded",
        decision_reason="ok",
        emr_id="1",
        dos="2026-06-01",
    )
    time.sleep(0.5)
    hb = tmp_path / "monitoring" / "heartbeat.json"
    assert hb.is_file()
    payload = json.loads(hb.read_text(encoding="utf-8"))
    assert payload["run_id"] == "r1"
    assert payload["stage"] == "download"
    log_path = tmp_path / "logs" / "download.jsonl"
    assert log_path.is_file()
    lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(l.get("decision") == "chart_downloaded" for l in lines)
    obs.stop_heartbeat()


def test_stall_detection(tmp_path: Path):
    obs = ObsContext(
        tmp_path,
        run_id="r2",
        script="test",
        stage="download",
        heartbeat_interval_s=0.2,
        stall_seconds=0.3,
        stall_abort_seconds=2.0,
        online=True,
    )
    obs.start_heartbeat()
    # no mark_success → stall
    time.sleep(0.8)
    obs.stop_heartbeat()
    log_path = tmp_path / "logs" / "download.jsonl"
    events = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(e.get("event") == "stall" for e in events)


def test_unit_fsm_resume(tmp_path: Path):
    store = UnitStateStore(tmp_path / "units.sqlite")
    store.upsert_units(
        [
            {
                "unit_id": "28029:1:2026-06-01",
                "priority": 1,
                "batch_id": "b1",
                "facility_id": "28029",
                "emr_id": "1",
                "dos": "2026-06-01",
            }
        ]
    )
    u = store.claim_next()
    assert u is not None and u.state == "in_progress"
    store.transition(u.unit_id, "downloaded")
    store.transition(u.unit_id, "extracted")
    # crash simulation: in_progress then reclaim to extracted
    store.transition(u.unit_id, "in_progress")
    reset = store.reclaim_stale_in_progress(ttl_seconds=0)
    assert u.unit_id in reset
    again = store.get(u.unit_id)
    assert again is not None
    assert again.state in {"extracted", "queued", "downloaded"}
    store.close()


def test_init_and_resume_run(tmp_path: Path):
    root = tmp_path / "coverage_fix"
    # Create tiny input files referenced by overriding defaults via monkeypatch-like inputs
    inputs = {
        "snowflake": tmp_path / "sf.csv",
        "patients_export": tmp_path / "pe.csv",
        "daily_notes": tmp_path / "dn.csv",
        "cpt_codes": tmp_path / "cpt.csv",
        "reconciliation_visits": tmp_path / "rec.csv",
    }
    for p in inputs.values():
        p.write_text("a\n", encoding="utf-8")

    run = init_run(root=root, operator="tester", script="test", inputs=inputs)
    assert (run.run_dir / "manifest.json").is_file()
    assert (root / "RUN_LOCK").is_file()
    run_id = run.run_id
    # enqueue units and transition one to downloaded
    run.store.upsert_units(
        [{"unit_id": f"u{i}", "priority": i, "batch_id": "toy", "emr_id": str(i)} for i in range(20)]
    )
    for _ in range(5):
        u = run.store.claim_next(batch_id="toy")
        assert u
        run.store.transition(u.unit_id, "downloaded")
    finish_run(run, status="paused_for_test")

    run2 = resume_run(run_id, root=root, script="test", allow_input_drift=False)
    counts = run2.store.counts_by_state()
    assert counts.get("downloaded", 0) == 5
    assert counts.get("queued", 0) == 15
    finish_run(run2, status="completed")
    assert not (root / "RUN_LOCK").exists()
