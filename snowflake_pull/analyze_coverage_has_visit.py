#!/usr/bin/env python3
"""Sub-bucket coverage gaps with schedule/visit status awareness.

Contract: expect note/CPT only for Checked Out (completed) or check_out_at.
cancel / no_show / scheduled-only / checked-in-only = expected no-note.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import parse_date  # noqa: E402
from snowflake_pull.analyze_coverage_by_schedule_status import (  # noqa: E402
    classify as classify_by_status,
    load_visit_status_index,
)
from snowflake_pull.analyze_coverage_gap import load_keyset  # noqa: E402


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def classify_has_visit_legacy(
    coverage_rows: list[dict[str, str]],
    *,
    visit_keys: set[tuple[str, str]],
    note_keys: set[tuple[str, str]],
    schedule_keys: set[tuple[str, str]],
    cpt_keys: set[tuple[str, str]],
    recon_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    """Legacy path without status (kept for backward-compatible CLI)."""
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, str]]] = {
        k: []
        for k in (
            "schedule_orphan_no_note_no_cpt",
            "note_without_cpt",
            "has_cpt_missing_from_run",
            "false_positive_already_in_recon",
            "not_has_clinical_visit",
            "other",
        )
    }

    for row in coverage_rows:
        emr = (row.get("emr_id") or "").strip()
        d = parse_date(row.get("dos"))
        if not emr or d is None:
            counts["other"] += 1
            continue
        key = (emr, d.isoformat())
        if key not in visit_keys:
            bucket = "not_has_clinical_visit"
        elif key in recon_keys:
            bucket = "false_positive_already_in_recon"
        elif key in cpt_keys:
            bucket = "has_cpt_missing_from_run"
        elif key in note_keys:
            bucket = "note_without_cpt"
        else:
            bucket = "schedule_orphan_no_note_no_cpt"

        counts[bucket] += 1
        if len(samples[bucket]) < 12:
            samples[bucket].append(
                {
                    "emr_id": emr,
                    "dos": d.isoformat(),
                    "patient": row.get("patient") or "",
                    "sf_check": row.get("sf_check") or "",
                    "has_schedule": "1" if key in schedule_keys else "0",
                    "bucket": bucket,
                }
            )

    total = sum(counts.values()) or 1
    return {
        "total_coverage_rows": total,
        "has_clinical_visit_approx": total - counts.get("not_has_clinical_visit", 0),
        "by_sub_bucket": dict(counts.most_common()),
        "by_sub_bucket_pct": {
            k: round(100.0 * v / total, 1) for k, v in counts.most_common()
        },
        "samples": samples,
        "status_aware": False,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coverage-visits", type=Path, required=True)
    p.add_argument("--clinical-visits", type=Path, default=None)
    p.add_argument("--clinical-notes", type=Path, default=None)
    p.add_argument("--schedule", type=Path, default=None)
    p.add_argument("--recon-lines", type=Path, default=None)
    p.add_argument("--service-lines", type=Path, default=None)
    p.add_argument(
        "--visit-status",
        type=Path,
        default=None,
        help="CSV emr_id,dos,visit_status,has_check_out,has_note,has_cpt — preferred",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/coverage_has_visit_subbuckets.json"),
    )
    args = p.parse_args(argv)

    coverage_rows = load_csv(args.coverage_visits)

    if args.visit_status and args.visit_status.is_file():
        report = classify_by_status(
            coverage_rows,
            load_visit_status_index(args.visit_status),
        )
        # Alias keys for prior consumers
        report["by_sub_bucket"] = report["by_bucket"]
        report["by_sub_bucket_pct"] = report["by_bucket_pct"]
        report["has_clinical_visit_approx"] = (
            report["total_coverage_rows"] - report["summary"].get("no_core_visit", 0)
        )
        report["status_aware"] = True
        report["service_lines_provided"] = True
    else:
        if not all(
            p and Path(p).is_file()
            for p in (args.clinical_visits, args.clinical_notes, args.schedule, args.recon_lines)
        ):
            print(
                "Need --visit-status OR all of --clinical-visits/--clinical-notes/"
                "--schedule/--recon-lines",
                file=sys.stderr,
            )
            return 2
        recon_keys: set[tuple[str, str]] = set()
        for row in load_csv(args.recon_lines):
            emr = (row.get("webpt_patient_id") or "").strip()
            d = parse_date(row.get("date_of_service"))
            if emr and d:
                recon_keys.add((emr, d.isoformat()))
        cpt_keys = (
            load_keyset(args.service_lines, "emr_id", "dos")
            if args.service_lines and args.service_lines.is_file()
            else set()
        )
        report = classify_has_visit_legacy(
            coverage_rows,
            visit_keys=load_keyset(args.clinical_visits, "emr_id", "dos"),
            note_keys=load_keyset(args.clinical_notes, "emr_id", "dos"),
            schedule_keys=load_keyset(args.schedule, "emr_id", "dos"),
            cpt_keys=cpt_keys,
            recon_keys=recon_keys,
        )
        report["service_lines_provided"] = bool(cpt_keys)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "by_sub_bucket": report.get("by_sub_bucket") or report.get("by_bucket"),
                "summary": report.get("summary"),
                "status_aware": report.get("status_aware"),
            },
            indent=2,
        )
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
