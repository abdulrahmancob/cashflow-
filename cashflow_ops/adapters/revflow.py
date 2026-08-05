"""RevFlow EOB acquire adapter."""

from __future__ import annotations

from pathlib import Path

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_script
from cashflow_ops.config import REVFLOW_DIR, REVFLOW_OUTPUT


def discover_and_export(
    *,
    from_date: str,
    to_date: str,
    output: Path | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> dict[str, CmdResult]:
    if skip:
        skipped = CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
        return {"discover": skipped, "export": skipped, "verify": skipped}

    out = output or REVFLOW_OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    scraper = REVFLOW_DIR / "scraper.py"
    discover = run_python_script(
        scraper,
        [
            "discover-eobs",
            "--from-date",
            from_date,
            "--to-date",
            to_date,
            "--output",
            str(out),
        ],
        cwd=REVFLOW_DIR,
        dry_run=dry_run,
        timeout=2 * 3600,
    )
    if not discover.ok and not dry_run:
        return {"discover": discover, "export": discover, "verify": discover}

    export = run_python_script(
        scraper,
        ["export-all", "--output", str(out)],
        cwd=REVFLOW_DIR,
        dry_run=dry_run,
        timeout=8 * 3600,
    )
    verify = run_python_script(
        scraper,
        ["verify-exports", "--output", str(out)],
        cwd=REVFLOW_DIR,
        dry_run=dry_run,
        timeout=1800,
    )
    return {"discover": discover, "export": export, "verify": verify}


def count_exports(output: Path | None = None) -> int:
    out = output or REVFLOW_OUTPUT
    exports = out / "exports"
    if not exports.is_dir():
        return 0
    return sum(1 for p in exports.glob("*.csv") if p.is_file())
