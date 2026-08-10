#!/usr/bin/env python3
"""Sample 50 typo_edit1 name_key pairs for FP audit. Levenshtein stays OFF by default.

Produces a review pack with heuristic pre-labels. Human/product review fills final_label.
Verdict: enable Levenshtein only if FP rate among reviewed same-person-looking pairs is low.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--classified",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/name_key_mismatch_classified.csv"),
    )
    p.add_argument("--sample-n", type=int, default=50)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/typo_edit1_fp_audit_sample.csv"),
    )
    p.add_argument(
        "--report",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/typo_edit1_fp_audit_report.json"),
    )
    args = p.parse_args(argv)

    rows = [
        r
        for r in csv.DictReader(args.classified.open(encoding="utf-8-sig", newline=""))
        if (r.get("class") or "") == "typo_edit1"
    ]
    rng = random.Random(args.seed)
    sample = rows if len(rows) <= args.sample_n else rng.sample(rows, args.sample_n)

    out_rows: list[dict[str, str]] = []
    heuristic = {"likely_same_person_spelling": 0, "ambiguous_or_different": 0}
    for r in sample:
        a = (r.get("webpt_name_key") or "").strip()
        b = (r.get("best_eob_name_key") or "").strip()
        # Heuristic only: shared long prefix (≥6) + single edit elsewhere → likely spelling
        prefix = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                break
            prefix += 1
        likely = prefix >= 6 and abs(len(a) - len(b)) <= 1
        # Flag high-risk short surnames (SMITH/SMYTH pattern): short common prefix relative to length
        if prefix < 4 and min(len(a), len(b)) >= 8:
            likely = False
        label = "likely_same_person_spelling" if likely else "ambiguous_or_different"
        heuristic[label] += 1
        out_rows.append(
            {
                "emr_id": r.get("emr_id") or "",
                "dos": r.get("dos") or "",
                "patient": r.get("patient") or "",
                "webpt_name": r.get("webpt_name") or "",
                "webpt_name_key": a,
                "best_eob_name_key": b,
                "heuristic_label": label,
                "final_label": "",  # fill: same_person | different_person | unclear
                "reviewer_notes": "",
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    report = {
        "population_typo_edit1": len(rows),
        "sample_n": len(out_rows),
        "seed": args.seed,
        "heuristic_counts": heuristic,
        "levenshtein_default": "OFF",
        "recommendation": (
            "KEEP_LEVENSHTEIN_OFF — complete final_label on sample; enable only if "
            "different_person rate among reviewed is near-zero and product accepts residual FP risk."
        ),
        "sample_path": str(args.out),
        "note": (
            "Heuristic is NOT a substitute for clinical identity review. "
            "Do not enable CASHFLOW_NAME_MATCH_LEVENSHTEIN from counts alone."
        ),
    }
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Wrote {args.out}", file=sys.stderr)
    print(f"Wrote {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
