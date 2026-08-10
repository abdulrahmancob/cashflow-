#!/usr/bin/env python3
"""C1: load-webpt then report orphan CPT / coverage DQ gates.

Standing ops after any case extract refresh: ops_extract_load_reconcile.sh
(extract → load-webpt → reconcile). Do not skip load when DB 'looks full'.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

REPORT = Path("/data/exports/side_by_side_case/reports/phase_c1_load_webpt_dq.json")
CPT_CSV = Path("/data/exports/side_by_side_case/extracted/cpt_codes.csv")


def main() -> int:
    from cashflow_db.db import connect
    from cashflow_db.loaders import load_webpt

    skip_load = os.environ.get("C1_SKIP_LOAD", "").strip() in {"1", "true", "yes"}
    counts: dict = {}
    if skip_load:
        print("[c1] skipping load_webpt (C1_SKIP_LOAD=1)", flush=True)
    else:
        print("[c1] load_webpt starting...", flush=True)
        counts = load_webpt()
        print("[c1] load_webpt done:", counts, flush=True)

    csv_with_case = 0
    csv_daily_ids: set[str] = set()
    if CPT_CSV.is_file():
        with CPT_CSV.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if not (row.get("case_id") or "").strip():
                    continue
                csv_with_case += 1
                did = (row.get("daily_note_id") or "").strip()
                if did:
                    csv_daily_ids.add(did)

    with connect() as conn:
        service_lines = int(
            conn.execute("SELECT COUNT(*) AS n FROM core.visit_service_line").fetchone()["n"]
        )
        clinical_notes = int(
            conn.execute("SELECT COUNT(*) AS n FROM core.clinical_note").fetchone()["n"]
        )
        note_ids = {
            r["external_daily_note_id"]
            for r in conn.execute(
                """
                SELECT external_daily_note_id FROM core.clinical_note
                WHERE external_daily_note_id IS NOT NULL
                """
            )
        }
        mapped = len(csv_daily_ids & note_ids)
        orphan_ids = len(csv_daily_ids - note_ids)
        orphan_rate = round(orphan_ids / len(csv_daily_ids), 4) if csv_daily_ids else None

        fac = conn.execute(
            """
            SELECT COALESCE(v.facility_id::text, '(null)') AS facility_id, COUNT(*) AS n
            FROM core.visit_service_line vsl
            JOIN core.visit v ON v.visit_id = vsl.visit_id
            GROUP BY 1 ORDER BY n DESC LIMIT 15
            """
        ).fetchall()
        dos = conn.execute(
            """
            SELECT date_trunc('month', v.service_date)::date AS month, COUNT(*) AS n
            FROM core.visit_service_line vsl
            JOIN core.visit v ON v.visit_id = vsl.visit_id
            WHERE v.service_date IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()

    gate = {
        "load_counts": counts,
        "service_lines": service_lines,
        "clinical_notes": clinical_notes,
        "csv_cpt_rows_with_case": csv_with_case,
        "csv_unique_daily_note_ids": len(csv_daily_ids),
        "note_map_hits": mapped,
        "orphan_daily_note_ids": orphan_ids,
        "orphan_cpt_rate": orphan_rate,
        "facility_top": [dict(r) for r in fac],
        "service_month_dist": [
            {"month": str(r["month"]), "n": int(r["n"])} for r in dos
        ],
        "pass_service_lines_gt_113": service_lines > 113,
        "pass_orphan_rate_lt_0_35": orphan_rate is not None and orphan_rate < 0.35,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(gate, indent=2, default=str), encoding="utf-8")
    print(json.dumps(gate, indent=2, default=str), flush=True)
    ok = gate["pass_service_lines_gt_113"] and (
        orphan_rate is None or gate["pass_orphan_rate_lt_0_35"]
    )
    print(f"[c1] DQ_PASS={ok}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
