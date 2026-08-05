"""Postgres-backed pipeline state store."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import DATABASE_URL, STAGE_STALE_SECONDS
from cashflow_ops.contracts import ArtifactSpec, StageStatus


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def next_dataset_version(as_of_date: date) -> str:
    """Allocate `{as_of}.{seq}` for the day."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)::int AS n
            FROM ops.pipeline_run
            WHERE as_of_date = %s
            """,
            (as_of_date,),
        ).fetchone()
        seq = int(row["n"] or 0) + 1
    return f"{as_of_date.isoformat()}.{seq}"


def create_pipeline_run(
    *,
    as_of_date: date,
    trigger_source: str,
    lookback_days: int,
    stage_keys: list[str],
    stage_meta: dict[str, dict[str, Any]],
    notes: str | None = None,
    dataset_version: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> str:
    ds = dataset_version or next_dataset_version(as_of_date)
    meta = {"stage_keys": stage_keys, "dataset_version": ds}
    if meta_extra:
        meta.update(meta_extra)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.pipeline_run (
                as_of_date, status, trigger_source, lookback_days,
                started_at, notes, meta, dataset_version
            )
            VALUES (%s, 'running', %s, %s, now(), %s, %s::jsonb, %s)
            RETURNING run_id
            """,
            (
                as_of_date,
                trigger_source,
                lookback_days,
                notes,
                json.dumps(meta),
                ds,
            ),
        ).fetchone()
        run_id = str(row["run_id"])
        for key in stage_keys:
            meta = stage_meta.get(key, {})
            conn.execute(
                """
                INSERT INTO ops.stage_run (
                    run_id, stage_key, status, max_attempts, on_failure, inputs
                )
                VALUES (%s::uuid, %s, 'pending', %s, %s, %s::jsonb)
                """,
                (
                    run_id,
                    key,
                    int(meta.get("max_attempts", 1)),
                    str(meta.get("on_failure", "stop")),
                    json.dumps(meta.get("inputs", {})),
                ),
            )
        conn.commit()
        return run_id


def get_pipeline_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ops.pipeline_run WHERE run_id = %s::uuid",
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def latest_pipeline_run(as_of: date | None = None) -> dict[str, Any] | None:
    with _connect() as conn:
        if as_of:
            row = conn.execute(
                """
                SELECT * FROM ops.pipeline_run
                WHERE as_of_date = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (as_of,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM ops.pipeline_run
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None


def list_stage_runs(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops.stage_run
            WHERE run_id = %s::uuid
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def stage_statuses(run_id: str) -> dict[str, str]:
    return {r["stage_key"]: r["status"] for r in list_stage_runs(run_id)}


def reclaim_stale_running(run_id: str, stale_seconds: int = STAGE_STALE_SECONDS) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    reclaimed: list[str] = []
    with _connect() as conn:
        rows = conn.execute(
            """
            UPDATE ops.stage_run
            SET status = 'failed',
                error_message = COALESCE(error_message, '') || ' [reclaimed stale running]',
                finished_at = now()
            WHERE run_id = %s::uuid
              AND status = 'running'
              AND started_at IS NOT NULL
              AND started_at < %s
            RETURNING stage_key
            """,
            (run_id, cutoff),
        ).fetchall()
        conn.commit()
        reclaimed = [r["stage_key"] for r in rows]
    return reclaimed


def mark_stage_running(run_id: str, stage_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.stage_run
            SET status = 'running',
                attempt = attempt + 1,
                started_at = COALESCE(started_at, now()),
                error_message = NULL
            WHERE run_id = %s::uuid AND stage_key = %s
            """,
            (run_id, stage_key),
        )
        conn.commit()


def mark_stage_finished(
    run_id: str,
    stage_key: str,
    status: StageStatus | str,
    *,
    outputs: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.stage_run
            SET status = %s,
                outputs = %s::jsonb,
                error_message = %s,
                finished_at = now()
            WHERE run_id = %s::uuid AND stage_key = %s
            """,
            (
                str(status.value if isinstance(status, StageStatus) else status),
                json.dumps(outputs or {}),
                error_message,
                run_id,
                stage_key,
            ),
        )
        conn.commit()


def reset_stage_pending(run_id: str, stage_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.stage_run
            SET status = 'pending',
                error_message = NULL,
                finished_at = NULL,
                outputs = '{}'::jsonb
            WHERE run_id = %s::uuid AND stage_key = %s
            """,
            (run_id, stage_key),
        )
        conn.commit()


