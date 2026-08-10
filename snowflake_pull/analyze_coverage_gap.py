#!/usr/bin/env python3
"""Bucket coverage_gap visits (SF paid+check in EOB, no recon visit) against clinical/schedule facts."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import parse_date  # noqa: E402

PACK_END = date(2026, 8, 30)
PACK_START = date(2026, 1, 1)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_keyset(path: Path, emr_col: str, dos_col: str) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for row in load_csv(path):
        emr = (row.get(emr_col) or "").strip()
        d = parse_date(row.get(dos_col))
        if emr and d:
            out.add((emr, d.isoformat()))
    return out


def load_emr_set(path: Path, col: str) -> set[str]:
    return {(r.get(col) or "").strip() for r in load_csv(path) if (r.get(col) or "").strip()}


def classify(
    coverage_rows: list[dict[str, str]],
    *,
    visit_keys: set[tuple[str, str]],
    note_keys: set[tuple[str, str]],
    schedule_keys: set[tuple[str, str]],
    known_emrs: set[str],
    checked_out_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Bucket coverage gaps.

    When checked_out_keys is provided, split has_clinical_visit into:
      - has_checked_out_visit (expect note/CPT)
      - has_non_checkout_visit (expected no-note: cancel/no_show/scheduled/checked-in)
    """
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, str]]] = {k: [] for k in (
        "outside_pack_window",
        "has_clinical_visit",
        "has_checked_out_visit",
        "has_non_checkout_visit",
        "has_clinical_note_only",
        "in_schedule_not_clinical",
        "patient_known_no_dos",
        "unknown_emr",
    )}

    for row in coverage_rows:
        emr = (row.get("emr_id") or "").strip()
        d = parse_date(row.get("dos"))
        if not emr or d is None:
            counts["unknown_emr"] += 1
            continue
        key = (emr, d.isoformat())

        if d < PACK_START or d > PACK_END:
            bucket = "outside_pack_window"
        elif key in visit_keys:
            if checked_out_keys is not None:
                bucket = (
                    "has_checked_out_visit"
                    if key in checked_out_keys
                    else "has_non_checkout_visit"
                )
            else:
                bucket = "has_clinical_visit"
        elif key in note_keys:
            bucket = "has_clinical_note_only"
        elif key in schedule_keys:
            bucket = "in_schedule_not_clinical"
        elif emr in known_emrs:
            bucket = "patient_known_no_dos"
        else:
            bucket = "unknown_emr"

        counts[bucket] += 1
        if len(samples[bucket]) < 12:
            samples[bucket].append(
                {
                    "emr_id": emr,
                    "dos": d.isoformat(),
                    "patient": row.get("patient") or "",
                    "clinic": row.get("clinic") or "",
                    "sf_check": row.get("sf_check") or "",
                    "bucket": bucket,
                }
            )

    total = sum(counts.values()) or 1
    pack_related = (
        counts["outside_pack_window"]
        + counts["in_schedule_not_clinical"]
        + counts["patient_known_no_dos"]
        + counts["unknown_emr"]
        + counts.get("has_non_checkout_visit", 0)
    )
    clinical_present_not_in_recon = (
        counts.get("has_clinical_visit", 0)
        + counts.get("has_checked_out_visit", 0)
        + counts["has_clinical_note_only"]
    )

    return {
        "total": total,
        "by_bucket": dict(counts.most_common()),
        "by_bucket_pct": {k: round(100.0 * v / total, 1) for k, v in counts.most_common()},
        "summary": {
            "outside_or_not_in_clinical_extract_est": pack_related,
            "clinical_exists_but_missing_from_recon_run": clinical_present_not_in_recon,
            "pct_outside_or_not_extracted": round(100.0 * pack_related / total, 1),
            "pct_clinical_present_recon_miss": round(
                100.0 * clinical_present_not_in_recon / total, 1
            ),
            "contract": (
                "Expect note/CPT only for Checked Out (completed). "
                "cancel/no_show/scheduled/checked-in counted under has_non_checkout_visit "
                "when visit-status export is provided."
            ),
        },
        "samples": samples,
    }


def load_checked_out_keys(path: Path) -> set[tuple[str, str]]:
    """EMR+DOS where visit is Checked Out (completed) or has check_out_at."""
    out: set[tuple[str, str]] = set()
    for row in load_csv(path):
        emr = (row.get("emr_id") or "").strip()
        d = parse_date(row.get("dos"))
        if not emr or d is None:
            continue
        st = (row.get("visit_status") or "").strip().lower()
        has_co = (row.get("has_check_out") or "").strip() in {"1", "true", "TRUE", "yes"}
        if st == "completed" or has_co:
            out.add((emr, d.isoformat()))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coverage-visits", type=Path, required=True)
    p.add_argument("--clinical-visits", type=Path, required=True, help="CSV emr_id,dos from core.visit")
    p.add_argument("--clinical-notes", type=Path, required=True, help="CSV emr_id,dos from notes/cases")
    p.add_argument("--schedule", type=Path, required=True, help="CSV emr_id,dos from schedule")
    p.add_argument("--patients", type=Path, required=True, help="CSV webpt_patient_id")
    p.add_argument(
        "--visit-status",
        type=Path,
        default=None,
        help="Optional CSV with visit_status/has_check_out to split checkout vs expected",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/coverage_gap_by_bucket.json"),
    )
    args = p.parse_args(argv)

    checked_out = (
        load_checked_out_keys(args.visit_status)
        if args.visit_status and args.visit_status.is_file()
        else None
    )
    report = classify(
        load_csv(args.coverage_visits),
        visit_keys=load_keyset(args.clinical_visits, "emr_id", "dos"),
        note_keys=load_keyset(args.clinical_notes, "emr_id", "dos"),
        schedule_keys=load_keyset(args.schedule, "emr_id", "dos"),
        known_emrs=load_emr_set(args.patients, "webpt_patient_id"),
        checked_out_keys=checked_out,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"by_bucket": report["by_bucket"], "summary": report["summary"], "total": report["total"]}, indent=2))
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
