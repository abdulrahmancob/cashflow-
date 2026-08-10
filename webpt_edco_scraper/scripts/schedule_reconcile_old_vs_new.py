#!/usr/bin/env python3
"""Reconcile old Jul-28 no-upcoming sheet vs new no-summer Office Visit sheet using schedule."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

CANCELLED = "Cancelled/No Show"
AS_OF = "2026-07-28"
JAN_MAY = {"2026-01", "2026-02", "2026-03", "2026-04", "2026-05"}
SUMMER = {"2026-06", "2026-07", "2026-08"}


def _case_dos(r: dict[str, str]) -> str:
    return f"{str(r.get('case_id') or '').strip()}|{str(r.get('dos') or '').strip()}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-csv", type=Path, required=True)
    ap.add_argument("--new-csv", type=Path, required=True)
    ap.add_argument("--schedule-csv", type=Path, required=True)
    ap.add_argument("--cohort-csv", type=Path, required=True)
    ap.add_argument("--raw-payments-csv", type=Path, required=True)
    ap.add_argument("--unpaid-csv", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    old = list(csv.DictReader(args.old_csv.open(encoding="utf-8-sig", newline="")))
    new = list(csv.DictReader(args.new_csv.open(encoding="utf-8-sig", newline="")))
    cohort = list(csv.DictReader(args.cohort_csv.open(encoding="utf-8-sig", newline="")))
    pay = list(csv.DictReader(args.raw_payments_csv.open(encoding="utf-8-sig", newline="")))
    unp = list(csv.DictReader(args.unpaid_csv.open(encoding="utf-8-sig", newline="")))

    old_by = {_case_dos(r): r for r in old}
    new_by = {_case_dos(r): r for r in new}
    both_keys = set(old_by) & set(new_by)
    only_old_keys = set(old_by) - set(new_by)
    only_new_keys = set(new_by) - set(old_by)

    old_cases = {str(r.get("case_id") or "").strip() for r in old}
    new_cases = {str(r.get("case_id") or "").strip() for r in new}
    cohort_cases = {str(r.get("case_id") or "").strip() for r in cohort}
    cases_only_old = sorted(old_cases - new_cases)
    cases_only_new = sorted(new_cases - old_cases)

    need = set(cases_only_old)
    sched: dict[str, dict] = defaultdict(
        lambda: {
            "jan_may": 0,
            "jun": 0,
            "jul": 0,
            "aug": 0,
            "jun_jul_aug": 0,
            "jm_cancelled": 0,
            "summer_before_asof": 0,
            "summer_on_or_after_asof": 0,
            "facility_id": "",
            "patient_id": "",
            "sample": [],
        }
    )
    with args.schedule_csv.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            cid = str(r.get("case_id") or "").strip()
            if cid not in need:
                continue
            d = sched[cid]
            if not d["facility_id"]:
                d["facility_id"] = str(r.get("facility_id") or "").strip()
                d["patient_id"] = str(r.get("patient_id") or "").strip()
            at = (r.get("appointment_at") or "")[:10]
            ym = at[:7]
            st = (r.get("visit_status") or "").strip()
            if len(d["sample"]) < 5:
                d["sample"].append(f"{at}|{st}")
            if ym in JAN_MAY:
                if st == CANCELLED:
                    d["jm_cancelled"] += 1
                else:
                    d["jan_may"] += 1
            if ym in SUMMER and st != CANCELLED:
                d["jun_jul_aug"] += 1
                if ym == "2026-06":
                    d["jun"] += 1
                elif ym == "2026-07":
                    d["jul"] += 1
                elif ym == "2026-08":
                    d["aug"] += 1
                if at and at < AS_OF:
                    d["summer_before_asof"] += 1
                elif at:
                    d["summer_on_or_after_asof"] += 1

    pay_by_case: dict[str, list] = defaultdict(list)
    for r in pay:
        pay_by_case[str(r.get("case_id") or "").strip()].append(r)
    unp_cases = {str(r.get("case_id") or "").strip() for r in unp}
    ov_under_cases: set[str] = set()
    ov_paid_cases: set[str] = set()
    for cid in cases_only_old:
        for r in pay_by_case.get(cid, []):
            desc = (r.get("description") or "").lower()
            if "office visit copay" not in desc:
                continue
            due = float(r.get("amount_due") or 0)
            paid_amt = float(r.get("amount_paid") or 0)
            if due > paid_amt + 0.009:
                ov_under_cases.add(cid)
            else:
                ov_paid_cases.add(cid)

    old_rows_by_case: Counter[str] = Counter(
        str(r.get("case_id") or "").strip() for r in old
    )
    old_meta = {}
    for r in old:
        cid = str(r.get("case_id") or "").strip()
        if cid not in old_meta:
            old_meta[cid] = (
                str(r.get("facility_id") or ""),
                str(r.get("patient_id") or ""),
            )

    rows_out = []
    class_counts: Counter[str] = Counter()
    for cid in cases_only_old:
        d = sched.get(cid) or {
            "jan_may": 0,
            "jun": 0,
            "jul": 0,
            "aug": 0,
            "jun_jul_aug": 0,
            "jm_cancelled": 0,
            "summer_before_asof": 0,
            "summer_on_or_after_asof": 0,
            "facility_id": "",
            "patient_id": "",
            "sample": [],
        }
        if not d["facility_id"] and cid in old_meta:
            d["facility_id"], d["patient_id"] = old_meta[cid]

        in_cohort = cid in cohort_cases
        if d["jun_jul_aug"] > 0:
            klass = "has_summer"
        elif in_cohort and cid in ov_under_cases:
            klass = "still_underpaid_missing_from_new"
        elif in_cohort and cid in ov_paid_cases:
            klass = "now_paid"
        elif in_cohort and cid in unp_cases:
            # still have some unpaid line (cx/NS/etc) but no Office Visit underpaid
            klass = "unpaid_non_ov_now"
        elif (not in_cohort) and d["jan_may"] == 0 and d["jm_cancelled"] > 0:
            klass = "cancelled_only"
        elif in_cohort and cid not in pay_by_case:
            klass = "in_cohort_zero_payments"
        elif not in_cohort:
            klass = "not_in_cohort_other"
        else:
            klass = "other"
        class_counts[klass] += 1

        rows_out.append(
            {
                "case_id": cid,
                "facility_id": d["facility_id"],
                "patient_id": d["patient_id"],
                "classification": klass,
                "in_new_cohort": "1" if in_cohort else "0",
                "old_sheet_rows": str(old_rows_by_case.get(cid, 0)),
                "sched_jan_may_active": str(d["jan_may"]),
                "sched_jun": str(d["jun"]),
                "sched_jul": str(d["jul"]),
                "sched_aug": str(d["aug"]),
                "sched_jun_jul_aug_active": str(d["jun_jul_aug"]),
                "sched_jm_cancelled": str(d["jm_cancelled"]),
                "summer_before_asof": str(d["summer_before_asof"]),
                "summer_on_or_after_asof": str(d["summer_on_or_after_asof"]),
                "has_any_unpaid_now": "1" if cid in unp_cases else "0",
                "has_ov_underpaid_now": "1" if cid in ov_under_cases else "0",
                "has_ov_fully_paid_now": "1" if cid in ov_paid_cases else "0",
                "sample_appointments": "; ".join(d["sample"]),
            }
        )

    fields = list(rows_out[0].keys()) if rows_out else ["case_id", "classification"]
    cases_csv = out / "cases_only_in_old_vs_schedule.csv"
    with cases_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    summary = {
        "old_rows": len(old),
        "new_rows": len(new),
        "overlap_case_dos": len(both_keys),
        "only_old_case_dos": len(only_old_keys),
        "only_new_case_dos": len(only_new_keys),
        "old_cases": len(old_cases),
        "new_cases": len(new_cases),
        "cases_only_old": len(cases_only_old),
        "cases_only_new": len(cases_only_new),
        "cases_only_old_classification": dict(class_counts),
        "as_of": AS_OF,
        "drain_cases_remaining_at_run": None,
        "conclusion": (
            "Old sheet is larger because it used no-upcoming-as-of-2026-07-28 "
            "(allows Jun/early-Jul visits) on a broader unpaid scrape; "
            "new sheet uses no Jun/Jul/Aug schedule + current underpaid Office Visit Copay."
        ),
    }
    (out / "schedule_reconcile_old_vs_new.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    md_lines = [
        "# Schedule reconcile: old Jul-28 sheet vs new no-summer sheet",
        "",
        "## Definitions",
        "",
        "- **Old** (`missing_payments_no_upcoming_jan_may_asof_2026-07-28.csv`): Copay unpaid + no appointment on/after **2026-07-28**.",
        "- **New** (`patient_payments_unpaid_jan_may_2026.csv`): case has Jan–May schedule, **no** non-cancelled Jun/Jul/Aug, Office Visit Copay underpaid now.",
        "",
        "## Counts",
        "",
        f"- Old rows: **{len(old)}** / cases **{len(old_cases)}**",
        f"- New rows: **{len(new)}** / cases **{len(new_cases)}**",
        f"- Overlap `case_id|dos`: **{len(both_keys)}**",
        f"- Only in old: **{len(only_old_keys)}** rows / **{len(cases_only_old)}** cases",
        f"- Only in new: **{len(only_new_keys)}** rows / **{len(cases_only_new)}** cases",
        "",
        "## Cases only in old — schedule classification",
        "",
    ]
    for k, v in class_counts.most_common():
        md_lines.append(f"- `{k}`: **{v}**")
    md_lines.extend(
        [
            "",
            "## Conclusion",
            "",
            summary["conclusion"],
            "",
            f"Detail: `{cases_csv.name}`",
            "",
        ]
    )
    md_path = out / "schedule_reconcile_old_vs_new.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    cov = out / "coverage_report.md"
    if cov.exists():
        text = cov.read_text(encoding="utf-8")
        marker = "## Schedule reconcile vs old Jul-28 sheet"
        block = "\n".join(md_lines[md_lines.index("## Counts") :])
        if marker in text:
            text = text.split(marker)[0].rstrip()
        cov.write_text(text + f"\n\n{marker}\n\n" + block + "\n", encoding="utf-8")
        cov_json = out / "coverage_report.json"
        if cov_json.exists():
            data = json.loads(cov_json.read_text(encoding="utf-8"))
            data["schedule_reconcile_old_vs_new"] = summary
            cov_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"wrote={cases_csv}")
    print(f"wrote={md_path}")


if __name__ == "__main__":
    main()
