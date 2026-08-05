"""WebPT scraper adapters (schedule, checkouts, patient payments)."""

from __future__ import annotations

from pathlib import Path

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_script
from cashflow_ops.config import WEBPT_DIR, WEBPT_OUTPUT


def _scraper() -> Path:
    return WEBPT_DIR / "scraper.py"


def export_schedule(
    *,
    start: str,
    end: str,
    output: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    out = output or WEBPT_OUTPUT
    return run_python_script(
        _scraper(),
        [
            "--headless",
            "export-schedule",
            "--start-date",
            start,
            "--end-date",
            end,
            "--output",
            str(out),
        ],
        cwd=WEBPT_DIR,
        dry_run=dry_run,
        timeout=6 * 3600,
    )


def export_checkouts(
    *,
    output: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    out = output or WEBPT_OUTPUT
    return run_python_script(
        _scraper(),
        ["--headless", "export-checkouts", "--output", str(out)],
        cwd=WEBPT_DIR,
        dry_run=dry_run,
        timeout=2 * 3600,
    )


def scrape_patient_payments(
    *,
    output: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    out = output or WEBPT_OUTPUT
    return run_python_script(
        _scraper(),
        ["--headless", "scrape-patient-payments", "--output", str(out)],
        cwd=WEBPT_DIR,
        dry_run=dry_run,
        timeout=4 * 3600,
    )
