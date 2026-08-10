#!/usr/bin/env python3
"""Classify true_match_gap visits by failure reason using exported CSVs.

Priority (first match wins):
  name_key_mismatch > tracker_excluded > zero_pay_rollup > modifier_block
  > cpt_mismatch > payment_consumed > collision_scope > no_candidate_other

zero_pay_rollup: visit_status=pending because paid_lines=0, but all recon lines
are zero_pay / non-pending (matcher attached $0 EOB) — not an identity miss.
"""
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


def _norm_check(value: str) -> str:
    text = (value or "").strip().upper()
    text = re.sub(r"[\s\-]", "", text)
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _norm_cpt(value: str) -> str:
    return re.sub(r"\D", "", (value or "").strip())


def _norm_mod(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().upper())


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def classify(
    *,
    gap_rows: list[dict[str, str]],
    recon_lines: list[dict[str, str]],
    eob_rows: list[dict[str, str]],
    tracked_refs: set[str],
) -> dict[str, Any]:
    # Index recon lines by emr+dos
    lines_by_emr_dos: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    matched_keys: set[tuple[str, str, str, str]] = set()  # name, dos, cpt, mod used
    matched_no_mod: set[tuple[str, str, str]] = set()
    name_dos_patients: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in recon_lines:
        emr = (row.get("webpt_patient_id") or "").strip()
        dos = (parse_date(row.get("date_of_service")) or date.min).isoformat()
        if not emr or dos == date.min.isoformat():
            continue
        lines_by_emr_dos[(emr, dos)].append(row)
        nk = name_key_from_webpt(row.get("patient_name") or "")
        if nk:
            name_dos_patients[(nk, dos)].add(emr)
        st = (row.get("status") or "").lower()
        if st != "pending" and (row.get("check_eft_num") or "").strip():
            cpt = _norm_cpt(row.get("cpt_code") or "")
            mod = _norm_mod(row.get("modifier") or "")
            if nk and cpt:
                matched_keys.add((nk, dos, cpt, mod))
                matched_no_mod.add((nk, dos, cpt))

    # Index EOB by check and by (name_key, dos)
    eob_by_check: dict[str, list[dict[str, str]]] = defaultdict(list)
    eob_by_name_dos: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in eob_rows:
        chk = _norm_check(row.get("check_eft_num") or "")
        dos = (parse_date(row.get("date_of_service")) or date.min).isoformat()
        nk = (row.get("name_key") or "").strip().upper()
        if chk:
            eob_by_check[chk].append(row)
        if nk and dos != date.min.isoformat():
            eob_by_name_dos[(nk, dos)].append(row)

    reason_counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    classified: list[dict[str, Any]] = []

    for gap in gap_rows:
        emr = (gap.get("emr_id") or "").strip()
        dos = (gap.get("dos") or "").strip()
        our_lines = lines_by_emr_dos.get((emr, dos), [])
        webpt_nk = ""
        if our_lines:
            webpt_nk = name_key_from_webpt(our_lines[0].get("patient_name") or "")
        pending_cpts = {
            _norm_cpt(r.get("cpt_code") or "")
            for r in our_lines
            if (r.get("status") or "").lower() == "pending" and _norm_cpt(r.get("cpt_code") or "")
        }
        pending_full = {
            (_norm_cpt(r.get("cpt_code") or ""), _norm_mod(r.get("modifier") or ""))
            for r in our_lines
            if (r.get("status") or "").lower() == "pending"
        }

        checks = [
            _norm_check(c)
            for c in (gap.get("sf_checks") or gap.get("sf_check") or "").split(";")
            if _norm_check(c)
        ]
        eob_for_visit: list[dict[str, str]] = []
        for c in checks:
            eob_for_visit.extend(eob_by_check.get(c, []))
        # Prefer same DOS
        eob_same_dos = [
            e
            for e in eob_for_visit
            if (parse_date(e.get("date_of_service")) or date.min).isoformat() == dos
        ]
        if not eob_same_dos:
            eob_same_dos = eob_for_visit

        flags: dict[str, Any] = {
            "pending_line_count": len([r for r in our_lines if (r.get("status") or "").lower() == "pending"]),
            "eob_same_dos_count": len(eob_same_dos),
            "webpt_name_key": webpt_nk,
        }

        reason = "no_candidate_other"
        line_statuses = Counter(
            (r.get("status") or "").strip().lower() for r in our_lines if (r.get("status") or "").strip()
        )
        flags["line_statuses"] = dict(line_statuses)

        # 1) name_key_mismatch
        eob_nks = {(e.get("name_key") or "").strip().upper() for e in eob_same_dos if (e.get("name_key") or "").strip()}
        if webpt_nk and eob_nks and webpt_nk not in eob_nks:
            reason = "name_key_mismatch"
            flags["eob_name_keys"] = sorted(eob_nks)[:5]
        else:
            # 2) tracker_excluded — all SF checks absent from tracked_refs when tracked set non-empty
            # Mirror db_io._partition_by_tracker: exact ref OR last4 hit counts as tracked.
            def _check_tracked(c: str) -> bool:
                if c in tracked_refs:
                    return True
                if len(c) >= 4 and c[-4:] in tracked_refs:
                    return True
                return False

            if tracked_refs and checks and not any(_check_tracked(c) for c in checks):
                reason = "tracker_excluded"
            # 2b) zero_pay_rollup — visit pending with no pending lines (all $0 / PR / etc.)
            elif flags["pending_line_count"] == 0 and (
                line_statuses.get("zero_pay", 0) > 0
                or line_statuses.get("patient_responsibility", 0) > 0
                or line_statuses.get("secondary_pending", 0) > 0
            ):
                reason = "zero_pay_rollup"
            else:
                eob_cpts = {_norm_cpt(e.get("cpt_code") or "") for e in eob_same_dos}
                eob_full = {
                    (_norm_cpt(e.get("cpt_code") or ""), _norm_mod(e.get("modifiers") or e.get("modifier") or ""))
                    for e in eob_same_dos
                }
                overlap_cpt = pending_cpts & eob_cpts
                # 3) modifier_block: CPT overlaps but no exact modifier pair
                if overlap_cpt and not (pending_full & eob_full):
                    reason = "modifier_block"
                elif pending_cpts and eob_cpts and not overlap_cpt:
                    # 4) cpt_mismatch
                    reason = "cpt_mismatch"
                elif overlap_cpt and webpt_nk:
                    # 5) payment_consumed: matching no-mod key already used by a matched line
                    consumed = any((webpt_nk, dos, cpt) in matched_no_mod for cpt in overlap_cpt)
                    if consumed:
                        reason = "payment_consumed"
                    elif webpt_nk and len(name_dos_patients.get((webpt_nk, dos), set())) >= 2:
                        reason = "collision_scope"
                    else:
                        pos_pay = [
                            e
                            for e in eob_same_dos
                            if _norm_cpt(e.get("cpt_code") or "") in overlap_cpt
                            and float(e.get("paid_amount") or 0) > 0
                        ]
                        if not pos_pay:
                            reason = "cpt_mismatch"
                            flags["zero_pay_only"] = True
                        else:
                            reason = "payment_consumed"
                            flags["positive_pay_candidates"] = len(pos_pay)
                elif webpt_nk and len(name_dos_patients.get((webpt_nk, dos), set())) >= 2:
                    reason = "collision_scope"
                elif not eob_same_dos:
                    reason = "no_candidate_other"
                    flags["note"] = "no_eob_rows_for_sf_checks_on_dos"
                elif flags["pending_line_count"] == 0 and not our_lines:
                    reason = "no_candidate_other"
                    flags["note"] = "no_recon_lines_for_emr_dos"
                else:
                    reason = "no_candidate_other"

        reason_counts[reason] += 1
        row_out = {**gap, "reason": reason, "flags": flags}
        classified.append(row_out)
        if len(samples[reason]) < 15:
            samples[reason].append(row_out)

    total = sum(reason_counts.values()) or 1
    return {
        "total": total,
        "by_reason": dict(reason_counts.most_common()),
        "by_reason_pct": {
            k: round(100.0 * v / total, 1) for k, v in reason_counts.most_common()
        },
        "samples": {k: v for k, v in samples.items()},
        "classified_count": len(classified),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gap-visits", type=Path, required=True)
    p.add_argument("--recon-lines", type=Path, required=True)
    p.add_argument("--eob-payments", type=Path, required=True)
    p.add_argument("--tracked-refs", type=Path, default=None)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/true_match_gap_by_reason.json"),
    )
    args = p.parse_args(argv)

    gap_rows = load_csv(args.gap_visits)
    recon_lines = load_csv(args.recon_lines)
    eob_rows = load_csv(args.eob_payments)
    tracked: set[str] = set()
    if args.tracked_refs and args.tracked_refs.is_file():
        for row in load_csv(args.tracked_refs):
            for k in ("eft_ref", "check_eft_num", "eft_1", "eft_2", "eft_last4"):
                if row.get(k):
                    tracked.add(_norm_check(row[k]))

    report = classify(
        gap_rows=gap_rows,
        recon_lines=recon_lines,
        eob_rows=eob_rows,
        tracked_refs=tracked,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # samples CSV
    sample_path = args.out.with_name("true_match_gap_reason_samples.csv")
    flat: list[dict[str, str]] = []
    for reason, rows in report["samples"].items():
        for r in rows:
            flat.append(
                {
                    "reason": reason,
                    "emr_id": r.get("emr_id", ""),
                    "dos": r.get("dos", ""),
                    "patient": r.get("patient", ""),
                    "sf_check": r.get("sf_check", ""),
                    "join_via": r.get("join_via", ""),
                    "flags": json.dumps(r.get("flags") or {}),
                }
            )
    if flat:
        with sample_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)

    print(json.dumps({"by_reason": report["by_reason"], "by_reason_pct": report["by_reason_pct"], "total": report["total"]}, indent=2))
    print(f"Wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
