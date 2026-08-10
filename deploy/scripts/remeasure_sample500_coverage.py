#!/usr/bin/env python3
"""After load-webpt, remasure how many of the 500 sample units gained note/CPT flags."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample-csv", type=Path, required=True)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("/home/abdu/sf_eval/checked_out_gap_sample_500_remeasure.json"),
    )
    args = ap.parse_args()

    sample = list(csv.DictReader(args.sample_csv.open(encoding="utf-8-sig", newline="")))
    keys = [((r.get("emr_id") or r.get("patient_id") or "").strip(), (r.get("dos") or "")[:10]) for r in sample]

    sql = r"""
COPY (
  SELECT
    p.webpt_patient_id AS emr_id,
    v.service_date::text AS dos,
    CASE WHEN EXISTS (SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id=v.visit_id) THEN 1 ELSE 0 END AS has_note,
    CASE WHEN EXISTS (SELECT 1 FROM core.visit_service_line sl WHERE sl.visit_id=v.visit_id) THEN 1 ELSE 0 END AS has_cpt
  FROM core.visit v
  JOIN core.patient p ON p.patient_id=v.patient_id
  WHERE p.webpt_patient_id IS NOT NULL
) TO STDOUT WITH CSV HEADER
"""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "cashflow-postgres-1",
            "psql",
            "-U",
            "cashflow",
            "-d",
            "cashflow",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = proc.stdout.splitlines()
    reader = csv.DictReader(lines)
    idx = {
        ((row.get("emr_id") or "").strip(), (row.get("dos") or "")[:10]): row
        for row in reader
    }

    gained_note = 0
    gained_cpt = 0
    still_no_cpt = 0
    still_no_note = 0
    missing_visit = 0
    for emr, dos in keys:
        row = idx.get((emr, dos))
        if row is None:
            missing_visit += 1
            still_no_note += 1
            still_no_cpt += 1
            continue
        hn = (row.get("has_note") or "0").strip() == "1"
        hc = (row.get("has_cpt") or "0").strip() == "1"
        if hn:
            gained_note += 1
        else:
            still_no_note += 1
        if hc:
            gained_cpt += 1
        else:
            still_no_cpt += 1

    n = len(keys) or 1
    report = {
        "sample_n": len(keys),
        "has_note_now": gained_note,
        "has_cpt_now": gained_cpt,
        "still_no_note": still_no_note,
        "still_no_cpt": still_no_cpt,
        "missing_visit": missing_visit,
        "pct_has_note": round(100.0 * gained_note / n, 1),
        "pct_has_cpt": round(100.0 * gained_cpt / n, 1),
        "note": (
            "Baseline sample was checked_out_no_cpt (has_note=0,has_cpt=0). "
            "These are post load-webpt warehouse flags for the same EMR+DOS."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
