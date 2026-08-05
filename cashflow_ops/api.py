"""Platform / ops HTTP routes (mounted under /api/v1 and /api)."""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from cashflow_ops.security import ROLE_SUPER, AuthUser, require_roles

router = APIRouter(tags=["platform"])

SCHEMA_VERSION = "017"
PKG_VERSION = "0.3.0"


def _git_sha() -> str:
    env = os.getenv("GIT_SHA") or os.getenv("GITHUB_SHA")
    if env:
        return env[:12]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


@router.get("/platform")
def platform_status(
    _: AuthUser = Depends(require_roles(ROLE_SUPER, "finance")),
) -> dict[str, Any]:
    from cashflow_ops import state

    last = state.latest_pipeline_run()
    last_payload = None
    status = "healthy"
    if last:
        last_payload = {
            "run_id": str(last["run_id"]),
            "as_of_date": str(last["as_of_date"]),
            "status": last["status"],
            "dataset_version": last.get("dataset_version"),
        }
        if last["status"] == "failed":
            status = "degraded"
        elif last["status"] == "running":
            status = "healthy"
    else:
        status = "degraded"

    return {
        "version": PKG_VERSION,
        "git_sha": _git_sha(),
        "schema_version": SCHEMA_VERSION,
        "last_pipeline": last_payload,
        "status": status,
    }


@router.get("/ops/runs")
def ops_runs(
    limit: int = Query(20, ge=1, le=100),
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops.state import _connect

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, as_of_date, status, trigger_source, dataset_version,
                   started_at, finished_at
            FROM ops.pipeline_run
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return {"runs": [dict(r) for r in rows]}


@router.get("/ops/runs/{run_id}")
def ops_run_detail(
    run_id: str,
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops.engine import run_status

    payload = run_status(run_id)
    if payload.get("error"):
        raise HTTPException(404, detail="run not found")
    return payload


@router.get("/ops/metrics")
def ops_metrics(
    run_id: str = Query(...),
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops import metrics

    return {
        "run_id": run_id,
        "metrics": metrics.list_metrics(run_id),
        "runtimes": metrics.list_runtimes(run_id),
    }


@router.get("/ops/events")
def ops_events(
    run_id: str = Query(...),
    limit: int = Query(200, ge=1, le=2000),
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops import events

    return {"run_id": run_id, "events": events.list_events(run_id, limit=limit)}


@router.get("/ops/quality")
def ops_quality(
    as_of: str | None = None,
    days: int = Query(30, ge=1, le=365),
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops import quality
    from cashflow_ops.config import cairo_today

    d = date.fromisoformat(as_of) if as_of else cairo_today()
    return {"as_of": str(d), "days": days, "trend": quality.quality_trend(d, days=days)}


@router.get("/ops/sla")
def ops_sla(_: AuthUser = Depends(require_roles(ROLE_SUPER))) -> dict[str, Any]:
    from cashflow_ops.state import _connect

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT scope_type, scope_key, max_seconds, enabled, notes
            FROM monitoring.sla_definition
            ORDER BY scope_type, scope_key
            """
        ).fetchall()
    return {"sla": [dict(r) for r in rows]}


@router.get("/ops/health")
def ops_health(
    limit: int = Query(50, ge=1, le=500),
    _: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_ops.state import _connect

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (system_key)
                system_key, probe_name, status, response_ms, checked_at, detail
            FROM monitoring.system_health
            ORDER BY system_key, checked_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        configs = conn.execute("SELECT * FROM monitoring.system_config").fetchall()
    return {
        "latest": [dict(r) for r in rows],
        "config": [dict(r) for r in configs],
    }
