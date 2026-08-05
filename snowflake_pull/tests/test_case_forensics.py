"""Tests for observer-only Case forensics platform."""

from __future__ import annotations

import json
from pathlib import Path

from snowflake_pull.case_forensics import (
    CaseTimeline,
    critical_path_sec,
    io_span,
    normalize_endpoint,
    percentile,
    sample_gate_ok,
    summarize,
    weak_fingerprint,
)
from snowflake_pull.scripts import run_case_forensics_report as analyzer


def test_percentile_and_summarize() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(xs, 50) == 5.5
    st = summarize(xs)
    assert st["count"] == 10
    assert st["max"] == 10.0
    assert st["p90"] >= 9.0


def test_phase_id_correlation_and_timeline(tmp_path: Path) -> None:
    from snowflake_pull import case_forensics as f

    f.configure(tmp_path)
    f.bind_case(facility_id="1", case_id="100", patient_id="9")
    tl = CaseTimeline()
    with tl.phase("discovery"):
        assert f.correlation()["phase_name"] == "discovery"
        f._append_jsonl(  # noqa: SLF001
            "http_requests.jsonl",
            {
                "endpoint": "/api/x",
                "phase_name": f.correlation()["phase_name"],
                "phase_id": f.correlation()["phase_id"],
                "elapsed_sec": 1.5,
                "status": 200,
                "facility_id": "1",
                "case_id": "100",
            },
        )
    with tl.phase("pdf_wave"):
        pass
    tl.pdf_count = 2
    tl.bytes_total = 1000
    tl.emit(ok=True)
    phases = list((tmp_path / "case_phases.jsonl").read_text().splitlines())
    assert len(phases) == 1
    row = json.loads(phases[0])
    assert "discovery" in row["phases"]
    assert "pdf_wave" in row["phases"]
    http = json.loads((tmp_path / "http_requests.jsonl").read_text().splitlines()[0])
    assert http["phase_name"] == "discovery"


def test_io_span_records(tmp_path: Path) -> None:
    from snowflake_pull import case_forensics as f

    f.configure(tmp_path)
    tl = CaseTimeline()
    with io_span("manifest_write", tl):
        pass
    assert tl.io_sec.get("manifest_write", 0) >= 0
    assert (tmp_path / "io_events.jsonl").is_file()


def test_critical_path_parallel_pdfs() -> None:
    events = [
        {
            "event_id": "o",
            "name": "open_s1",
            "duration": 2.0,
            "start_rel": 0,
            "end_rel": 2,
            "parent_id": "",
            "depends_on": [],
        },
        {
            "event_id": "d",
            "name": "discovery",
            "duration": 3.0,
            "start_rel": 2,
            "end_rel": 5,
            "parent_id": "",
            "depends_on": ["o"],
        },
        {
            "event_id": "w",
            "name": "pdf_wave",
            "duration": 10.0,
            "start_rel": 5,
            "end_rel": 15,
            "parent_id": "",
            "depends_on": ["d"],
        },
        {
            "event_id": "p1",
            "name": "pdf_job",
            "duration": 10.0,
            "start_rel": 5,
            "end_rel": 15,
            "parent_id": "w",
            "depends_on": [],
        },
        {
            "event_id": "p2",
            "name": "pdf_job",
            "duration": 4.0,
            "start_rel": 5,
            "end_rel": 9,
            "parent_id": "w",
            "depends_on": [],
        },
        {
            "event_id": "m",
            "name": "manifest",
            "duration": 1.0,
            "start_rel": 15,
            "end_rel": 16,
            "parent_id": "",
            "depends_on": ["w"],
        },
    ]
    cp = critical_path_sec(events)
    assert cp["parallelizable_sec"] == 4.0  # 10+4 - max(10)
    assert cp["critical_path_sec"] >= 2 + 3 + 10 + 1 - 0.1


def test_sample_gate() -> None:
    assert sample_gate_ok(199, 10000) is False
    assert sample_gate_ok(200, 4999) is False
    assert sample_gate_ok(200, 5000) is True


def test_normalize_endpoint_and_fingerprint() -> None:
    assert "{id}" in normalize_endpoint("https://x/app/12345/foo")
    assert weak_fingerprint(200, 10, b"abc") == weak_fingerprint(200, 10, b"abc")


def test_analyzer_dual_gate_and_deliverables(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    # Synthetic under-gate sample
    cases = []
    for i in range(5):
        cases.append(
            {
                "facility_id": "1",
                "case_id": str(100 + i),
                "patient_id": "9",
                "wall_sec": 30.0,
                "phases": {
                    "open_s1": 2.0,
                    "discovery": 3.0,
                    "pdf_wave": 20.0,
                    "manifest": 1.0,
                    "inter_case_delay": 4.0,
                    "facility_acquire": 0.5,
                    "fsm": 0.1,
                    "claim": 0.1,
                },
                "idle": {"semaphore": 2.0, "rate_control": 4.0, "browser": 2.0},
                "events": [],
                "pdf_count": 5,
                "bytes_total": 500000,
                "io_sec": {"manifest_write": 0.2},
                "sem_peak": 3,
                "ok": True,
            }
        )
    with (reports / "case_phases.jsonl").open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    with (reports / "http_requests.jsonl").open("w", encoding="utf-8") as f:
        for i in range(20):
            f.write(
                json.dumps(
                    {
                        "endpoint": "/print",
                        "phase_name": "pdf_wave",
                        "elapsed_sec": 1.0,
                        "status": 200,
                        "bytes": 1000,
                        "facility_id": "1",
                        "case_id": "100",
                        "fingerprint": "aaaa",
                        "url": f"https://x/print?id={i}",
                    }
                )
                + "\n"
            )
    result = analyzer.analyze(reports)
    assert result["n_cases"] == 5
    assert result["gate"] is False
    assert (reports / "time_budget.md").is_file()
    assert (reports / "critical_path_report.md").is_file()
    assert (reports / "http_phase_correlation.csv").is_file()
    assert (reports / "optimization_recommendations.md").read_text(
        encoding="utf-8"
    ).startswith("Insufficient sample size.")
    # Budget file mentions Average Case
    assert "Average Case" in (reports / "time_budget.md").read_text(encoding="utf-8")


def test_simulator_cannot_exceed_wall() -> None:
    sw = {"avg": 40.0}
    sp = {"avg": 30.0}
    spar = {"avg": 5.0}
    out = analyzer._simulate(  # noqa: SLF001
        cases=[{"phases": {"inter_case_delay": 10}, "idle": {}}],
        phase_stats={
            "inter_case_delay": {"avg": 10},
            "manifest": {"avg": 1},
            "facility_acquire": {"avg": 2},
            "discovery": {"avg": 3},
        },
        idle_tot={"semaphore": 8.0},
        sw=sw,
        sp=sp,
        spar=spar,
        worker_wall=40.0,
    )
    assert "PDF concurrency" in out["markdown"]
