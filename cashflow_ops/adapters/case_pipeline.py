"""Case-centric download / enrich adapters (snowflake_pull scripts)."""

from __future__ import annotations

from pathlib import Path

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_script
from cashflow_ops.config import CASE_PIPELINE_DIR, REPO_ROOT, SNOWFLAKE_DIR, WEBPT_OUTPUT


def _scripts() -> Path:
    return SNOWFLAKE_DIR / "scripts"


def build_case_schedule(
    *,
    schedule_export: Path,
    start: str,
    end: str,
    out_dir: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    out = out_dir or (CASE_PIPELINE_DIR / "schedule")
    return run_python_script(
        _scripts() / "build_case_schedule.py",
        [
            "--schedule-export",
            str(schedule_export),
            "--out-dir",
            str(out),
            "--start",
            start,
            "--end",
            end,
        ],
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=1800,
    )


def run_case_download(
    *,
    schedule_export: Path | None = None,
    start: str,
    end: str,
    out_dir: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
    phase: str = "download",
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    out = out_dir or CASE_PIPELINE_DIR
    sched = schedule_export or _latest_schedule_csv()
    args = [
        "--schedule-export",
        str(sched),
        "--out-dir",
        str(out),
        "--start",
        start,
        "--end",
        end,
        "--skip-schedule-rebuild",
        "--phase",
        phase,
        "--auto",
    ]
    return run_python_script(
        _scripts() / "run_case_pipeline.py",
        args,
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=12 * 3600,
    )


def run_case_ocr_batch(
    *,
    out_dir: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    script = _scripts() / "run_case_ocr_batch.py"
    if not script.exists():
        return CmdResult(
            ok=True,
            returncode=0,
            stdout="ocr batch script missing — skipped",
            stderr="",
            skipped=True,
        )
    args = ["--out-dir", str(out_dir or CASE_PIPELINE_DIR)]
    return run_python_script(
        script, args, cwd=REPO_ROOT, dry_run=dry_run, timeout=12 * 3600
    )


def run_full_case_enrich(
    *,
    out_dir: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    script = _scripts() / "run_full_case_data_pipeline.py"
    if not script.exists():
        # Fall back to OCR batch alone
        return run_case_ocr_batch(out_dir=out_dir, dry_run=dry_run, skip=False)
    return run_python_script(
        script,
        ["--out-dir", str(out_dir or CASE_PIPELINE_DIR)],
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=12 * 3600,
    )


def _latest_schedule_csv() -> Path:
    matches = sorted(WEBPT_OUTPUT.glob("schedule_visits_*.csv"))
    if matches:
        return matches[-1]
    return WEBPT_OUTPUT / "schedule_visits.csv"
