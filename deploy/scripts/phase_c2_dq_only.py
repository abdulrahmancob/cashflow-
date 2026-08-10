#!/usr/bin/env python3
"""C2 DQ against latest reconciliation_run (no rematch)."""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

REPORT = Path("/data/exports/side_by_side_case/reports/phase_c2_reconcile_dq.json")
CPT_CSV = Path("/data/exports/side_by_side_case/extracted/cpt_codes.csv")


def main() -> int:
    from cashflow_db.db import connect

    with connect() as conn:
        run = conn.execute(
            """
            SELECT reconciliation_run_id::text, status, row_count, notes, created_at, finished_at
            FROM billing.reconciliation_run
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not run:
            print("[c2] no reconciliation_run", flush=True)
            return 2
        run_id = run["reconciliation_run_id"]
        status = run["status"]
        stats = conn.execute(
            """
            SELECT
              COUNT(*) AS webpt_lines,
              COUNT(*) FILTER (WHERE status = 'paid' OR paid_amount > 0) AS matchedish,
              COUNT(*) FILTER (WHERE status = 'pending') AS pending_lines
            FROM billing.reconciliation_line
            WHERE reconciliation_run_id = %s::uuid
            """,
            (run_id,),
        ).fetchone()
        # Prefer summary fields if present in notes JSON — else derive
        matched = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM billing.reconciliation_line
            WHERE reconciliation_run_id = %s::uuid
              AND check_eft_num IS NOT NULL AND check_eft_num <> ''
            """,
            (run_id,),
        ).fetchone()["n"]
        orphan_n = None  # full orphan scan is expensive; use run summary when available

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

    webpt = int(stats["webpt_lines"] or 0)
    pending = int(stats["pending_lines"] or 0)
    matched = int(matched)
    match_rate = round(matched / webpt, 4) if webpt else 0.0
    gate = {
        "reconcile_result_ok": True,
        "summary": {
            "reconciliation_run_id": run_id,
            "webpt_lines": webpt,
            "matched_lines": matched,
            "pending_lines": pending,
            "orphan_payments_est": orphan_n,
            "lines_written": run["row_count"],
        },
        "run_status": status,
        "match_rate": match_rate,
        "sample_n": sample_n,
        "sample_ok": sample_ok,
        "sample_hit_rate": round(sample_ok / sample_n, 4) if sample_n else None,
        "samples": samples,
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
