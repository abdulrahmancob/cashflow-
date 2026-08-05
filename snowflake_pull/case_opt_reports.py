"""Optimization report writers for fastest-safe Case drain."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def append_benchmark_row(reports_dir: Path, row: dict[str, Any]) -> Path:
    path = Path(reports_dir) / "benchmark_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _utc(), **row}) + "\n")
    return path


def write_optimization_reports(
    reports_dir: Path,
    *,
    baseline_cph: float,
    current_cph: float,
    peak_cph: float,
    facility_stats: dict[str, Any],
    retry_stats: dict[str, Any],
    failure_counts: dict[str, int],
    benchmark_rows: list[dict[str, Any]],
    restart_events: list[dict[str, Any]],
    integrity: dict[str, Any],
    knobs: dict[str, Any],
) -> dict[str, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    delta = current_cph - baseline_cph
    pct = (delta / baseline_cph * 100.0) if baseline_cph > 0 else 0.0

    paths["performance"] = _write(
        reports_dir / "performance_report.md",
        "\n".join(
            [
                "# Performance Report",
                "",
                f"Generated: {_utc()}",
                "",
                f"- Baseline cph: {baseline_cph:.2f}",
                f"- Current cph: {current_cph:.2f}",
                f"- Peak cph: {peak_cph:.2f}",
                f"- Delta: {delta:+.2f} ({pct:+.1f}%)",
                f"- PDF concurrency: {knobs.get('pdf_concurrency')}",
                f"- Inter-case delay: {knobs.get('inter_case_delay_sec')}",
                f"- Facility strategy: {knobs.get('facility_strategy')}",
                f"- Sticky facility: {knobs.get('sticky_facility')}",
                "",
                "## Integrity",
                "",
                f"- CaseMismatch: {integrity.get('case_mismatch', 0)}",
                f"- Cross-case contamination: {integrity.get('cross_case', 'none detected')}",
                "",
            ]
        ),
    )

    bench_lines = [
        "# Benchmark Report",
        "",
        f"Generated: {_utc()}",
        "",
        "| Change | Before cph | After cph | Delta | Decision |",
        "|---|---:|---:|---:|---|",
    ]
    for r in benchmark_rows:
        bench_lines.append(
            f"| {r.get('change')} | {r.get('before_cph')} | {r.get('after_cph')} | "
            f"{r.get('delta')} | {r.get('decision')} |"
        )
    if not benchmark_rows:
        bench_lines.append("| (pending live windows) | — | — | — | — |")
    bench_lines.append("")
    paths["benchmark"] = _write(reports_dir / "benchmark_report.md", "\n".join(bench_lines))

    paths["optimization"] = _write(
        reports_dir / "optimization_summary.md",
        "\n".join(
            [
                "# Optimization Summary",
                "",
                f"Generated: {_utc()}",
                "",
                "## Kept",
                "",
                "- facility_exhaust sticky clinic drain",
                "- open-once unified PDF wave (chart + edoc)",
                "- adaptive delay ladder 0/5/10/20/40/90",
                "- main-then-retry queues; CaseMismatch never auto-retry",
                "- single WebPT session only",
                "",
                "## Rejected",
                "",
                "- Second browser / parallel WebPT login (WebPT constraint)",
                "- OCR/extract during download (P4)",
                "- Disabling throttle / fighting WAF",
                "",
                f"## Result: {current_cph:.1f} cph (baseline {baseline_cph:.1f})",
                "",
            ]
        ),
    )

    paths["retry"] = _write(
        reports_dir / "retry_summary.md",
        "\n".join(
            [
                "# Retry Summary",
                "",
                f"Generated: {_utc()}",
                "",
                "```json",
                json.dumps(retry_stats, indent=2),
                "```",
                "",
            ]
        ),
    )

    fail_lines = [
        "# Failure Breakdown",
        "",
        f"Generated: {_utc()}",
        "",
        "| Class | Count |",
        "|---|---:|",
    ]
    for k, v in sorted(failure_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        fail_lines.append(f"| {k} | {v} |")
    if not failure_counts:
        fail_lines.append("| (none) | 0 |")
    fail_lines.append("")
    paths["failure"] = _write(
        reports_dir / "failure_breakdown.md", "\n".join(fail_lines)
    )

    paths["facility"] = _write(
        reports_dir / "facility_statistics.md",
        "\n".join(
            [
                "# Facility Statistics",
                "",
                f"Generated: {_utc()}",
                "",
                f"- Facility switches: {facility_stats.get('facility_switches', 0)}",
                f"- Avg cases before switch: {facility_stats.get('avg_cases_before_switch', 0)}",
                f"- Time lost switching (sec): {facility_stats.get('time_lost_switching_sec', 0)}",
                f"- Current facility: {facility_stats.get('current_facility', '')}",
                "",
                "```json",
                json.dumps(facility_stats, indent=2),
                "```",
                "",
            ]
        ),
    )

    restart_path = reports_dir / "worker_restart_log.md"
    existing_lines: list[str] = []
    if restart_path.is_file():
        for line in restart_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- ") and "(no restarts" not in line:
                existing_lines.append(line)
    restart_lines = [
        "# Worker Restart Log",
        "",
        f"Generated: {_utc()}",
        "",
    ]
    seen = set(existing_lines)
    for line in existing_lines:
        restart_lines.append(line)
    for ev in restart_events[-50:]:
        line = f"- {ev.get('at')}: {ev.get('reason')} pids={ev.get('pids')}"
        if line not in seen:
            restart_lines.append(line)
            seen.add(line)
    if len(restart_lines) == 4:
        restart_lines.append("- (no restarts this session)")
    restart_lines.append("")
    paths["restarts"] = _write(restart_path, "\n".join(restart_lines))

    paths["final"] = _write(
        reports_dir / "final_execution_summary.md",
        "\n".join(
            [
                "# Final Execution Summary",
                "",
                f"Generated: {_utc()}",
                "",
                "**Status:** in progress until main+retry queues empty and every case is Completed or Terminal.",
                "",
                f"- Current cph: {current_cph:.2f}",
                f"- Peak cph: {peak_cph:.2f}",
                f"- CaseMismatch: {integrity.get('case_mismatch', 0)}",
                f"- Single WebPT session: YES",
                "",
                "Promote remains gated on Production Ready = YES after queue empty + validation PASS.",
                "",
            ]
        ),
    )
    return paths
