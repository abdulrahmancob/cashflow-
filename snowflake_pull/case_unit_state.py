"""SQLite FSM for Case-centric schedule units (facility:case:patient:dos)."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

VALID_STATES = (
    "queued",
    "retry_1",
    "retry_2",
    "retry_3",
    "in_progress",
    "downloaded",
    "extracted",
    "reconciled",
    "done",
    "failed_terminal",
)

# Claim order: main queue first, then retry tiers
CLAIMABLE_STATES = ("queued", "retry_1", "retry_2", "retry_3")

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"in_progress", "failed_terminal", "done", "retry_1"},
    "retry_1": {"in_progress", "failed_terminal", "retry_2"},
    "retry_2": {"in_progress", "failed_terminal", "retry_3"},
    "retry_3": {"in_progress", "failed_terminal"},
    "in_progress": {
        "downloaded",
        "extracted",
        "reconciled",
        "queued",
        "retry_1",
        "retry_2",
        "retry_3",
        "failed_terminal",
        "done",
    },
    "downloaded": {"in_progress", "extracted", "failed_terminal", "done"},
    "extracted": {"in_progress", "reconciled", "failed_terminal", "done"},
    "reconciled": {"done", "failed_terminal"},
    "done": set(),
    "failed_terminal": {"queued", "retry_1"},
}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_case_unit_id(
    facility_id: str | int, case_id: str | int, patient_id: str | int, dos: str
) -> str:
    return f"{facility_id}:{case_id}:{patient_id}:{dos[:10]}"


@dataclass
class CaseUnit:
    unit_id: str
    state: str
    priority: int
    batch_id: str
    facility_id: str
    facility_name: str
    case_id: str
    patient_id: str
    dos: str
    visit_status: str
    patient_name: str
    opened_case_id: str
    retry_count: int
    error_type: str
    prev_state: str
    updated_at: str
    in_progress_since: str
    extra_json: str


@dataclass
class CaseGroupClaim:
    """One case download covers all visit siblings for (facility_id, case_id)."""

    facility_id: str
    case_id: str
    patient_id: str
    patient_name: str
    facility_name: str
    primary: CaseUnit
    siblings: list[CaseUnit] = field(default_factory=list)

    @property
    def unit_ids(self) -> list[str]:
        return [u.unit_id for u in self.siblings]

    @property
    def dos_list(self) -> list[str]:
        return sorted({u.dos for u in self.siblings if u.dos})


class CaseUnitStateStore:
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
                CREATE TABLE IF NOT EXISTS case_units (
                    unit_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    batch_id TEXT NOT NULL DEFAULT '',
                    facility_id TEXT NOT NULL DEFAULT '',
                    facility_name TEXT NOT NULL DEFAULT '',
                    case_id TEXT NOT NULL DEFAULT '',
                    patient_id TEXT NOT NULL DEFAULT '',
                    dos TEXT NOT NULL DEFAULT '',
                    visit_status TEXT NOT NULL DEFAULT '',
                    patient_name TEXT NOT NULL DEFAULT '',
                    opened_case_id TEXT NOT NULL DEFAULT '',
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
                "CREATE INDEX IF NOT EXISTS idx_case_units_state_priority "
                "ON case_units(state, priority, unit_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_case_units_fac_case_state "
                "ON case_units(batch_id, state, facility_id, case_id)"
            )
            self._conn.commit()

    def upsert_units(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        now = _utc()
        with self._lock:
            for row in rows:
                unit_id = row["unit_id"]
                cur = self._conn.execute(
                    "SELECT unit_id FROM case_units WHERE unit_id = ?", (unit_id,)
                ).fetchone()
                if cur:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO case_units (
                        unit_id, state, priority, batch_id, facility_id, facility_name,
                        case_id, patient_id, dos, visit_status, patient_name,
                        opened_case_id, retry_count, error_type, prev_state,
                        updated_at, in_progress_since, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, '', '', ?, '', ?)
                    """,
                    (
                        unit_id,
                        row.get("state", "queued"),
                        int(row.get("priority", 100)),
                        row.get("batch_id", ""),
                        str(row.get("facility_id", "")),
                        row.get("facility_name", ""),
                        str(row.get("case_id", "")),
                        str(row.get("patient_id", "")),
                        (row.get("dos") or "")[:10],
                        row.get("visit_status", ""),
                        row.get("patient_name", ""),
                        now,
                        row.get("extra_json", "{}"),
                    ),
                )
                n += 1
            self._conn.commit()
        return n

    def claim_next(self, *, batch_id: str | None = None) -> CaseUnit | None:
        with self._lock:
            if batch_id:
                row = self._conn.execute(
                    """
                    SELECT * FROM case_units
                    WHERE state='queued' AND batch_id=?
                    ORDER BY priority ASC, unit_id ASC LIMIT 1
                    """,
                    (batch_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM case_units WHERE state='queued'
                    ORDER BY priority ASC, unit_id ASC LIMIT 1
                    """
                ).fetchone()
            if row is None:
                return None
            unit_id = row["unit_id"]
            now = _utc()
            self._conn.execute(
                """
                UPDATE case_units
                SET state='in_progress', prev_state='queued', updated_at=?,
                    in_progress_since=?
                WHERE unit_id=?
                """,
                (now, now, unit_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM case_units WHERE unit_id=?", (unit_id,)
            ).fetchone()
        return self._to_unit(row) if row else None

    def transition(
        self,
        unit_id: str,
        new_state: str,
        *,
        error_type: str = "",
        opened_case_id: str = "",
        force: bool = False,
    ) -> CaseUnit:
        if new_state not in VALID_STATES:
            raise ValueError(f"invalid state {new_state}")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM case_units WHERE unit_id = ?", (unit_id,)
            ).fetchone()
            if row is None:
                raise KeyError(unit_id)
            old = row["state"]
            if not force and new_state not in TRANSITIONS.get(old, set()):
                raise ValueError(f"illegal transition {old} -> {new_state}")
            now = _utc()
            retry = int(row["retry_count"] or 0)
            if old == "in_progress" and new_state in (
                "queued",
                "retry_1",
                "retry_2",
                "retry_3",
            ):
                retry += 1
            self._conn.execute(
                """
                UPDATE case_units
                SET state=?, prev_state=?, updated_at=?,
                    in_progress_since=CASE WHEN ?= 'in_progress' THEN ? ELSE in_progress_since END,
                    error_type=CASE WHEN ? != '' THEN ? ELSE error_type END,
                    opened_case_id=CASE WHEN ? != '' THEN ? ELSE opened_case_id END,
                    retry_count=?
                WHERE unit_id=?
                """,
                (
                    new_state,
                    old,
                    now,
                    new_state,
                    now,
                    error_type,
                    error_type,
                    opened_case_id,
                    opened_case_id,
                    retry,
                    unit_id,
                ),
            )
            try:
                from snowflake_pull.case_forensics import io_span

                with io_span("sqlite_commit"):
                    self._conn.commit()
            except Exception:
                self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM case_units WHERE unit_id=?", (unit_id,)
            ).fetchone()
        return self._to_unit(row)

    def counts_by_state(self, *, batch_id: str | None = None) -> dict[str, int]:
        with self._lock:
            if batch_id:
                rows = self._conn.execute(
                    "SELECT state, COUNT(1) AS n FROM case_units "
                    "WHERE batch_id=? GROUP BY state",
                    (batch_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT state, COUNT(1) AS n FROM case_units GROUP BY state"
                ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def counts_by_error_type(self, *, batch_id: str | None = None) -> dict[str, int]:
        """Count units by error_type (non-empty only)."""
        with self._lock:
            if batch_id:
                rows = self._conn.execute(
                    """
                    SELECT error_type, COUNT(1) AS n FROM case_units
                    WHERE batch_id=? AND error_type != ''
                    GROUP BY error_type
                    """,
                    (batch_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT error_type, COUNT(1) AS n FROM case_units
                    WHERE error_type != ''
                    GROUP BY error_type
                    """
                ).fetchall()
        return {r["error_type"]: int(r["n"]) for r in rows}

    def units_in_states(self, states: list[str]) -> list[CaseUnit]:
        if not states:
            return []
        placeholders = ",".join("?" * len(states))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM case_units WHERE state IN ({placeholders})",
                tuple(states),
            ).fetchall()
        return [self._to_unit(r) for r in rows]

    def claim_next_for_facility(
        self, facility_id: str, *, batch_id: str | None = None
    ) -> CaseUnit | None:
        """Claim next queued unit sticky to facility_id."""
        with self._lock:
            if batch_id:
                row = self._conn.execute(
                    """
                    SELECT * FROM case_units
                    WHERE state='queued' AND batch_id=? AND facility_id=?
                    ORDER BY priority ASC, case_id ASC, unit_id ASC LIMIT 1
                    """,
                    (batch_id, str(facility_id)),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM case_units
                    WHERE state='queued' AND facility_id=?
                    ORDER BY priority ASC, case_id ASC, unit_id ASC LIMIT 1
                    """,
                    (str(facility_id),),
                ).fetchone()
            if row is None:
                return None
            return self._claim_row_unlocked(row)

    def claim_next_case_group(
        self,
        *,
        batch_id: str | None = None,
        preferred_facility: str | None = None,
        claim_states: tuple[str, ...] | None = None,
    ) -> CaseGroupClaim | None:
        """Claim all siblings for one (facility_id, case_id) from claimable states.

        Main drain uses claim_states=('queued',). After main empty, pass
        ('retry_1',) then ('retry_2',) then ('retry_3',).
        """
        states = claim_states or ("queued",)
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            row = None
            if preferred_facility:
                for state in states:
                    if batch_id:
                        row = self._conn.execute(
                            """
                            SELECT * FROM case_units
                            WHERE state=? AND batch_id=? AND facility_id=?
                            ORDER BY priority ASC, case_id ASC, unit_id ASC LIMIT 1
                            """,
                            (state, batch_id, str(preferred_facility)),
                        ).fetchone()
                    else:
                        row = self._conn.execute(
                            """
                            SELECT * FROM case_units
                            WHERE state=? AND facility_id=?
                            ORDER BY priority ASC, case_id ASC, unit_id ASC LIMIT 1
                            """,
                            (state, str(preferred_facility)),
                        ).fetchone()
                    if row is not None:
                        break
            if row is None:
                for state in states:
                    if batch_id:
                        row = self._conn.execute(
                            """
                            SELECT * FROM case_units
                            WHERE state=? AND batch_id=?
                            ORDER BY priority ASC, facility_id ASC, case_id ASC, unit_id ASC
                            LIMIT 1
                            """,
                            (state, batch_id),
                        ).fetchone()
                    else:
                        row = self._conn.execute(
                            """
                            SELECT * FROM case_units WHERE state=?
                            ORDER BY priority ASC, facility_id ASC, case_id ASC, unit_id ASC
                            LIMIT 1
                            """,
                            (state,),
                        ).fetchone()
                    if row is not None:
                        break
            if row is None:
                return None

            facility_id = str(row["facility_id"])
            case_id = str(row["case_id"])
            if batch_id:
                siblings = self._conn.execute(
                    f"""
                    SELECT * FROM case_units
                    WHERE state IN ({placeholders}) AND batch_id=? AND facility_id=? AND case_id=?
                    ORDER BY unit_id ASC
                    """,
                    (*states, batch_id, facility_id, case_id),
                ).fetchall()
            else:
                siblings = self._conn.execute(
                    f"""
                    SELECT * FROM case_units
                    WHERE state IN ({placeholders}) AND facility_id=? AND case_id=?
                    ORDER BY unit_id ASC
                    """,
                    (*states, facility_id, case_id),
                ).fetchall()

            now = _utc()
            units: list[CaseUnit] = []
            for srow in siblings:
                self._conn.execute(
                    """
                    UPDATE case_units
                    SET state='in_progress', prev_state=?, updated_at=?,
                        in_progress_since=?
                    WHERE unit_id=?
                    """,
                    (str(srow["state"]), now, now, srow["unit_id"]),
                )
                refreshed = self._conn.execute(
                    "SELECT * FROM case_units WHERE unit_id=?", (srow["unit_id"],)
                ).fetchone()
                units.append(self._to_unit(refreshed))
            self._conn.commit()

        primary = units[0]
        return CaseGroupClaim(
            facility_id=facility_id,
            case_id=case_id,
            patient_id=primary.patient_id,
            patient_name=primary.patient_name,
            facility_name=primary.facility_name,
            primary=primary,
            siblings=units,
        )

    def transition_many(
        self,
        unit_ids: list[str],
        new_state: str,
        *,
        error_type: str = "",
        opened_case_id: str = "",
        force: bool = False,
    ) -> list[CaseUnit]:
        out: list[CaseUnit] = []
        for uid in unit_ids:
            out.append(
                self.transition(
                    uid,
                    new_state,
                    error_type=error_type,
                    opened_case_id=opened_case_id,
                    force=force,
                )
            )
        return out

    def reclaim_stale_in_progress(
        self, older_than_sec: float = 1800.0, *, batch_id: str | None = None
    ) -> int:
        """Requeue in_progress units older than threshold (crash resume)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=older_than_sec)
        ).isoformat()
        with self._lock:
            if batch_id:
                rows = self._conn.execute(
                    """
                    SELECT unit_id FROM case_units
                    WHERE state='in_progress' AND batch_id=?
                      AND in_progress_since != '' AND in_progress_since < ?
                    """,
                    (batch_id, cutoff),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT unit_id FROM case_units
                    WHERE state='in_progress'
                      AND in_progress_since != '' AND in_progress_since < ?
                    """,
                    (cutoff,),
                ).fetchall()
            now = _utc()
            n = 0
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE case_units
                    SET state='queued', prev_state='in_progress', updated_at=?,
                        retry_count=retry_count+1, in_progress_since=''
                    WHERE unit_id=?
                    """,
                    (now, row["unit_id"]),
                )
                n += 1
            self._conn.commit()
        return n

    def remaining_cases_by_facility(
        self,
        *,
        batch_id: str | None = None,
        states: tuple[str, ...] = ("queued",),
    ) -> dict[str, int]:
        """Count distinct claimable (facility, case) pairs per facility."""
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            if batch_id:
                rows = self._conn.execute(
                    f"""
                    SELECT facility_id, COUNT(DISTINCT case_id) AS n
                    FROM case_units
                    WHERE state IN ({placeholders}) AND batch_id=?
                    GROUP BY facility_id
                    """,
                    (*states, batch_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"""
                    SELECT facility_id, COUNT(DISTINCT case_id) AS n
                    FROM case_units
                    WHERE state IN ({placeholders})
                    GROUP BY facility_id
                    """,
                    (*states,),
                ).fetchall()
        return {str(r["facility_id"]): int(r["n"]) for r in rows}

    def write_checkpoint(
        self,
        path: Path,
        *,
        batch_id: str,
        watermark: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc(),
            "batch_id": batch_id,
            "counts_by_state": self.counts_by_state(batch_id=batch_id),
            "error_type_counts": self.counts_by_error_type(batch_id=batch_id),
            "remaining_cases_by_facility": self.remaining_cases_by_facility(
                batch_id=batch_id
            ),
            "watermark": watermark or {},
            "extra": extra or {},
        }
        # WAL checkpoint for durability
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            self._conn.commit()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _claim_row_unlocked(self, row: sqlite3.Row) -> CaseUnit:
        unit_id = row["unit_id"]
        now = _utc()
        self._conn.execute(
            """
            UPDATE case_units
            SET state='in_progress', prev_state='queued', updated_at=?,
                in_progress_since=?
            WHERE unit_id=?
            """,
            (now, now, unit_id),
        )
        self._conn.commit()
        refreshed = self._conn.execute(
            "SELECT * FROM case_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        return self._to_unit(refreshed)

    def _to_unit(self, row: sqlite3.Row) -> CaseUnit:
        return CaseUnit(**{k: row[k] for k in row.keys()})
