"""Forecast build adapter (DB SoT)."""

from __future__ import annotations

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_module
from cashflow_ops.config import REPO_ROOT


def build_from_db(*, dry_run: bool = False, as_of: str | None = None) -> CmdResult:
    args = ["build", "--from-db"]
    if as_of:
        args.extend(["--as-of", as_of])
    return run_python_module(
        "cashflow_forecast",
        args,
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=4 * 3600,
    )