def mark_stages_blocked(run_id: str, stage_keys: list[str], reason: str) -> None:
    if not stage_keys:
        return
    with _connect() as conn:
        for key in stage_keys:
            conn.execute(
                """
                UPDATE ops.stage_run
                SET status = 'blocked',
                    error_message = %s,
                    finished_at = now()
                WHERE run_id = %s::uuid
                  AND stage_key = %s
                  AND status IN ('pending', 'blocked')
                """,
                (reason, run_id, key),
            )
        conn.commit()


def finish_pipeline_run(run_id: str, status: str, notes: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE ops.pipeline_run
            SET status = %s,
                finished_at = now(),
                notes = COALESCE(%s, notes)
            WHERE run_id = %s::uuid
            """,
            (status, notes, run_id),
        )
        conn.commit()


def record_artifacts(run_id: str, stage_key: str, artifacts: list[ArtifactSpec]) -> None:
    if not artifacts:
        return
    with _connect() as conn:
        for art in artifacts:
            conn.execute(
                """
                INSERT INTO ops.stage_artifact (
                    run_id, stage_key, artifact_key, uri, row_count,
                    checksum, etl_run_id, payload
                )
                VALUES (
                    %s::uuid, %s, %s, %s, %s, %s,
                    %s::uuid, %s::jsonb
                )
                ON CONFLICT (run_id, artifact_key) DO UPDATE SET
                    uri = EXCLUDED.uri,
                    row_count = EXCLUDED.row_count,
                    checksum = EXCLUDED.checksum,
                    etl_run_id = EXCLUDED.etl_run_id,
                    payload = EXCLUDED.payload,
                    stage_key = EXCLUDED.stage_key
                """,
                (
                    run_id,
                    stage_key,
                    art.key,
                    art.uri,
                    art.row_count,
                    art.checksum,
                    art.etl_run_id,
                    json.dumps(art.payload or {}),
                ),
            )
        conn.commit()


def get_artifacts(run_id: str) -> dict[str, dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops.stage_artifact
            WHERE run_id = %s::uuid
            """,
            (run_id,),
        ).fetchall()
        return {r["artifact_key"]: dict(r) for r in rows}


