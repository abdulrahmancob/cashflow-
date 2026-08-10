#!/usr/bin/env python3
"""Audit tracker_excluded true-gap checks: intentional vs indexing/normalization holes."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import name_key_from_webpt, parse_date  # noqa: E402

try:
    from cashflow_reconcile.payer_registry import extract_eft_refs_from_description
except Exception:  # pragma: no cover
    def extract_eft_refs_from_description(desc: str) -> list[str]:
        return []


def _norm_check(value: str) -> str:
    text = (value or "").strip().upper()
    text = re.sub(r"[\s\-]", "", text)
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _collect_tracker_excluded(
    gap_rows: list[dict[str, str]],
    recon_lines: list[dict[str, str]],
    eob_rows: list[dict[str, str]],
    tracked_refs: set[str],
) -> list[dict[str, str]]:
    """Re-derive tracker_excluded visits (same priority gate as analyzer after name)."""
    lines_by: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in recon_lines:
        emr = (row.get("webpt_patient_id") or "").strip()
        dos = (parse_date(row.get("date_of_service")) or date.min).isoformat()
        if emr and dos != date.min.isoformat():
            lines_by[(emr, dos)].append(row)

    eob_by_check: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in eob_rows:
        chk = _norm_check(row.get("check_eft_num") or "")
        if chk:
            eob_by_check[chk].append(row)

    out: list[dict[str, str]] = []
    if not tracked_refs:
        return out
    for gap in gap_rows:
        emr = (gap.get("emr_id") or "").strip()
        dos = (gap.get("dos") or "").strip()
        our = lines_by.get((emr, dos), [])
        webpt_nk = name_key_from_webpt(our[0].get("patient_name") or "") if our else ""
        checks = [
            _norm_check(c)
            for c in (gap.get("sf_checks") or gap.get("sf_check") or "").split(";")
            if _norm_check(c)
        ]
        eob_same = []
        for c in checks:
            for e in eob_by_check.get(c, []):
                if (parse_date(e.get("date_of_service")) or date.min).isoformat() == dos:
                    eob_same.append(e)
        eob_nks = {
            (e.get("name_key") or "").strip().upper()
            for e in eob_same
            if (e.get("name_key") or "").strip()
        }
        if webpt_nk and eob_nks and webpt_nk not in eob_nks:
            continue  # name_key_mismatch takes priority
        if checks and not any(c in tracked_refs for c in checks):
            out.append({**gap, "norm_checks": ";".join(checks)})
    return out


def audit_checks(
    excluded: list[dict[str, str]],
    *,
    deposit_refs: set[str],
    deposit_norm: set[str],
    tracker_rich_refs: set[str],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in excluded:
        checks = [c for c in (row.get("norm_checks") or "").split(";") if c]
        verdict = "intentional_absent"
        detail = ""
        for c in checks:
            if c in deposit_refs:
                verdict = "in_deposit_exact"
                detail = c
                break
            if c in deposit_norm or _norm_check(c) in deposit_norm:
                # already normed; try last4
                if len(c) >= 4 and c[-4:] in deposit_refs:
                    verdict = "in_deposit_last4_only"
                    detail = c
                    break
            if c in tracker_rich_refs:
                verdict = "in_tracker_rich_not_deposit_eft"
                detail = c
                break
            # normalization variant: zero-pad / case already handled; try without alnum
            for d in deposit_norm:
                if d.endswith(c) or c.endswith(d) and min(len(c), len(d)) >= 4:
                    if _norm_check(d) == c or d == c:
                        verdict = "norm_variant"
                        detail = f"{c}~{d}"
                        break
            if verdict == "norm_variant":
                break
            if len(c) >= 4 and c[-4:] in deposit_refs:
                verdict = "in_deposit_last4_only"
                detail = c
                break

        # refine: if in rich tracker
        if verdict == "intentional_absent":
            for c in checks:
                if c in tracker_rich_refs:
                    verdict = "in_tracker_rich_not_deposit_eft"
                    detail = c
                    break

        counts[verdict] += 1
        if len(samples[verdict]) < 15:
            samples[verdict].append(
                {
                    "emr_id": row.get("emr_id") or "",
                    "dos": row.get("dos") or "",
                    "patient": row.get("patient") or "",
                    "sf_check": row.get("sf_check") or "",
                    "norm_checks": row.get("norm_checks") or "",
                    "detail": detail,
                    "verdict": verdict,
                }
            )

    total = sum(counts.values()) or 1
    false_exclude = (
        counts.get("in_deposit_exact", 0)
        + counts.get("in_deposit_last4_only", 0)
        + counts.get("in_tracker_rich_not_deposit_eft", 0)
        + counts.get("norm_variant", 0)
    )
    return {
        "total": total,
        "by_verdict": dict(counts.most_common()),
        "by_verdict_pct": {k: round(100.0 * v / total, 1) for k, v in counts.most_common()},
        "false_exclude_est": false_exclude,
        "intentional_est": counts.get("intentional_absent", 0),
        "recommendation": (
            "widen_get_tracked_eft_refs"
            if counts.get("in_tracker_rich_not_deposit_eft", 0) or counts.get("norm_variant", 0)
            else (
                "fix_partition_last4_norm"
                if counts.get("in_deposit_last4_only", 0) or counts.get("in_deposit_exact", 0)
                else "no_product_change_intentional"
            )
        ),
        "samples": dict(samples),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gap-visits", type=Path, required=True)
    p.add_argument("--recon-lines", type=Path, required=True)
    p.add_argument("--eob-payments", type=Path, required=True)
    p.add_argument("--tracked-refs", type=Path, required=True)
    p.add_argument(
        "--tracker-rows",
        type=Path,
        default=None,
        help="Optional CSV with eft_1,eft_2,check_reference,description",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/tracker_excluded_audit.json"),
    )
    args = p.parse_args(argv)

    tracked: set[str] = set()
    for row in load_csv(args.tracked_refs):
        for k in ("eft_ref", "check_eft_num", "eft_1", "eft_2", "eft_last4"):
            if row.get(k):
                tracked.add(_norm_check(row[k]))
                tracked.add((row[k] or "").strip())

    deposit_refs = set(tracked)
    deposit_norm = {_norm_check(x) for x in tracked if x}

    rich: set[str] = set()
    if args.tracker_rows and args.tracker_rows.is_file():
        for row in load_csv(args.tracker_rows):
            for k in ("eft_1", "eft_2", "eft_last4", "check_reference"):
                if row.get(k):
                    rich.add(_norm_check(row[k]))
                    rich.add((row[k] or "").strip())
            for ref in extract_eft_refs_from_description(row.get("description") or ""):
                rich.add(_norm_check(ref))
                rich.add(ref.strip())

    excluded = _collect_tracker_excluded(
        load_csv(args.gap_visits),
        load_csv(args.recon_lines),
        load_csv(args.eob_payments),
        tracked_refs=deposit_norm | {_norm_check(x) for x in deposit_refs},
    )
    report = audit_checks(
        excluded,
        deposit_refs=deposit_refs,
        deposit_norm=deposit_norm,
        tracker_rich_refs=rich,
    )
    report["excluded_visits"] = len(excluded)
    report["tracked_ref_count"] = len(deposit_norm)
    report["rich_ref_count"] = len(rich)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    sample_path = args.out.with_name("tracker_excluded_audit_samples.csv")
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
                "excluded_visits": report["excluded_visits"],
                "by_verdict": report["by_verdict"],
                "recommendation": report["recommendation"],
                "false_exclude_est": report["false_exclude_est"],
                "intentional_est": report["intentional_est"],
            },
            indent=2,
        )
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
