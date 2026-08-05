"""Snowflake KPI pull adapter (staging only — never case SoT)."""

from __future__ import annotations

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_module
from cashflow_ops.config import REPO_ROOT, SNOWFLAKE_OUTPUT


def pull_kpi(
    *,
    start: str | None = None,
    end: str | None = None,
    dry_run: bool = False,
    skip: bool = False,
) -> CmdResult:
    """Pull Snowflake billing KPI CSV. Date window is controlled by env/SQL defaults."""
    del start, end  # reserved for future SQL-template wiring
    if skip:
        return CmdResult(ok=True, returncode=0, stdout="skipped", stderr="", skipped=True)
    return run_python_module(
        "snowflake_pull",
        [],
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=2 * 3600,
    )


def billing_csv_count() -> int:
    if not SNOWFLAKE_OUTPUT.is_dir():
        return 0
    return sum(1 for p in SNOWFLAKE_OUTPUT.glob("billing_*.csv") if p.is_file())
