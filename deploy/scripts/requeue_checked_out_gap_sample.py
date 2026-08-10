#!/usr/bin/env python3
"""Insert/requeue sample units into case_units.sqlite for WebPT drain.

- Sets batch_id=checked_out_gap_sample_500 (or --batch-id)
- state=queued, clears errors/retries
- Soft-forces re-download: if case dir has NO daily_notes/*.pdf, leave as-is
  (drain will fetch). If unit was downloaded but sample DOS has no matching
  daily_note PDF name hint, still requeue — download_case_unit pulls missing docs.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BATCH_DEFAULT = "checked_out_gap_sample_500"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--case-root",
        type=Path,
        default=Path("/data/exports/side_by_side_case"),
    )
    ap.add_argument(
        "--sample-csv",
        type=Path,
        default=None,
    )
    ap.add_argument("--batch-id", default=BATCH_DEFAULT)
    ap.add_argument(
        "--clear-manifest",
        action="store_true",
        help="Delete artifacts_manifest.csv under sample case dirs so drain re-lists docs",
    )
    args = ap.parse_args()

    sample_csv = args.sample_csv or (
        args.case_root / "reports" / "checked_out_gap_sample_500.csv"
    )
    db_path = args.case_root / "case_units.sqlite"
    cases_root = args.case_root / "cases"
    now = datetime.now(timezone.utc).isoformat()

    rows: list[dict[str, str]] = []
    with sample_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("EMPTY_SAMPLE", flush=True)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    cur = conn.cursor()

    inserted = 0
    updated = 0
    manifests_cleared = 0
    seen_cases: set[tuple[str, str]] = set()

    for r in rows:
        unit_id = (r.get("unit_id") or "").strip()
        fac = (r.get("facility_id") or "").strip()
        case_id = (r.get("case_id") or "").strip()
        pid = (r.get("patient_id") or r.get("emr_id") or "").strip()
        dos = (r.get("dos") or "")[:10]
        if not unit_id or not fac or not case_id or not pid or not dos:
            continue

        existing = cur.execute(
            "SELECT unit_id, state, batch_id FROM case_units WHERE unit_id=?",
            (unit_id,),
        ).fetchone()
        if existing:
            cur.execute(
                """
                UPDATE case_units
                SET state='queued',
                    prev_state=COALESCE(state, ''),
                    batch_id=?,
                    priority=10,
                    retry_count=0,
                    error_type='',
                    in_progress_since='',
                    updated_at=?
                WHERE unit_id=?
                """,
                (args.batch_id, now, unit_id),
            )
            updated += 1
        else:
            cur.execute(
                """
                INSERT INTO case_units (
                    unit_id, state, priority, batch_id, facility_id, facility_name,
                    case_id, patient_id, dos, visit_status, patient_name,
                    opened_case_id, retry_count, error_type, prev_state,
                    updated_at, in_progress_since, extra_json
                ) VALUES (?, 'queued', 10, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, '', '', ?, '', ?)
                """,
                (
                    unit_id,
                    args.batch_id,
                    fac,
                    (r.get("facility_name") or "").strip(),
                    case_id,
                    pid,
                    dos,
                    (r.get("visit_status") or r.get("sched_visit_status") or "").strip(),
                    (r.get("patient") or "").strip(),
                    now,
                    json.dumps({"sample": "checked_out_gap_sample_500"}),
                ),
            )
            inserted += 1

        key = (fac, case_id)
        if args.clear_manifest and key not in seen_cases:
            seen_cases.add(key)
            man = cases_root / fac / case_id / "manifests" / "artifacts_manifest.csv"
            if man.is_file():
                man.unlink()
                manifests_cleared += 1

    conn.commit()

    states = cur.execute(
        """
        SELECT state, COUNT(*) FROM case_units
        WHERE batch_id=? GROUP BY state ORDER BY 2 DESC
        """,
        (args.batch_id,),
    ).fetchall()
    distinct_cases = cur.execute(
        """
        SELECT COUNT(DISTINCT facility_id || ':' || case_id) FROM case_units
        WHERE batch_id=? AND state='queued'
        """,
        (args.batch_id,),
    ).fetchone()[0]

    report = {
        "batch_id": args.batch_id,
        "sample_rows": len(rows),
        "inserted": inserted,
        "updated": updated,
        "manifests_cleared": manifests_cleared,
        "queued_distinct_cases": distinct_cases,
        "states_for_batch": {s: int(c) for s, c in states},
    }
    out = args.case_root / "reports" / "checked_out_gap_sample_500_requeue.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
