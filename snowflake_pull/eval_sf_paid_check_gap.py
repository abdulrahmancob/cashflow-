#!/usr/bin/env python3
"""Evaluate SF paid+check visits vs our eob checks and reconcile status (DOS>=2026).

Join primary key: (EMR_ID / webpt_patient_id, DOS). Fallback: name_key.

Buckets:
- SF paid + check, check NOT in eob_check → not our problem
- SF paid + check in eob, our visit paid/partial → ok
- SF paid + check in eob, our visit pending → true match gap
- SF paid + check in eob, no recon visit for EMR+DOS → coverage gap (no WebPT visit in run)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import name_key_from_webpt, parse_date  # noqa: E402
from snowflake_pull.compare_visits import (  # noqa: E402
    name_key_from_snowflake_patient,
    normalize_status,
)

CHECK_COLS = (
    "PRIMARY_CHECK_NUMBER",
    "SECONDARY_CHECK_NUMBER",
    "THIRD_CHECK_NUMBER",
    "FOURTH_CHECK_NUMBER",
)


def _norm_check(value: str) -> str:
    text = (value or "").strip().upper()
    text = re.sub(r"[\s\-]", "", text)
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _check_keys(value: str) -> set[str]:
    """Full check only (no last4) to avoid false positives on short numbers."""
    raw = _norm_check(value)
    return {raw} if raw else set()


def load_our_checks(path: Path) -> set[str]:
    checks: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            checks |= _check_keys(row.get("check_eft_num") or "")
    return checks


def load_our_visits(path: Path) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    by_emr: dict[tuple[str, str], dict] = {}
    by_name: dict[tuple[str, str], dict] = {}
    rank = {"paid": 50, "partial": 40, "pending": 10}

    def better(a: dict | None, b: dict) -> dict:
        if a is None:
            return b
        return b if rank.get(b["visit_status"], 0) > rank.get(a["visit_status"], 0) else a

    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = parse_date(row.get("date_of_service"))
            if dos is None or dos < date(2026, 1, 1):
                continue
            st = (row.get("visit_status") or "pending").strip().lower() or "pending"
            rec = {
                "visit_status": st,
                "primary_check": row.get("primary_check_number") or "",
                "secondary_check": row.get("secondary_check_number") or "",
                "patient_name": row.get("patient_name") or "",
                "facility": row.get("facility_name") or "",
                "webpt_patient_id": (row.get("webpt_patient_id") or "").strip(),
                "paid_lines": row.get("paid_lines") or "0",
                "pending_lines": row.get("pending_lines") or "0",
            }
            emr = rec["webpt_patient_id"]
            if emr:
                key = (emr, dos.isoformat())
                by_emr[key] = better(by_emr.get(key), rec)
            nk = name_key_from_webpt(rec["patient_name"])
            if nk:
                key = (nk, dos.isoformat())
                by_name[key] = better(by_name.get(key), rec)
    return by_emr, by_name


def sf_row_checks(row: dict[str, str]) -> list[str]:
    found: list[str] = []
    for col in CHECK_COLS:
        v = (row.get(col) or "").strip()
        if v:
            found.append(v)
    return found


def _best_status(rows: list[dict[str, str]]) -> str:
    best = "pending"
    order = {
        "paid": 50,
        "partial": 40,
        "deduct": 30,
        "collection": 25,
        "denied": 20,
        "pending": 10,
    }
    for r in rows:
        st = normalize_status(r.get("STATUS") or "", source="snowflake")
        if order.get(st, 0) > order.get(best, 0):
            best = st
    return best


def evaluate(
    *,
    snowflake_csv: Path,
    our_checks_csv: Path,
    our_visits_csv: Path,
    sample_n: int = 25,
) -> dict[str, Any]:
    our_checks = load_our_checks(our_checks_csv)
    by_emr, by_name = load_our_visits(our_visits_csv)

    sf_buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with snowflake_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = parse_date(row.get("DATE_OF_SERVICE"))
            if dos is None or dos < date(2026, 1, 1):
                continue
            emr = (row.get("EMR_ID") or "").strip()
            if emr:
                sf_buckets[(emr, dos.isoformat())].append(row)
            else:
                nk = name_key_from_snowflake_patient(row.get("PATIENT") or "")
                if nk:
                    sf_buckets[(f"name:{nk}", dos.isoformat())].append(row)

    totals = {
        "sf_visit_keys": len(sf_buckets),
        "sf_paid_with_check": 0,
        "check_in_ours": 0,
        "check_not_in_ours": 0,
        "in_ours_our_paid_or_partial": 0,
        "in_ours_our_pending": 0,
        "in_ours_missing_visit": 0,
        "sf_pending_visits": 0,
        "sf_other_status_with_check": 0,
        "joined_via_emr": 0,
        "joined_via_name": 0,
        "coverage_sf_emr": 0,
        "coverage_sf_name_only": 0,
    }
    true_gap_samples: list[dict[str, str]] = []
    coverage_gap_samples: list[dict[str, str]] = []
    not_ours_samples: list[dict[str, str]] = []
    true_gap_rows: list[dict[str, str]] = []
    coverage_gap_rows: list[dict[str, str]] = []

    for (emr_or_name, dos), rows in sf_buckets.items():
        best = _best_status(rows)
        checks_raw: list[str] = []
        for r in rows:
            checks_raw.extend(sf_row_checks(r))
        seen: set[str] = set()
        checks: list[str] = []
        for c in checks_raw:
            n = _norm_check(c)
            if n and n not in seen:
                seen.add(n)
                checks.append(c.strip())

        if best == "pending" and not checks:
            totals["sf_pending_visits"] += 1
            continue
        if best != "paid" or not checks:
            if checks:
                totals["sf_other_status_with_check"] += 1
            continue

        totals["sf_paid_with_check"] += 1
        in_ours = False
        matched_check = ""
        for c in checks:
            if _check_keys(c) & our_checks:
                in_ours = True
                matched_check = c
                break

        patient = next((r.get("PATIENT") or "" for r in rows if (r.get("PATIENT") or "").strip()), "")
        clinic = next((r.get("CLINIC") or "" for r in rows if (r.get("CLINIC") or "").strip()), "")
        emr = emr_or_name[5:] if emr_or_name.startswith("name:") else emr_or_name

        if not in_ours:
            totals["check_not_in_ours"] += 1
            if len(not_ours_samples) < sample_n:
                not_ours_samples.append(
                    {
                        "patient": patient,
                        "emr_id": emr if not emr_or_name.startswith("name:") else "",
                        "dos": dos,
                        "clinic": clinic,
                        "sf_checks": ";".join(checks[:4]),
                        "bucket": "check_not_in_our_eob",
                    }
                )
            continue

        totals["check_in_ours"] += 1
        ours = None
        join_via = ""
        if not emr_or_name.startswith("name:"):
            ours = by_emr.get((emr, dos))
            if ours:
                join_via = "emr"
                totals["joined_via_emr"] += 1
        if ours is None:
            nk = name_key_from_snowflake_patient(patient)
            if nk:
                ours = by_name.get((nk, dos))
                if ours:
                    join_via = "name"
                    totals["joined_via_name"] += 1

        if ours is None:
            totals["in_ours_missing_visit"] += 1
            sf_id = "name_only" if emr_or_name.startswith("name:") else "emr"
            if sf_id == "emr":
                totals["coverage_sf_emr"] = totals.get("coverage_sf_emr", 0) + 1
            else:
                totals["coverage_sf_name_only"] = totals.get("coverage_sf_name_only", 0) + 1
            cov = {
                "patient": patient,
                "emr_id": emr if sf_id == "emr" else "",
                "dos": dos,
                "clinic": clinic,
                "sf_check": matched_check,
                "sf_checks": ";".join(checks[:6]),
                "join_via": sf_id,  # SF identity used (recon join failed)
                "bucket": "check_in_eob_no_recon_visit",
            }
            coverage_gap_rows.append(cov)
            if len(coverage_gap_samples) < sample_n:
                coverage_gap_samples.append(cov)
            continue

        st = ours["visit_status"]
        if st in {"paid", "partial"}:
            totals["in_ours_our_paid_or_partial"] += 1
        else:
            # pending or any non-paid status with check present in our EOB
            totals["in_ours_our_pending"] += 1
            gap = {
                "patient": patient,
                "emr_id": ours.get("webpt_patient_id") or emr,
                "dos": dos,
                "clinic": clinic,
                "sf_check": matched_check,
                "sf_checks": ";".join(checks[:6]),
                "our_status": st,
                "join_via": join_via,
                "bucket": "true_gap_pending_with_our_check",
            }
            true_gap_rows.append(gap)
            if len(true_gap_samples) < sample_n:
                true_gap_samples.append(gap)

    paid = max(totals["sf_paid_with_check"], 1)
    in_eob = max(totals["check_in_ours"], 1)
    true_gap = totals["in_ours_our_pending"]
    coverage_gap = totals["in_ours_missing_visit"]
    report = {
        "terms": {
            "identity_attachment_gap": (
                "EOB exists for same DOS+CPT but matcher did not assign it to the WebPT "
                "line — name_key / 1:1 consumption / modifier, not missing files."
            ),
            "collision": (
                "Two+ WebPT patients share printed name + DOS; matcher blocks payments "
                "until scoped. Prior measure: 21 keys / 61 lines — negligible."
            ),
            "scope": "Pre-2026 DOS excluded (filter only; no production DELETE).",
        },
        "inputs": {
            "snowflake_csv": str(snowflake_csv),
            "our_checks_csv": str(our_checks_csv),
            "our_visits_csv": str(our_visits_csv),
            "our_check_keys": len(our_checks),
            "our_visit_keys_emr": len(by_emr),
            "our_visit_keys_name": len(by_name),
            "sf_rows_file": "billing_2026-01-01_to_now.csv",
        },
        "totals": totals,
        "rates": {
            "pct_paid_check_in_our_eob": round(100.0 * totals["check_in_ours"] / paid, 1),
            "pct_paid_check_not_in_our_eob": round(100.0 * totals["check_not_in_ours"] / paid, 1),
            "pct_ok_among_in_eob_with_visit": round(
                100.0
                * totals["in_ours_our_paid_or_partial"]
                / max(totals["in_ours_our_paid_or_partial"] + true_gap, 1),
                1,
            ),
            "pct_true_pending_gap_among_joined": round(
                100.0 * true_gap / max(totals["in_ours_our_paid_or_partial"] + true_gap, 1),
                1,
            ),
            "pct_coverage_gap_of_in_eob": round(100.0 * coverage_gap / in_eob, 1),
        },
        "true_gap_samples": true_gap_samples,
        "coverage_gap_samples": coverage_gap_samples,
        "not_ours_samples": not_ours_samples,
        "true_gap_rows": true_gap_rows,
        "coverage_gap_rows": coverage_gap_rows,
        "verdict": {
            "not_our_problem_check_absent": totals["check_not_in_ours"],
            "true_match_gap_pending_with_check_in_eob": true_gap,
            "coverage_gap_check_in_eob_no_recon_visit": coverage_gap,
            "system_ok_paid_partial": totals["in_ours_our_paid_or_partial"],
        },
        "join_metrics": {
            "joined_via_emr": totals["joined_via_emr"],
            "joined_via_name": totals["joined_via_name"],
            "true_gap_via_emr": sum(1 for r in true_gap_rows if r.get("join_via") == "emr"),
            "true_gap_via_name": sum(1 for r in true_gap_rows if r.get("join_via") == "name"),
            "coverage_sf_emr": totals["coverage_sf_emr"],
            "coverage_sf_name_only": totals["coverage_sf_name_only"],
            "join_failed": coverage_gap,
        },
    }
    return report


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snowflake", type=Path, required=True)
    p.add_argument("--our-checks", type=Path, required=True)
    p.add_argument("--our-visits", type=Path, required=True)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/sf_paid_check_gap_report.json"),
    )
    p.add_argument("--sample", type=int, default=25)
    p.add_argument(
        "--write-kpi",
        action="store_true",
        help="Append true_match_gap KPI line to true_match_gap_kpi.jsonl",
    )
    p.add_argument("--run-id", default="aae72074-5b88-438e-b238-960dd08208a3")
    args = p.parse_args(argv)
    report = evaluate(
        snowflake_csv=args.snowflake,
        our_checks_csv=args.our_checks,
        our_visits_csv=args.our_visits,
        sample_n=args.sample,
    )
    out_dir = args.out.parent
    args.out.parent.mkdir(parents=True, exist_ok=True)
    true_rows = report.pop("true_gap_rows")
    cov_rows = report.pop("coverage_gap_rows")
    _write_csv(out_dir / "true_gap_visits.csv", true_rows)
    _write_csv(out_dir / "coverage_gap_visits.csv", cov_rows)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.write_kpi:
        from datetime import datetime, timezone

        gap_by_reason: dict[str, Any] | None = None
        coverage_buckets: dict[str, Any] | None = None
        reason_path = out_dir / "true_match_gap_by_reason.json"
        cov_path = out_dir / "coverage_gap_by_bucket.json"
        lines_csv = out_dir / "our_lines_2026.csv"
        eob_csv = out_dir / "eob_payments_2026.csv"
        tracked_csv = out_dir / "tracked_refs.csv"
        if lines_csv.is_file() and eob_csv.is_file():
            from snowflake_pull.analyze_true_match_gap import classify as classify_gap
            from snowflake_pull.analyze_true_match_gap import load_csv as _load
            from snowflake_pull.analyze_true_match_gap import _norm_check as _nc

            tracked: set[str] = set()
            if tracked_csv.is_file():
                for row in _load(tracked_csv):
                    for k in ("eft_ref", "check_eft_num", "eft_1", "eft_2", "eft_last4"):
                        if row.get(k):
                            tracked.add(_nc(row[k]))
            gap_report = classify_gap(
                gap_rows=true_rows,
                recon_lines=_load(lines_csv),
                eob_rows=_load(eob_csv),
                tracked_refs=tracked,
            )
            reason_path.write_text(json.dumps(gap_report, indent=2, default=str), encoding="utf-8")
            gap_by_reason = {
                "by_reason": gap_report["by_reason"],
                "by_reason_pct": gap_report["by_reason_pct"],
                "total": gap_report["total"],
            }
            print(f"Wrote {reason_path}", file=sys.stderr)

        clin_v = out_dir / "clinical_visits_2026.csv"
        clin_n = out_dir / "clinical_notes_2026.csv"
        sched = out_dir / "schedule_2026.csv"
        pats = out_dir / "patients_emr.csv"
        if all(p.is_file() for p in (clin_v, clin_n, sched, pats)):
            from snowflake_pull.analyze_coverage_gap import classify as classify_cov
            from snowflake_pull.analyze_coverage_gap import load_csv as _load_c
            from snowflake_pull.analyze_coverage_gap import load_emr_set, load_keyset

            cov_report = classify_cov(
                cov_rows,
                visit_keys=load_keyset(clin_v, "emr_id", "dos"),
                note_keys=load_keyset(clin_n, "emr_id", "dos"),
                schedule_keys=load_keyset(sched, "emr_id", "dos"),
                known_emrs=load_emr_set(pats, "webpt_patient_id"),
            )
            # SF identity on coverage cohort (recon join failed by definition)
            via = {"sf_emr": 0, "sf_name_only": 0}
            for row in cov_rows:
                j = (row.get("join_via") or "").strip().lower()
                if j == "emr":
                    via["sf_emr"] += 1
                else:
                    via["sf_name_only"] += 1
            cov_report["sf_identity"] = via
            cov_path.write_text(json.dumps(cov_report, indent=2), encoding="utf-8")
            coverage_buckets = {
                "by_bucket": cov_report["by_bucket"],
                "summary": cov_report["summary"],
                "sf_identity": via,
                "total": cov_report["total"],
            }
            print(f"Wrote {cov_path}", file=sys.stderr)

        kpi_path = out_dir / "true_match_gap_kpi.jsonl"
        line = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "true_match_gap": report["verdict"]["true_match_gap_pending_with_check_in_eob"],
            "ok_paid_partial": report["verdict"]["system_ok_paid_partial"],
            "coverage_gap": report["verdict"]["coverage_gap_check_in_eob_no_recon_visit"],
            "check_not_in_eob": report["verdict"]["not_our_problem_check_absent"],
            "join_metrics": report.get("join_metrics"),
            "rates": report.get("rates"),
            "gap_by_reason": gap_by_reason,
            "coverage_buckets": coverage_buckets,
        }
        with kpi_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        print(f"Appended KPI -> {kpi_path}", file=sys.stderr)

    print(json.dumps({k: report[k] for k in ("totals", "rates", "verdict", "join_metrics", "terms")}, indent=2))
    print(f"Wrote {args.out}", file=sys.stderr)
    print(f"Wrote {out_dir / 'true_gap_visits.csv'} ({len(true_rows)})", file=sys.stderr)
    print(f"Wrote {out_dir / 'coverage_gap_visits.csv'} ({len(cov_rows)})", file=sys.stderr)
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
