"""SQLite-backed unit FSM for resumable coverage recovery work."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VALID_STATES = (
    "queued",
    "in_progress",
    "downloaded",
    "extracted",
    "reconciled",
    "done",
    "failed_terminal",
)

# Allowed transitions (from -> to)
TRANSITIONS: dict[str, set[str]] = {
    "queued": {"in_progress", "failed_terminal", "done"},
    "in_progress": {
        "downloaded",
        "extracted",
        "reconciled",
        "queued",  # TTL reset
        "failed_terminal",
        "done",
    },
    "downloaded": {"in_progress", "extracted", "failed_terminal", "done"},
    "extracted": {"in_progress", "reconciled", "failed_terminal", "done"},
    "reconciled": {"done", "failed_terminal"},
    "done": set(),
    "failed_terminal": {"queued"},  # manual requeue
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Unit:
    unit_id: str
    state: str
    priority: int
    batch_id: str
    facility_id: str
    facility_name: str
    webpt_patient_id: str
    emr_id: str
    dos: str
    visit_status: str
    patient_name: str
    retry_count: int
    error_type: str
    prev_state: str
    updated_at: str
    in_progress_since: str
    extra_json: str


class UnitStateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS units (
                    unit_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    batch_id TEXT NOT NULL DEFAULT '',
                    facility_id TEXT NOT NULL DEFAULT '',
                    facility_name TEXT NOT NULL DEFAULT '',
                    webpt_patient_id TEXT NOT NULL DEFAULT '',
                    emr_id TEXT NOT NULL DEFAULT '',
                    dos TEXT NOT NULL DEFAULT '',
                    visit_status TEXT NOT NULL DEFAULT '',
                    patient_name TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT NOT NULL DEFAULT '',
                    prev_state TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    in_progress_since TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_units_state_priority "
                "ON units(state, priority, unit_id)"
            )
            self._conn.commit()

    def upsert_units(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        now = _utc()
        with self._lock:
            for row in rows:
                unit_id = row["unit_id"]
                cur = self._conn.execute(
                    "SELECT unit_id FROM units WHERE unit_id = ?", (unit_id,)
                ).fetchone()
                if cur:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO units (
                        unit_id, state, priority, batch_id, facility_id, facility_name,
                        webpt_patient_id, emr_id, dos, visit_status, patient_name,
                        retry_count, error_type, prev_state, updated_at, in_progress_since,
                        extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', ?, '', ?)
                    """,
                    (
                        unit_id,
                        row.get("state", "queued"),
                        int(row.get("priority", 100)),
                        row.get("batch_id", ""),
                        row.get("facility_id", ""),
                        row.get("facility_name", ""),
                        row.get("webpt_patient_id", ""),
                        row.get("emr_id", ""),
                        row.get("dos", ""),
                        row.get("visit_status", ""),
                        row.get("patient_name", ""),
                        now,
                        row.get("extra_json", "{}"),
                    ),
                )
                n += 1
            self._conn.commit()
        return n

    def get(self, unit_id: str) -> Unit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM units WHERE unit_id = ?", (unit_id,)
            ).fetchone()
        return self._to_unit(row) if row else None

    def _to_unit(self, row: sqlite3.Row) -> Unit:
        return Unit(**{k: row[k] for k in row.keys()})

    def transition(
        self,
        unit_id: str,
        new_state: str,
        *,
        error_type: str = "",
        force: bool = False,
    ) -> Unit:
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid state {new_state}")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM units WHERE unit_id = ?", (unit_id,)
            ).fetchone()
            if row is None:
                raise KeyError(unit_id)
            old = row["state"]
            if not force and new_state not in TRANSITIONS.get(old, set()):
                raise ValueError(f"illegal transition {old} -> {new_state}")
            now = _utc()
            in_prog = now if new_state == "in_progress" else ""
            retry = row["retry_count"]
            if new_state == "queued" and old == "in_progress":
                retry = int(retry) + 1
            self._conn.execute(
                """
                UPDATE units
                SET state = ?, prev_state = ?, updated_at = ?,
                    in_progress_since = CASE WHEN ? = 'in_progress' THEN ? ELSE in_progress_since END,
                    error_type = CASE WHEN ? != '' THEN ? ELSE error_type END,
                    retry_count = ?
                WHERE unit_id = ?
                """,
                (
                    new_state,
                    old,
                    now,
                    new_state,
                    in_prog,
                    error_type,
                    error_type,
                    retry,
                    unit_id,
                ),
            )
            self._conn.commit()
            out = self._conn.execute(
                "SELECT * FROM units WHERE unit_id = ?", (unit_id,)
            ).fetchone()
        return self._to_unit(out)

    def claim_next(self, *, batch_id: str | None = None) -> Unit | None:
        with self._lock:
            if batch_id:
                row = self._conn.execute(
                    """
                    SELECT * FROM units
                    WHERE state = 'queued' AND batch_id = ?
                    ORDER BY priority ASC, unit_id ASC
                    LIMIT 1
                    """,
                    (batch_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM units
                    WHERE state = 'queued'
                    ORDER BY priority ASC, unit_id ASC
                    LIMIT 1
                    """
                ).fetchone()
            if row is None:
                return None
            unit_id = row["unit_id"]
        return self.transition(unit_id, "in_progress")

    def reclaim_stale_in_progress(self, ttl_seconds: float) -> list[str]:
        """Reset in_progress units older than TTL back to previous durable state."""
        now = datetime.now(timezone.utc)
        reset_ids: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM units WHERE state = 'in_progress'"
            ).fetchall()
        for row in rows:
            since = row["in_progress_since"] or row["updated_at"]
            try:
                started = datetime.fromisoformat(since)
            except ValueError:
                started = now
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = (now - started).total_seconds()
            if age < ttl_seconds:
                continue
            prev = row["prev_state"] or "queued"
            if prev not in {"queued", "downloaded", "extracted", "reconciled"}:
                prev = "queued"
            self.transition(row["unit_id"], prev, force=True)
            # bump retry via queued transition if needed
            if prev == "queued":
                pass
            else:
                # put back to durable state then allow reclaim by setting queued? 
                # Keep durable state so resume continues at extract/reconcile.
                pass
            reset_ids.append(row["unit_id"])
        return reset_ids

    def counts_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM units GROUP BY state"
            ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def list_resumable(self) -> list[Unit]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM units
                WHERE state NOT IN ('done', 'failed_terminal')
                ORDER BY priority ASC, unit_id ASC
                """
            ).fetchall()
        return [self._to_unit(r) for r in rows]

    def units_in_states(self, states: Iterable[str]) -> list[Unit]:
        states = list(states)
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM units WHERE state IN ({placeholders}) "
                "ORDER BY priority ASC, unit_id ASC",
                states,
            ).fetchall()
        return [self._to_unit(r) for r in rows]
