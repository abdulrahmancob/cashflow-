"""Reconciliation + insurance behavior adapters."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from cashflow_ops.adapters.subprocess_runner import CmdResult, run_python_module, run_python_script
from cashflow_ops.config import REPO_ROOT, WEBPT_DIR, WEBPT_LEGACY_OUTPUT

log = logging.getLogger(__name__)


def reconcile_from_db(
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "line_count": 0}
    from cashflow_reconcile.db_io import run_reconciliation_from_db

    summary = run_reconciliation_from_db(
        service_from=service_from,
        service_to=service_to,
        emit_csv=False,
    )
    return {"ok": True, "summary": summary}


def insurance_behavior(*, dry_run: bool = False) -> CmdResult:
    return run_python_module(
        "cashflow_reconcile.insurance_behavior",
        ["--from-db"],
        cwd=REPO_ROOT,
        dry_run=dry_run,
        timeout=2 * 3600,
    )


def audit_billing(*, dry_run: bool = False) -> CmdResult:
    script = WEBPT_DIR / "scripts" / "audit_billing.py"
    extracted = WEBPT_LEGACY_OUTPUT / "extracted"
    case_extracted = (
        REPO_ROOT
        / "snowflake_pull"
        / "artifacts"
        / "side_by_side_case"
        / "extracted"
    )
    src = case_extracted if (case_extracted / "daily_notes.csv").exists() else extracted
    out = WEBPT_LEGACY_OUTPUT / "audit"
    if not script.exists():
        return CmdResult(
            ok=True,
            returncode=0,
            stdout="audit_billing.py missing — skipped",
            stderr="",
            skipped=True,
        )
    return run_python_script(
        script,
        ["--extracted", str(src), "--out", str(out)],
        cwd=WEBPT_DIR,
        dry_run=dry_run,
        timeout=2 * 3600,
    )
