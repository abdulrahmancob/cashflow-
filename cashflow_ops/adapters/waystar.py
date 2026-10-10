"""Waystar rejections / denials adapter."""

from __future__ import annotations

import os
from pathlib import Path

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_script
from cashflow_ops.config import WAYSTAR_DIR, WAYSTAR_OUTPUT


def scrape_rejected(
    *,
    trans_from: str,
    trans_to: str,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    scraper = WAYSTAR_DIR / "scraper.py"
    if not scraper.exists():
        return CmdResult(
            ok=False,
            returncode=127,
            stdout="",
            stderr=f"missing {scraper}",
            skipped=False,
        )
    # Waystar dates are often MM/DD/YYYY
    return run_python_script(
        scraper,
        [
            "--rejected",
            "--trans-from",
            _mdy(trans_from),
            "--trans-to",
            _mdy(trans_to),
            "--run-id",
            f"daily_{trans_from}_{trans_to}",
        ],
        cwd=WAYSTAR_DIR,
        dry_run=dry_run,
        timeout=6 * 3600,
    )


def scrape_denials(*, dry_run: bool = False, skip: bool = False) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    script = WAYSTAR_DIR / "scrape_denials.py"
    if not script.exists():
        return CmdResult(
            ok=True,
            returncode=0,
            stdout="denials script missing — skipped",
            stderr="",
            skipped=True,
        )
    return run_python_script(
        script, [], cwd=WAYSTAR_DIR, dry_run=dry_run, timeout=4 * 3600
    )


def download_recent_claims(*, dry_run: bool = False, skip: bool = False) -> CmdResult:
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    script = WAYSTAR_DIR / "download_claims_listing.py"
    if not script.exists():
        return CmdResult(
            ok=False,
            returncode=127,
            stdout="",
            stderr=f"missing {script}",
            skipped=False,
        )
    out_dir = WAYSTAR_OUTPUT / "claims_recent"
    args = ["--recent", "--out-dir", str(out_dir)]
    raw_months = (os.getenv("WAYSTAR_RECENT_MONTHS") or "2").strip()
    try:
        months_back = int(raw_months)
    except ValueError:
        months_back = 2
    if months_back >= 0:
        args.extend(["--months-back", str(months_back)])
    if (os.getenv("CASHFLOW_WAYSTAR_FRESH_LOGIN") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        args.append("--fresh-login")
    return run_python_script(
        script,
        args,
        cwd=WAYSTAR_DIR,
        dry_run=dry_run,
        timeout=6 * 3600,
    )


def count_waystar_outputs() -> dict[str, int]:
    if not WAYSTAR_OUTPUT.is_dir():
        return {"csv_files": 0, "recent_claims": 0}
    n = sum(1 for p in WAYSTAR_OUTPUT.rglob("*.csv") if p.is_file())
    recent = WAYSTAR_OUTPUT / "claims_recent" / "claims_recent.json"
    merged = WAYSTAR_OUTPUT / "claims_recent" / "claims_merged.json"
    return {
        "csv_files": n,
        "recent_claims": int(recent.is_file() or merged.is_file()),
    }


def recent_claims_path() -> Path | None:
    for cand in (
        WAYSTAR_OUTPUT / "claims_recent" / "claims_recent.json",
        WAYSTAR_OUTPUT / "claims_recent" / "claims_merged.json",
    ):
        if cand.is_file():
            return cand
    return None


def _mdy(iso: str) -> str:
    """YYYY-MM-DD → MM/DD/YYYY when needed."""
    if "/" in iso:
        return iso
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"
