#!/usr/bin/env python3
"""Build stratified sample of checked_out_no_cpt coverage gaps mapped to case units.

Writes:
  reports/checked_out_gap_sample_500.csv
  reports/checked_out_gap_sample_500_summary.json
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _flag(row: dict[str, str], key: str) -> bool:
    return (row.get(key) or "").strip() in {"1", "true", "TRUE", "yes", "Y"}


def _is_checked_out_no_cpt(vs: dict[str, str]) -> bool:
    st = (vs.get("visit_status") or "").strip().lower()
    checked_out = st == "completed" or _flag(vs, "has_check_out")
    if not checked_out:
        return False
    if _flag(vs, "has_cpt"):
        return False
    # checked_out_no_cpt: no note (or note without cpt is separate; plan targets no_cpt)
    return not _flag(vs, "has_note")


def stratified_sample(
    rows: list[dict[str, Any]], *, n: int, seed: int
) -> list[dict[str, Any]]:
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        dos = (r.get("dos") or "")[:10]
        month = dos[:7] if len(dos) >= 7 else "unknown"
        by_month[month].append(r)

    rng = random.Random(seed)
    for month in by_month:
        rng.shuffle(by_month[month])

    total = sum(len(v) for v in by_month.values()) or 1
    # Proportional quotas, then fill remainder
    quotas: dict[str, int] = {}
    assigned = 0
    months = sorted(by_month.keys())
    for i, month in enumerate(months):
        if i == len(months) - 1:
            quotas[month] = max(0, n - assigned)
        else:
            q = int(round(n * len(by_month[month]) / total))
            quotas[month] = min(len(by_month[month]), q)
            assigned += quotas[month]
    # Fix overshoot / undershoot
    selected: list[dict[str, Any]] = []
    for month in months:
        take = min(quotas[month], len(by_month[month]))
        selected.extend(by_month[month][:take])
    if len(selected) < n:
        used = {(r["patient_id"], r["dos"]) for r in selected}
        pool = [r for r in rows if (r["patient_id"], r["dos"]) not in used]
        rng.shuffle(pool)
        selected.extend(pool[: n - len(selected)])
    elif len(selected) > n:
        rng.shuffle(selected)
        selected = selected[:n]
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--case-root",
        type=Path,
        default=Path("/data/exports/side_by_side_case"),
    )
    ap.add_argument("--coverage-gap", type=Path, required=True)
    ap.add_argument("--visit-status", type=Path, required=True)
    ap.add_argument("--schedule-cases", type=Path, default=None)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Default: case-root/reports/checked_out_gap_sample_500.csv",
    )
    args = ap.parse_args()

    schedule_path = args.schedule_cases or (
        args.case_root / "schedule" / "schedule_cases.csv"
    )
    out_csv = args.out_csv or (
        args.case_root / "reports" / "checked_out_gap_sample_500.csv"
    )
    out_json = out_csv.with_name(out_csv.stem + "_summary.json")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    # visit_status index: emr+dos → best row
    vs_index: dict[tuple[str, str], dict[str, str]] = {}
    for row in _load_csv(args.visit_status):
        emr = (row.get("emr_id") or "").strip()
        dos = (row.get("dos") or "")[:10]
        if not emr or not dos:
            continue
        key = (emr, dos)
        prev = vs_index.get(key)
        if prev is None:
            vs_index[key] = row
            continue
        # Prefer completed + no cpt for this cohort
        score = (
            int(_is_checked_out_no_cpt(row)),
            int(_flag(row, "has_check_out")),
        )
        prev_score = (
            int(_is_checked_out_no_cpt(prev)),
            int(_flag(prev, "has_check_out")),
        )
        if score > prev_score:
            vs_index[key] = row

    gap_rows = _load_csv(args.coverage_gap)
    cohort: list[dict[str, str]] = []
    for g in gap_rows:
        emr = (g.get("emr_id") or "").strip()
        dos = (g.get("dos") or "")[:10]
        if not emr or not dos:
            continue
        vs = vs_index.get((emr, dos))
        if vs is None or not _is_checked_out_no_cpt(vs):
            continue
        cohort.append(
            {
                "emr_id": emr,
                "dos": dos,
                "patient": (g.get("patient") or vs.get("patient") or "").strip(),
                "sf_check": (g.get("sf_check") or "").strip(),
                "visit_status": (vs.get("visit_status") or "").strip(),
                "schedule_status": (vs.get("schedule_status") or "").strip(),
            }
        )

    # schedule map patient_id+dos → unit
    sched_map: dict[tuple[str, str], dict[str, str]] = {}
    for row in _load_csv(schedule_path):
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("dos") or "")[:10]
        case_id = (row.get("case_id") or "").strip()
        if not pid or not dos or not case_id:
            continue
        key = (pid, dos)
        # Prefer Checked Out / completed when duplicates
        st = (row.get("visit_status") or "").lower()
        prev = sched_map.get(key)
        if prev is None:
            sched_map[key] = row
            continue
        prev_st = (prev.get("visit_status") or "").lower()
        rank = 0 if "check" in st or st == "completed" else 1
        prev_rank = 0 if "check" in prev_st or prev_st == "completed" else 1
        if rank < prev_rank:
            sched_map[key] = row

    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, str]] = []
    for c in cohort:
        s = sched_map.get((c["emr_id"], c["dos"]))
        if s is None:
            unmapped.append(c)
            continue
        unit_id = (s.get("unit_id") or "").strip()
        if not unit_id:
            unit_id = (
                f"{s.get('facility_id')}:{s.get('case_id')}:"
                f"{s.get('patient_id')}:{c['dos']}"
            )
        mapped.append(
            {
                **c,
                "patient_id": (s.get("patient_id") or c["emr_id"]).strip(),
                "facility_id": (s.get("facility_id") or "").strip(),
                "facility_name": (s.get("facility_name") or "").strip(),
                "case_id": (s.get("case_id") or "").strip(),
                "unit_id": unit_id,
                "appointment_id": (s.get("appointment_id") or "").strip(),
                "ins_name": (s.get("ins_name") or "").strip(),
                "sched_visit_status": (s.get("visit_status") or "").strip(),
            }
        )

    # Dedupe by unit_id
    by_unit: dict[str, dict[str, Any]] = {}
    for r in mapped:
        by_unit[r["unit_id"]] = r
    mapped_unique = list(by_unit.values())

    sample = stratified_sample(mapped_unique, n=min(args.n, len(mapped_unique)), seed=args.seed)

    fieldnames = [
        "unit_id",
        "facility_id",
        "facility_name",
        "case_id",
        "patient_id",
        "emr_id",
        "dos",
        "patient",
        "sf_check",
        "visit_status",
        "schedule_status",
        "sched_visit_status",
        "appointment_id",
        "ins_name",
        "batch_id",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in sample:
            w.writerow({k: r.get(k, "") for k in fieldnames if k != "batch_id"} | {
                "batch_id": "checked_out_gap_sample_500"
            })

    by_month: dict[str, int] = defaultdict(int)
    by_fac: dict[str, int] = defaultdict(int)
    for r in sample:
        by_month[(r["dos"] or "")[:7]] += 1
        by_fac[r.get("facility_id") or ""] += 1

    summary = {
        "seed": args.seed,
        "n_requested": args.n,
        "n_sampled": len(sample),
        "cohort_checked_out_no_cpt_in_coverage_gap": len(cohort),
        "mapped_unique_units": len(mapped_unique),
        "unmapped_emr_dos": len(unmapped),
        "map_rate": round(len(mapped_unique) / max(1, len(cohort)), 4),
        "sample_by_month": dict(sorted(by_month.items())),
        "sample_by_facility_top15": dict(
            sorted(by_fac.items(), key=lambda kv: -kv[1])[:15]
        ),
        "out_csv": str(out_csv),
        "batch_id": "checked_out_gap_sample_500",
    }
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote {out_csv}", flush=True)
    print(f"Wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
