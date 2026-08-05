"""Pipeline event log writers / readers."""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import DATABASE_URL

log = logging.getLogger(__name__)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def emit_event(
    run_id: str | None,
    *,
    event_key: str,
    message: str | None = None,
    stage_key: str | None = None,
    severity: str = "info",
    entity_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO monitoring.pipeline_event (
                    run_id, stage_key, event_key, severity, message, entity_key, payload
                )
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    run_id,
                    stage_key,
                    event_key,
                    severity,
                    message,
                    entity_key,
                    json.dumps(payload or {}),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("emit_event failed (%s): %s", event_key, exc)


def list_events(run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitoring.pipeline_event
            WHERE run_id = %s::uuid
            ORDER BY created_at
            LIMIT %s
            """,
            (run_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
