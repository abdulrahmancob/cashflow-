#!/usr/bin/env python3
"""Requeue downloaded cases that have no PDF files on disk (server ops)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/exports/side_by_side_case")
    db = root / "case_units.sqlite"
    cases = root / "cases"
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(db), timeout=120)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT DISTINCT facility_id, case_id FROM case_units WHERE state='downloaded'"
    ).fetchall()
    missing: list[tuple[str, str]] = []
    for fac, case in rows:
        d = cases / str(fac) / str(case)
        try:
            next(d.rglob("*.pdf"))
        except (StopIteration, OSError):
            missing.append((str(fac), str(case)))
    print(f"to_requeue={len(missing)}", flush=True)

    cur.execute("DROP TABLE IF EXISTS miss")
    cur.execute("CREATE TABLE miss(fac TEXT NOT NULL, case_id TEXT NOT NULL)")
    cur.executemany("INSERT INTO miss VALUES (?,?)", missing)
    cur.execute("CREATE INDEX idx_miss ON miss(fac, case_id)")
    conn.commit()

    cur.execute(
        """
        UPDATE case_units
        SET state='queued',
            prev_state='downloaded',
            updated_at=?,
            retry_count=0,
            error_type='',
            in_progress_since=''
        WHERE state='downloaded'
          AND rowid IN (
            SELECT cu.rowid
            FROM case_units cu
            JOIN miss m ON m.fac = cu.facility_id AND m.case_id = cu.case_id
            WHERE cu.state='downloaded'
          )
        """,
        (now,),
    )
    print(f"units_requeued={cur.rowcount}", flush=True)
    conn.commit()
    cur.execute("DROP TABLE IF EXISTS miss")
    conn.commit()
    print(
        "distinct_queued=",
        cur.execute(
            "SELECT COUNT(DISTINCT case_id) FROM case_units WHERE state='queued'"
        ).fetchone()[0],
        flush=True,
    )
    print(
        "states=",
        cur.execute(
            "SELECT state, COUNT(*) FROM case_units GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall(),
        flush=True,
    )
    conn.close()
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
