#!/usr/bin/env python3
"""Coverage inventory: extract files vs warehouse vs schedule-orphan gap.

Decision output: backfill_viable | expected_gap | mixed — never blind backfill.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path

CASE_DIR = Path(os.environ.get("CASE_PIPELINE_DIR", "/data/exports/side_by_side_case"))
OUT = Path(
    os.environ.get(
        "COVERAGE_INVENTORY_OUT",
        "/data/exports/side_by_side_case/reports/coverage_inventory.json",
    )
)


def _count_csv(path: Path, pred=None) -> int:
    if not path.is_file():
        return -1
    n = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if pred is None or pred(row):
                n += 1
    return n


def main() -> int:
    from cashflow_db.db import connect

    extracted = CASE_DIR / "extracted"
    notes_csv = extracted / "daily_notes.csv"
    cpt_csv = extracted / "cpt_codes.csv"

    notes_rows = _count_csv(notes_csv)
    notes_with_case = _count_csv(
        notes_csv, lambda r: bool((r.get("case_id") or "").strip())
    )
    cpt_rows = _count_csv(cpt_csv)
    cpt_with_case = _count_csv(cpt_csv, lambda r: bool((r.get("case_id") or "").strip()))
    cpt_with_note = _count_csv(
        cpt_csv,
        lambda r: bool((r.get("case_id") or "").strip())
        and bool((r.get("daily_note_id") or "").strip()),
    )

    with connect() as conn:
        wh = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM core.visit v
                 WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30') AS visits_pack,
              (SELECT COUNT(*) FROM core.visit v
                 WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30'
                   AND EXISTS (SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id)
              ) AS visits_with_note,
              (SELECT COUNT(*) FROM core.visit v
                 WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30'
                   AND EXISTS (SELECT 1 FROM core.visit_service_line sl WHERE sl.visit_id = v.visit_id)
              ) AS visits_with_cpt,
              (SELECT COUNT(*) FROM core.clinical_note cn
                 JOIN core.visit v ON v.visit_id = cn.visit_id
                WHERE COALESCE(cn.note_date, v.service_date)
                      BETWEEN DATE '2026-01-01' AND DATE '2026-08-30') AS notes_loaded,
              (SELECT COUNT(*) FROM core.visit_service_line sl
                 JOIN core.visit v ON v.visit_id = sl.visit_id
                WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30') AS cpt_lines_loaded,
              (SELECT COUNT(*) FROM core.schedule_appointment sa
                WHERE sa.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30') AS schedule_rows
            """
        ).fetchone()

        # Orphans among SF coverage keys if file present
        orphan_file = Path("/home/abdu/sf_eval/coverage_gap_visits.csv")
        orphan_stats = {}
        if orphan_file.is_file():
            # Sample join via SQL temp not available; summarize warehouse ratio instead
            pass

        monthly = conn.execute(
            """
            SELECT to_char(date_trunc('month', v.service_date), 'YYYY-MM') AS ym,
                   COUNT(*) AS visits,
                   COUNT(*) FILTER (
                     WHERE EXISTS (SELECT 1 FROM core.visit_service_line sl WHERE sl.visit_id = v.visit_id)
                   ) AS with_cpt,
                   COUNT(*) FILTER (
                     WHERE EXISTS (SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id)
                   ) AS with_note
            FROM core.visit v
            WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30'
            GROUP BY 1
            ORDER BY 1
            """
        ).fetchall()

    visits_pack = int(wh["visits_pack"] or 0)
    with_cpt = int(wh["visits_with_cpt"] or 0)
    with_note = int(wh["visits_with_note"] or 0)
    no_cpt = visits_pack - with_cpt
    pct_no_cpt = round(100.0 * no_cpt / visits_pack, 1) if visits_pack else 0.0

    # Decision logic
    extract_thin = cpt_with_case >= 0 and cpt_with_case < with_cpt * 0.5 if with_cpt else False
    # If most visits lack notes in warehouse AND extract note count ≈ loaded notes → expected
    notes_aligned = (
        notes_with_case >= 0
        and abs(notes_with_case - int(wh["notes_loaded"] or 0)) < max(500, 0.05 * max(notes_with_case, 1))
    )
    cpt_aligned = (
        cpt_with_note >= 0
        and abs(cpt_with_note - int(wh["cpt_lines_loaded"] or 0))
        < max(500, 0.05 * max(cpt_with_note, 1))
    )

    if notes_aligned and cpt_aligned and pct_no_cpt >= 50:
        decision = "expected_gap"
        rationale = (
            "Extract≈loaded for case-aware notes/CPT, yet majority of pack visits have no CPT. "
            "Gap is schedule/clinical grain (no daily note), not a failed load of existing extract."
        )
    elif (notes_with_case >= 0 and notes_with_case > int(wh["notes_loaded"] or 0) * 1.1) or (
        cpt_with_note >= 0 and cpt_with_note > int(wh["cpt_lines_loaded"] or 0) * 1.1
    ):
        decision = "backfill_viable"
        rationale = (
            "Case-aware extract has materially more rows than warehouse — reload/backfill indicated."
        )
    else:
        decision = "mixed"
        rationale = (
            "Partial misalignment or thin extract; inspect monthly table before any backfill."
        )

    report = {
        "pack_window": "2026-01-01..2026-08-30",
        "extract": {
            "daily_notes_csv": notes_rows,
            "daily_notes_with_case_id": notes_with_case,
            "cpt_codes_csv": cpt_rows,
            "cpt_with_case_id": cpt_with_case,
            "cpt_with_case_and_daily_note_id": cpt_with_note,
            "paths": {"notes": str(notes_csv), "cpt": str(cpt_csv)},
        },
        "warehouse": {
            "visits_pack": visits_pack,
            "visits_with_note": with_note,
            "visits_with_cpt": with_cpt,
            "visits_no_cpt": no_cpt,
            "pct_visits_no_cpt": pct_no_cpt,
            "notes_loaded": int(wh["notes_loaded"] or 0),
            "cpt_lines_loaded": int(wh["cpt_lines_loaded"] or 0),
            "schedule_rows": int(wh["schedule_rows"] or 0),
        },
        "monthly": [dict(r) for r in monthly],
        "decision": decision,
        "rationale": rationale,
        "prior_sf_coverage_orphan": 61418,
        "actions": {
            "expected_gap": "Document as SF/schedule wider than billing grain; do not treat as matcher failure.",
            "backfill_viable": "Re-run case-aware extract merge + load-webpt + re-reconcile + coverage probe.",
            "mixed": "Diff extract vs DB on case_id/daily_note_id samples before loading legacy CPT.",
        }[decision],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("decision", "rationale", "warehouse", "extract")}, indent=2, default=str))
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
