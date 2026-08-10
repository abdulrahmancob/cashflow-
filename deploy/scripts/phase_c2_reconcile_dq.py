#!/usr/bin/env python3
"""C2: run DB reconciliation + match DQ gates (not count-only)."""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

REPORT = Path("/data/exports/side_by_side_case/reports/phase_c2_reconcile_dq.json")
CPT_CSV = Path("/data/exports/side_by_side_case/extracted/cpt_codes.csv")


def main() -> int:
    from cashflow_db.db import connect
    from cashflow_ops.adapters.reconcile import reconcile_from_db

    print("[c2] reconcile_from_db starting...", flush=True)
    result = reconcile_from_db()
    summary = result.get("summary") or {}
    print("[c2] reconcile done:", summary, flush=True)

    run_id = summary.get("reconciliation_run_id")
    with connect() as conn:
        run = None
        if run_id:
            run = conn.execute(
                """
                SELECT reconciliation_run_id::text, status, row_count, notes, created_at, finished_at
                FROM billing.reconciliation_run
                WHERE reconciliation_run_id = %s::uuid
                """,
                (run_id,),
            ).fetchone()
        # If caller skipped reconcile, use latest success
        if run is None:
            run = conn.execute(
                """
                SELECT reconciliation_run_id::text, status, row_count, notes, created_at, finished_at
                FROM billing.reconciliation_run
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if run and not run_id:
                run_id = run["reconciliation_run_id"]
                summary.setdefault("reconciliation_run_id", run_id)
        status = (run["status"] if run else None) or "unknown"

        # Sample N CPT rows from CSV and check presence in visit_service_line via note+cpt
        sample_ok = 0
        sample_n = 0
        samples: list[dict] = []
        if CPT_CSV.is_file():
            rows = []
            with CPT_CSV.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if (row.get("case_id") or "").strip() and (row.get("cpt_code") or "").strip():
                        rows.append(row)
            random.seed(42)
            pick = rows if len(rows) <= 25 else random.sample(rows, 25)
            sample_n = len(pick)
            for row in pick:
                did = (row.get("daily_note_id") or "").strip()
                cpt = (row.get("cpt_code") or "").strip()
                hit = conn.execute(
                    """
                    SELECT 1
                    FROM core.visit_service_line vsl
                    JOIN core.clinical_note cn ON cn.visit_id = vsl.visit_id
                    WHERE cn.external_daily_note_id = %s
                      AND vsl.cpt_code = %s
                    LIMIT 1
                    """,
                    (did, cpt),
                ).fetchone()
                ok = hit is not None
                sample_ok += int(ok)
                samples.append(
                    {
                        "case_id": row.get("case_id"),
                        "daily_note_id": did,
                        "cpt_code": cpt,
                        "in_db": ok,
                    }
                )

    webpt = int(summary.get("webpt_lines") or 0)
    matched = int(summary.get("matched_lines") or 0)
    pending = int(summary.get("pending_lines") or 0)
    orphans = int(summary.get("orphan_payments") or 0)
    unmatched_webpt = pending  # pending ≈ unmatched webpt lines in this matcher
    match_rate = round(matched / webpt, 4) if webpt else 0.0

    gate = {
        "reconcile_result_ok": bool(result.get("ok")),
        "summary": summary,
        "run_status": status,
        "match_rate": match_rate,
        "matched_lines": matched,
        "unmatched_webpt_lines": unmatched_webpt,
        "orphan_payments": orphans,
        "sample_n": sample_n,
        "sample_ok": sample_ok,
        "sample_hit_rate": round(sample_ok / sample_n, 4) if sample_n else None,
        "samples": samples[:25],
        "pass_status_success": status == "success",
        "pass_webpt_gt_113": webpt > 113,
        "pass_nonzero_match_or_pending": (matched + pending) > 0 and webpt > 0,
        "pass_sample_hit_ge_0_5": (sample_ok / sample_n) >= 0.5 if sample_n else False,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(gate, indent=2, default=str), encoding="utf-8")
    print(json.dumps(gate, indent=2, default=str), flush=True)
    ok = (
        gate["pass_status_success"]
        and gate["pass_webpt_gt_113"]
        and gate["pass_nonzero_match_or_pending"]
        and gate["pass_sample_hit_ge_0_5"]
    )
    print(f"[c2] DQ_PASS={ok}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
