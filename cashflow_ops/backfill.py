"""Resumable multi-day backfill control plane."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import DATABASE_URL, DEFAULT_LOOKBACK_DAYS, DRY_RUN, SKIP_SCRAPERS
from cashflow_ops.engine import run_status, start_run

log = logging.getLogger(__name__)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _daterange(start: date, end: date) -> list[date]:
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def create_backfill(
    *,
    from_date: date,
    to_date: date,
    meta: dict[str, Any] | None = None,
) -> str:
    if to_date < from_date:
        raise ValueError("to_date must be >= from_date")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO monitoring.backfill_run (from_date, to_date, status, meta)
            VALUES (%s, %s, 'pending', %s::jsonb)
            RETURNING backfill_id
            """,
            (from_date, to_date, json.dumps(meta or {})),
        ).fetchone()
        backfill_id = str(row["backfill_id"])
        for d in _daterange(from_date, to_date):
            conn.execute(
                """
                INSERT INTO monitoring.backfill_day (backfill_id, as_of_date, status)
                VALUES (%s::uuid, %s, 'pending')
                """,
                (backfill_id, d),
            )
        conn.commit()
    return backfill_id


def get_backfill(backfill_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM monitoring.backfill_run WHERE backfill_id = %s::uuid",
            (backfill_id,),
        ).fetchone()
        return dict(row) if row else None


def list_backfill_days(backfill_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitoring.backfill_day
            WHERE backfill_id = %s::uuid
            ORDER BY as_of_date
            """,
            (backfill_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _set_backfill_status(backfill_id: str, status: str) -> None:
    with _connect() as conn:
        finished = status in {"success", "failed", "partial", "cancelled"}
        conn.execute(
            """
            UPDATE monitoring.backfill_run
            SET status = %s,
                finished_at = CASE WHEN %s THEN now() ELSE finished_at END
            WHERE backfill_id = %s::uuid
            """,
            (status, finished, backfill_id),
        )
        conn.commit()


def _update_day(
    backfill_id: str,
    as_of: date,
    *,
    status: str,
    pipeline_run_id: str | None = None,
    error_message: str | None = None,
    bump_attempt: bool = False,
) -> None:
    with _connect() as conn:
        if bump_attempt:
            conn.execute(
                """
                UPDATE monitoring.backfill_day
                SET status = %s,
                    pipeline_run_id = COALESCE(%s::uuid, pipeline_run_id),
                    error_message = %s,
                    attempt = attempt + 1
                WHERE backfill_id = %s::uuid AND as_of_date = %s
                """,
                (status, pipeline_run_id, error_message, backfill_id, as_of),
            )
        else:
            conn.execute(
                """
                UPDATE monitoring.backfill_day
                SET status = %s,
                    pipeline_run_id = COALESCE(%s::uuid, pipeline_run_id),
                    error_message = %s
                WHERE backfill_id = %s::uuid AND as_of_date = %s
                """,
                (status, pipeline_run_id, error_message, backfill_id, as_of),
            )
        conn.commit()


def run_backfill(
    backfill_id: str,
    *,
    continue_on_fail: bool = False,
    dry_run: bool | None = None,
    skip_scrapers: bool | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    bf = get_backfill(backfill_id)
    if not bf:
        raise ValueError(f"Unknown backfill_id: {backfill_id}")
    _set_backfill_status(backfill_id, "running")
    days = list_backfill_days(backfill_id)
    results: list[dict[str, Any]] = []

    for day in days:
        if day["status"] == "success":
            continue
        as_of: date = day["as_of_date"]
        _update_day(backfill_id, as_of, status="running", bump_attempt=True)
        try:
            run_id = start_run(
                as_of_date=as_of,
                trigger_source="backfill",
                lookback_days=lookback_days,
                dry_run=dry_run,
                skip_scrapers=skip_scrapers,
                meta_extra={"backfill_id": backfill_id},
            )
            st = run_status(run_id)["run"]["status"]
            if st in {"success", "partial"}:
                _update_day(
                    backfill_id, as_of, status="success", pipeline_run_id=run_id
                )
                results.append({"as_of": str(as_of), "run_id": run_id, "status": st})
            else:
                _update_day(
                    backfill_id,
                    as_of,
                    status="failed",
                    pipeline_run_id=run_id,
                    error_message=f"pipeline status={st}",
                )
                results.append({"as_of": str(as_of), "run_id": run_id, "status": st})
                if not continue_on_fail:
                    _set_backfill_status(backfill_id, "failed")
                    return {
                        "backfill_id": backfill_id,
                        "status": "failed",
                        "stopped_at": str(as_of),
                        "days": results,
                    }
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill day failed %s", as_of)
            _update_day(
                backfill_id,
                as_of,
                status="failed",
                error_message=str(exc),
            )
            results.append({"as_of": str(as_of), "status": "failed", "error": str(exc)})
            if not continue_on_fail:
                _set_backfill_status(backfill_id, "failed")
                return {
                    "backfill_id": backfill_id,
                    "status": "failed",
                    "stopped_at": str(as_of),
                    "days": results,
                }

    # Final status
    days = list_backfill_days(backfill_id)
    statuses = {d["status"] for d in days}
    if statuses <= {"success"}:
        final = "success"
    elif "failed" in statuses and "success" in statuses:
        final = "partial"
    elif "failed" in statuses:
        final = "failed"
    else:
        final = "partial"
    _set_backfill_status(backfill_id, final)
    return {"backfill_id": backfill_id, "status": final, "days": results}


def start_date_range_backfill(
    *,
    from_date: date,
    to_date: date,
    continue_on_fail: bool = False,
    dry_run: bool | None = None,
    skip_scrapers: bool | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    backfill_id = create_backfill(from_date=from_date, to_date=to_date)
    return run_backfill(
        backfill_id,
        continue_on_fail=continue_on_fail,
        dry_run=dry_run,
        skip_scrapers=skip_scrapers,
        lookback_days=lookback_days,
    )
