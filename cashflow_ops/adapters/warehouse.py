"""Warehouse migrate / load-all / validate adapters."""

from __future__ import annotations

import json

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_module
from cashflow_ops.config import REPO_ROOT


def migrate(*, dry_run: bool = False) -> CmdResult:
    return run_python_module(
        "cashflow_db", ["migrate"], cwd=REPO_ROOT, dry_run=dry_run, timeout=600
    )


def load_all(*, dry_run: bool = False, limit: int | None = None) -> CmdResult:
    args = ["load-all"]
    if limit is not None:
        args.extend(["--limit", str(limit)])
    return run_python_module(
        "cashflow_db", args, cwd=REPO_ROOT, dry_run=dry_run, timeout=6 * 3600
    )


def validate(*, dry_run: bool = False) -> CmdResult:
    return run_python_module(
        "cashflow_db", ["validate"], cwd=REPO_ROOT, dry_run=dry_run, timeout=1800
    )


def validate_report(result: CmdResult) -> dict:
    if result.dry_run or result.skipped:
        return {"ok": True, "dry_run": True}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": result.ok, "raw": result.stdout[-2000:]}
