#!/usr/bin/env python3
"""Bucket SF coverage-gap visits by schedule/visit status.

Operational contract: note/CPT expected only for Checked Out (completed) or
check_out_at present. cancel / no_show / scheduled-only = expected no-note.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import parse_date  # noqa: E402

# Prefer stronger clinical evidence when multiple visit rows share EMR+DOS
_STATUS_RANK = {
    "completed": 0,
    "unchecked_out": 1,
    "scheduled": 2,
    "confirmed": 2,
    "no_show": 3,
    "cancelled": 4,
}


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _flag(row: dict[str, str], key: str) -> bool:
    return (row.get(key) or "").strip() in {"1", "true", "TRUE", "yes", "Y"}


def load_visit_status_index(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """emr+dos → best visit row (prefer completed, then has_cpt/note)."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in load_csv(path):
        emr = (row.get("emr_id") or "").strip()
        d = parse_date(row.get("dos"))
        if not emr or d is None:
            continue
        key = (emr, d.isoformat())
        st = (row.get("visit_status") or "").strip().lower() or "scheduled"
        cand = {
            "visit_status": st,
            "schedule_status": (row.get("schedule_status") or "").strip().lower(),
            "has_check_out": _flag(row, "has_check_out"),
            "has_note": _flag(row, "has_note"),
            "has_cpt": _flag(row, "has_cpt"),
        }
        prev = best.get(key)
        if prev is None:
            best[key] = cand
            continue
        prev_rank = _STATUS_RANK.get(prev["visit_status"], 9)
        new_rank = _STATUS_RANK.get(st, 9)
        prev_score = (
            -prev_rank,
            int(prev["has_cpt"]),
            int(prev["has_note"]),
            int(prev["has_check_out"]),
        )
        new_score = (
            -new_rank,
            int(cand["has_cpt"]),
            int(cand["has_note"]),
            int(cand["has_check_out"]),
        )
        if new_score > prev_score:
            best[key] = cand
    return best


def classify_row(visit: dict[str, Any] | None) -> str:
    if visit is None:
        return "no_core_visit"

    st = visit["visit_status"]
    checked_out = st == "completed" or visit["has_check_out"]

    if st in {"cancelled", "no_show"}:
        return "cancelled_or_no_show"
    if st in {"scheduled", "confirmed"} and not checked_out:
        return "scheduled_never_arrived"
    if st == "unchecked_out" and not checked_out:
        return "checked_in_only"

    # Expect note/CPT path
    if checked_out or st == "completed":
        if visit["has_cpt"]:
            return "checked_out_has_cpt_not_in_recon"
        if visit["has_note"]:
            return "checked_out_note_no_cpt"
        return "checked_out_no_cpt"

    return "other_status"


def classify(
    coverage_rows: list[dict[str, str]],
    visit_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    status_hist: Counter[str] = Counter()
    samples: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in coverage_rows:
        emr = (row.get("emr_id") or "").strip()
        d = parse_date(row.get("dos"))
        if not emr or d is None:
            counts["no_core_visit"] += 1
            continue
        key = (emr, d.isoformat())
        visit = visit_index.get(key)
        bucket = classify_row(visit)
        counts[bucket] += 1
        if visit:
            status_hist[visit["visit_status"]] += 1
        if len(samples[bucket]) < 12:
            samples[bucket].append(
                {
                    "emr_id": emr,
                    "dos": d.isoformat(),
                    "patient": row.get("patient") or "",
                    "sf_check": row.get("sf_check") or "",
                    "visit_status": (visit or {}).get("visit_status", ""),
                    "schedule_status": (visit or {}).get("schedule_status", ""),
                    "has_note": str(int(bool((visit or {}).get("has_note")))),
                    "has_cpt": str(int(bool((visit or {}).get("has_cpt")))),
                    "bucket": bucket,
                }
            )

    total = sum(counts.values()) or 1
    expected = (
        counts.get("cancelled_or_no_show", 0)
        + counts.get("scheduled_never_arrived", 0)
        + counts.get("checked_in_only", 0)
    )
    real_gap = (
        counts.get("checked_out_no_cpt", 0)
        + counts.get("checked_out_note_no_cpt", 0)
        + counts.get("checked_out_has_cpt_not_in_recon", 0)
    )
    return {
        "total_coverage_rows": total,
        "by_bucket": dict(counts.most_common()),
        "by_bucket_pct": {k: round(100.0 * v / total, 1) for k, v in counts.most_common()},
        "visit_status_hist_on_matched_keys": dict(status_hist.most_common()),
        "summary": {
            "expected_no_note": expected,
            "pct_expected_no_note": round(100.0 * expected / total, 1),
            "real_gap_checked_out_missing_billing": real_gap,
            "pct_real_gap": round(100.0 * real_gap / total, 1),
            "no_core_visit": counts.get("no_core_visit", 0),
            "contract": (
                "Expect note/CPT only when visit status=completed (Checked Out) "
                "or check_out_at present. cancel/no_show/scheduled = expected gap."
            ),
        },
        "samples": dict(samples),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coverage-visits", type=Path, required=True)
    p.add_argument("--visit-status", type=Path, required=True)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/coverage_by_schedule_status.json"),
    )
    args = p.parse_args(argv)

    report = classify(
        load_csv(args.coverage_visits),
        load_visit_status_index(args.visit_status),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    sample_path = args.out.with_name("coverage_by_schedule_status_samples.csv")
    flat: list[dict[str, str]] = []
    for rows in report["samples"].values():
        flat.extend(rows)
    if flat:
        with sample_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)

    print(
        json.dumps(
            {
                "by_bucket": report["by_bucket"],
                "summary": report["summary"],
                "visit_status_hist": report["visit_status_hist_on_matched_keys"],
            },
            indent=2,
        )
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
