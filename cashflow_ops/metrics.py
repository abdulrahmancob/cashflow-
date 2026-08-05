"""Emit typed pipeline metrics and job runtimes into monitoring schema."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import DATABASE_URL

log = logging.getLogger(__name__)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def emit_metric(
    run_id: str,
    *,
    metric_key: str,
    metric_type: str,
    value_num: float | int | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    stage_key: str | None = None,
    entity_key: str | None = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring.pipeline_metric (
                    run_id, stage_key, metric_key, metric_type, entity_key,
                    value_num, value_text, unit
                )
                VALUES (
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    run_id,
                    stage_key,
                    metric_key,
                    metric_type,
                    entity_key,
                    value_num,
                    value_text,
                    unit,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("emit_metric failed (%s): %s", metric_key, exc)


def start_job_runtime(run_id: str, stage_key: str) -> datetime:
    started = datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring.job_runtime (
                    run_id, stage_key, started_at, queue_wait_seconds
                )
                VALUES (%s::uuid, %s, %s, 0)
                ON CONFLICT (run_id, stage_key) DO UPDATE SET
                    started_at = EXCLUDED.started_at,
                    finished_at = NULL,
                    duration_sec = NULL,
                    sla_breached = false
                """,
                (run_id, stage_key, started),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("start_job_runtime failed: %s", exc)
    return started


def finish_job_runtime(
    run_id: str,
    stage_key: str,
    *,
    started_at: datetime,
    sla_sec: int | None,
    sla_breached: bool,
    queue_wait_seconds: float = 0.0,
) -> float:
    finished = datetime.now(timezone.utc)
    duration = max((finished - started_at).total_seconds(), 0.0)
    try:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE monitoring.job_runtime
                SET finished_at = %s,
                    duration_sec = %s,
                    queue_wait_seconds = %s,
                    sla_sec = %s,
                    sla_breached = %s
                WHERE run_id = %s::uuid AND stage_key = %s
                """,
                (
                    finished,
                    duration,
                    queue_wait_seconds,
                    sla_sec,
                    sla_breached,
                    run_id,
                    stage_key,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("finish_job_runtime failed: %s", exc)
    emit_metric(
        run_id,
        metric_key=f"{stage_key}_seconds",
        metric_type="duration",
        value_num=duration,
        unit="seconds",
        stage_key=stage_key,
        entity_key=f"stage={stage_key}",
    )
    return duration


def get_sla_seconds(stage_key: str, facility_id: str | None = None) -> int | None:
    try:
        with _connect() as conn:
            if facility_id:
                row = conn.execute(
                    """
                    SELECT max_seconds FROM monitoring.sla_definition
                    WHERE enabled AND scope_type = 'facility'
                      AND scope_key = %s
                    """,
                    (f"facility:{facility_id}",),
                ).fetchone()
                if row:
                    return int(row["max_seconds"])
            row = conn.execute(
                """
                SELECT max_seconds FROM monitoring.sla_definition
                WHERE enabled AND scope_type = 'stage' AND scope_key = %s
                """,
                (stage_key,),
            ).fetchone()
            return int(row["max_seconds"]) if row else None
    except Exception as exc:  # noqa: BLE001
        log.warning("get_sla_seconds failed: %s", exc)
        return None


def list_metrics(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitoring.pipeline_metric
            WHERE run_id = %s::uuid
            ORDER BY recorded_at
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_runtimes(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitoring.job_runtime
            WHERE run_id = %s::uuid
            ORDER BY started_at
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def emit_stage_output_metrics(run_id: str, stage_key: str, outputs: dict[str, Any]) -> None:
    """Best-effort extraction of common numeric metrics from stage outputs."""
    mapping = {
        "schedule_rows": ("count", "count"),
        "notes_missing": ("count", "quality"),
        "cpt_missing": ("count", "quality"),
        "ocr_rows": ("count", "count"),
        "revflow_files": ("count", "count"),
        "forecast_total": ("money", "money"),
        "prediction_rows": ("count", "count"),
    }
    # Nested metrics from validate
    metrics_blob = outputs.get("metrics") if isinstance(outputs.get("metrics"), dict) else {}
    flat = {**outputs, **metrics_blob}
    for key, (unit, mtype) in mapping.items():
        if key in flat and flat[key] is not None:
            try:
                emit_metric(
                    run_id,
                    metric_key=key,
                    metric_type=mtype,
                    value_num=float(flat[key]),
                    unit=unit,
                    stage_key=stage_key,
                    entity_key=f"stage={stage_key}",
                )
            except (TypeError, ValueError):
                pass
