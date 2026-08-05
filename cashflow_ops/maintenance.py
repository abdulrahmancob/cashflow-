"""DB-managed maintenance windows and system health probes."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import (
    DATABASE_URL,
    REVFLOW_OUTPUT,
    WAYSTAR_OUTPUT,
    WEBPT_DIR,
)

log = logging.getLogger(__name__)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def get_system_mode(system_key: str) -> str:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT mode FROM monitoring.system_config WHERE system_key = %s",
                (system_key,),
            ).fetchone()
            return str(row["mode"]) if row else "auto"
    except Exception as exc:  # noqa: BLE001
        log.warning("get_system_mode failed: %s", exc)
        return "auto"


def record_health(
    system_key: str,
    *,
    status: str,
    probe_name: str = "default",
    response_ms: float | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring.system_health (
                    system_key, probe_name, status, response_ms, detail
                )
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (
                    system_key,
                    probe_name,
                    status,
                    response_ms,
                    json.dumps(detail or {}),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("record_health failed: %s", exc)


def _probe_webpt() -> tuple[str, float, dict[str, Any]]:
    t0 = time.perf_counter()
    state_file = WEBPT_DIR / "storage_state.json"
    ok = state_file.is_file()
    ms = (time.perf_counter() - t0) * 1000
    return ("up" if ok else "degraded", ms, {"path": str(state_file), "exists": ok})


def _probe_path(path: Path, label: str) -> tuple[str, float, dict[str, Any]]:
    t0 = time.perf_counter()
    ok = path.exists()
    ms = (time.perf_counter() - t0) * 1000
    return ("up" if ok else "down", ms, {"path": str(path), "label": label})


def _probe_postgres() -> tuple[str, float, dict[str, Any]]:
    t0 = time.perf_counter()
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        ms = (time.perf_counter() - t0) * 1000
        return ("up", ms, {})
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        return ("down", ms, {"error": str(exc)})


_PROBES = {
    "webpt": ("session_file", _probe_webpt),
    "revflow": ("output_dir", lambda: _probe_path(REVFLOW_OUTPUT, "revflow_output")),
    "waystar": ("output_dir", lambda: _probe_path(WAYSTAR_OUTPUT, "waystar_output")),
    "snowflake": ("module", lambda: _probe_path(Path("snowflake_pull"), "snowflake_pkg")),
    "postgres": ("connect", _probe_postgres),
}


def resolve_skip_systems() -> dict[str, dict[str, Any]]:
    """
    Return map system_key -> {skip: bool, reason, mode, health}.
    maintenance / probe-down (auto) => skip=True.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM monitoring.system_config").fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve_skip_systems: %s", exc)
        return out

    for row in rows:
        key = row["system_key"]
        mode = row["mode"]
        probe_name = row.get("probe_name") or "default"
        if mode == "maintenance":
            record_health(key, status="maintenance", probe_name=probe_name)
            out[key] = {
                "skip": True,
                "reason": "maintenance",
                "mode": mode,
                "status": "maintenance",
            }
            continue
        if mode == "force_up":
            out[key] = {"skip": False, "reason": "force_up", "mode": mode, "status": "up"}
            continue
        # auto — probe
        probe = _PROBES.get(key)
        if not probe:
            out[key] = {"skip": False, "reason": "no_probe", "mode": mode, "status": "unknown"}
            continue
        pname, fn = probe
        status, ms, detail = fn()
        record_health(key, status=status, probe_name=pname, response_ms=ms, detail=detail)
        if status in {"down"}:
            out[key] = {
                "skip": True,
                "reason": "probe_down",
                "mode": mode,
                "status": status,
                "response_ms": ms,
            }
        else:
            out[key] = {
                "skip": False,
                "reason": "probe_ok",
                "mode": mode,
                "status": status,
                "response_ms": ms,
            }
    return out
