#!/usr/bin/env python3
"""Summarize a re-scrape of previously zero-payment sample cases."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, required=True)
    ap.add_argument("--payments", type=Path, required=True, help="re-audit payments dump")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    sample = list(csv.DictReader(args.sample.open(encoding="utf-8-sig", newline="")))
    pay_rows = []
    if args.payments.exists() and args.payments.stat().st_size > 0:
        pay_rows = list(
            csv.DictReader(args.payments.open(encoding="utf-8-sig", newline=""))
        )

    sample_keys = {
        (
            str(r.get("facility_id") or "").strip(),
            str(r.get("patient_id") or "").strip(),
            str(r.get("case_id") or "").strip(),
        )
        for r in sample
    }
    pay_keys = {
        (
            str(r.get("facility_id") or "").strip(),
            str(r.get("patient_id") or "").strip(),
            str(r.get("case_id") or "").strip(),
        )
        for r in pay_rows
    }
    newly_nonempty = sorted(sample_keys & pay_keys)
    still_empty = sorted(sample_keys - pay_keys)

    # underpaid jan-may among newly nonempty
    underpaid = 0
    for r in pay_rows:
        iso = r.get("date_of_service_iso") or ""
        if not (len(iso) >= 7 and "2026-01" <= iso[:7] <= "2026-05"):
            continue
        due = float(r.get("amount_due") or 0)
        paid = float(r.get("amount_paid") or 0)
        if due > paid + 0.009:
            underpaid += 1

    summary = {
        "sample_size": len(sample_keys),
        "still_empty": len(still_empty),
        "newly_nonempty": len(newly_nonempty),
        "reaudit_payment_rows": len(pay_rows),
        "reaudit_jan_may_underpaid_rows": underpaid,
        "newly_nonempty_keys": [
            {"facility_id": a, "patient_id": b, "case_id": c}
            for a, b, c in newly_nonempty
        ],
        "verdict": (
            "Sample re-audit found no previously-empty cases with payments"
            if not newly_nonempty
            else f"WARNING: {len(newly_nonempty)} previously-empty cases now have payment rows"
        ),
    }
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote={args.out}")


if __name__ == "__main__":
    main()
