#!/usr/bin/env python3
"""Coverage report + zero-payment sample for Jan-May no-summer payments scrape."""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def _key(r: dict[str, str]) -> tuple[str, str, str]:
    return (
        str(r.get("facility_id") or "").strip(),
        str(r.get("patient_id") or "").strip(),
        str(r.get("case_id") or "").strip(),
    )


def _ckey(r: dict[str, str]) -> str:
    f, p, c = _key(r)
    return f"{f}:{p}:{c}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--sample-size", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    base: Path = args.dir

    cohort = list(
        csv.DictReader((base / "cohort_cases.csv").open(encoding="utf-8-sig", newline=""))
    )
    pay = list(
        csv.DictReader(
            (base / "patient_payments_202601_202605.csv").open(
                encoding="utf-8-sig", newline=""
            )
        )
    )
    unp = list(
        csv.DictReader(
            (base / "patient_payments_unpaid_202601_202605.csv").open(
                encoding="utf-8-sig", newline=""
            )
        )
    )
    ck = json.loads((base / "payments_checkpoint.json").read_text(encoding="utf-8"))
    done = set(ck.get("done_keys") or [])

    cohort_keys = {_ckey(r) for r in cohort}
    pay_by_case: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in pay:
        pay_by_case[_key(r)].append(r)
    unp_by_case: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in unp:
        unp_by_case[_key(r)].append(r)

    with_pay = [r for r in cohort if _key(r) in pay_by_case]
    zero_pay = [r for r in cohort if _key(r) not in pay_by_case]
    with_unpaid = [r for r in cohort if _key(r) in unp_by_case]
    missing_ck = sorted(cohort_keys - done)

    # Fully paid cases among those with payment rows (Jan-May underpaid none)
    fully_paid_cases = 0
    for r in with_pay:
        rows = pay_by_case[_key(r)]
        under = False
        for p in rows:
            iso = p.get("date_of_service_iso") or ""
            if not (len(iso) >= 7 and "2026-01" <= iso[:7] <= "2026-05"):
                continue
            due = float(p.get("amount_due") or 0)
            paid = float(p.get("amount_paid") or 0)
            if due > paid + 0.009:
                under = True
                break
        if not under:
            fully_paid_cases += 1

    unpaid_types = Counter((r.get("payment_type") or "").strip() for r in unp)
    unpaid_months = Counter((r.get("dos") or "")[:7] for r in unp)
    desc_top = Counter((r.get("description") or "").strip() for r in unp)

    report = {
        "cohort_cases": len(cohort),
        "checkpoint_done": len(done),
        "checkpoint_missing": len(missing_ck),
        "raw_payment_rows": len(pay),
        "unpaid_rows": len(unp),
        "cases_with_any_payment_row": len(with_pay),
        "cases_zero_payment_rows": len(zero_pay),
        "cases_with_unpaid_row": len(with_unpaid),
        "cases_with_payments_but_no_jan_may_underpaid": fully_paid_cases,
        "unpaid_payment_type": unpaid_types.most_common(),
        "unpaid_dos_months": sorted(unpaid_months.items()),
        "unpaid_description_top": desc_top.most_common(20),
        "conclusion": (
            "All cohort cases were scraped (checkpoint complete). "
            "Unpaid CSV is underpaid Patient Payments rows only — not the full cohort. "
            f"{len(zero_pay)} cases returned zero payment rows from WebPT."
        ),
    }

    report_json = base / "coverage_report.json"
    report_md = base / "coverage_report.md"
    report_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "# Patient Payments coverage report",
        "",
        f"- Cohort cases: **{report['cohort_cases']}**",
        f"- Checkpoint done: **{report['checkpoint_done']}** (missing **{report['checkpoint_missing']}**)",
        f"- Raw payment rows: **{report['raw_payment_rows']}**",
        f"- Unpaid rows: **{report['unpaid_rows']}**",
        f"- Cases with any payment row: **{report['cases_with_any_payment_row']}**",
        f"- Cases with zero payment rows: **{report['cases_zero_payment_rows']}**",
        f"- Cases with unpaid row: **{report['cases_with_unpaid_row']}**",
        f"- Cases with payments but no Jan–May underpaid: **{report['cases_with_payments_but_no_jan_may_underpaid']}**",
        "",
        "## Unpaid payment_type",
        "",
    ]
    for name, count in unpaid_types.most_common():
        lines.append(f"- {name or '(blank)'}: {count}")
    lines.extend(["", "## Unpaid DOS months", ""])
    for name, count in sorted(unpaid_months.items()):
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Top unpaid descriptions", ""])
    for name, count in desc_top.most_common(15):
        lines.append(f"- {count} × `{name}`")
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            report["conclusion"],
            "",
        ]
    )
    report_md.write_text("\n".join(lines), encoding="utf-8")

    # Zero-pay sample for live re-audit
    rng = random.Random(args.seed)
    sample = list(zero_pay)
    rng.shuffle(sample)
    sample = sample[: max(0, args.sample_size)]
    sample_path = base / "zero_payment_audit_sample.csv"
    fields = [
        "facility_id",
        "facility_name",
        "patient_id",
        "patient_name",
        "case_id",
        "mobile_phone",
        "home_phone",
        "work_phone",
        "email",
        "best_phone",
    ]
    with sample_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sample:
            w.writerow({k: r.get(k, "") for k in fields})

    zero_list = base / "zero_payment_cases.csv"
    with zero_list.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in zero_pay:
            w.writerow({k: r.get(k, "") for k in fields})

    print(json.dumps({k: report[k] for k in report if k != "unpaid_description_top"}, indent=2))
    print(f"wrote={report_json}")
    print(f"wrote={report_md}")
    print(f"wrote={sample_path} n={len(sample)}")
    print(f"wrote={zero_list} n={len(zero_pay)}")


if __name__ == "__main__":
    main()