def enqueue_retry_items(run_id: str, stage_key: str, items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    n = 0
    with _connect() as conn:
        for item in items:
            delay_hours = float(item.get("delay_hours", 1))
            conn.execute(
                """
                INSERT INTO ops.retry_item (
                    run_id, stage_key, item_type, item_key, status,
                    max_attempts, next_attempt_at, last_error, payload
                )
                VALUES (
                    %s::uuid, %s, %s, %s, 'pending',
                    %s, now() + (%s || ' hours')::interval, %s, %s::jsonb
                )
                """,
                (
                    run_id,
                    stage_key,
                    item["item_type"],
                    item["item_key"],
                    int(item.get("max_attempts", 3)),
                    str(delay_hours),
                    item.get("last_error"),
                    json.dumps(item.get("payload", {})),
                ),
            )
            n += 1
        conn.commit()
    return n


def due_retry_items(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops.retry_item
            WHERE status IN ('pending', 'failed')
              AND attempt < max_attempts
              AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_retry_item(
    retry_id: str | UUID,
    *,
    status: str,
    last_error: str | None = None,
    bump_attempt: bool = False,
    delay_hours: float | None = None,
) -> None:
    with _connect() as conn:
        if bump_attempt:
            conn.execute(
                """
                UPDATE ops.retry_item
                SET attempt = attempt + 1,
                    status = %s,
                    last_error = %s,
                    next_attempt_at = CASE
                        WHEN %s IS NULL THEN next_attempt_at
                        ELSE now() + (%s || ' hours')::interval
                    END,
                    updated_at = now()
                WHERE retry_id = %s::uuid
                """,
                (status, last_error, delay_hours, str(delay_hours or 0), str(retry_id)),
            )
        else:
            conn.execute(
                """
                UPDATE ops.retry_item
                SET status = %s,
                    last_error = %s,
                    updated_at = now()
                WHERE retry_id = %s::uuid
                """,
                (status, last_error, str(retry_id)),
            )
        conn.commit()


def record_alert(
    run_id: str | None,
    *,
    stage_key: str | None,
    severity: str,
    alert_key: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.alert_event (
                run_id, stage_key, severity, alert_key, message, payload
            )
            VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb)
            RETURNING alert_id
            """,
            (
                run_id,
                stage_key,
                severity,
                alert_key,
                message,
                json.dumps(payload or {}),
            ),
        ).fetchone()
        conn.commit()
        return str(row["alert_id"])


def list_alerts(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops.alert_event
            WHERE run_id = %s::uuid
            ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_daily_snapshot(
    *,
    as_of_date: date,
    run_id: str,
    summary: dict[str, Any],
    volumes: dict[str, Any],
    stage_statuses_map: dict[str, str],
    forecast_run_id: str | None = None,
    reconciliation_run_id: str | None = None,
    dataset_version: str | None = None,
) -> str:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.daily_snapshot (
                as_of_date, run_id, forecast_run_id, reconciliation_run_id,
                summary, volumes, stage_statuses, dataset_version
            )
            VALUES (
                %s, %s::uuid, %s::uuid, %s::uuid,
                %s::jsonb, %s::jsonb, %s::jsonb, %s
            )
            ON CONFLICT (as_of_date) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                forecast_run_id = EXCLUDED.forecast_run_id,
                reconciliation_run_id = EXCLUDED.reconciliation_run_id,
                summary = EXCLUDED.summary,
                volumes = EXCLUDED.volumes,
                stage_statuses = EXCLUDED.stage_statuses,
                dataset_version = EXCLUDED.dataset_version
            RETURNING snapshot_id
            """,
            (
                as_of_date,
                run_id,
                forecast_run_id,
                reconciliation_run_id,
                json.dumps(summary),
                json.dumps(volumes),
                json.dumps(stage_statuses_map),
                dataset_version,
            ),
        ).fetchone()
        conn.commit()
        return str(row["snapshot_id"])


def get_daily_snapshot(as_of: date) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM ops.daily_snapshot WHERE as_of_date = %s",
            (as_of,),
        ).fetchone()
        return dict(row) if row else None


def get_prior_snapshot(before: date) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM ops.daily_snapshot
            WHERE as_of_date < %s
            ORDER BY as_of_date DESC
            LIMIT 1
            """,
            (before,),
        ).fetchone()
        return dict(row) if row else None


def upsert_forecast_accuracy(
    *,
    as_of_date: date,
    run_id: str,
    forecast_run_id: str | None,
    forecast_total: float | None,
    actual_total: float | None,
    mape: float | None,
    bias: float | None,
    rmse: float | None,
    accuracy: float | None,
    per_insurance: list[dict[str, Any]],
    details: dict[str, Any],
) -> str:
    error_total = None
    if forecast_total is not None and actual_total is not None:
        error_total = forecast_total - actual_total
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO ops.forecast_accuracy_day (
                as_of_date, run_id, forecast_run_id,
                forecast_total, actual_total, error_total,
                mape, bias, rmse, accuracy, per_insurance, details
            )
            VALUES (
                %s, %s::uuid, %s::uuid,
                %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (as_of_date) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                forecast_run_id = EXCLUDED.forecast_run_id,
                forecast_total = EXCLUDED.forecast_total,
                actual_total = EXCLUDED.actual_total,
                error_total = EXCLUDED.error_total,
                mape = EXCLUDED.mape,
                bias = EXCLUDED.bias,
                rmse = EXCLUDED.rmse,
                accuracy = EXCLUDED.accuracy,
                per_insurance = EXCLUDED.per_insurance,
                details = EXCLUDED.details
            RETURNING accuracy_id
            """,
            (
                as_of_date,
                run_id,
                forecast_run_id,
                forecast_total,
                actual_total,
                error_total,
                mape,
                bias,
                rmse,
                accuracy,
                json.dumps(per_insurance),
                json.dumps(details),
            ),
        ).fetchone()
        conn.commit()
        return str(row["accuracy_id"])
