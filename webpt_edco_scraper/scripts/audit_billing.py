"""Audit WebPT extracted daily notes for CPT-vs-insurance and ICD denial conflicts.

Usage:
  python scripts/audit_billing.py \\
    --extracted output/jun_jul_2026/extracted \\
    --out output/jun_jul_2026/audit
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook

AUDIT_DIR = Path(__file__).resolve().parent.parent / "audit"
DEFAULT_CPT_RULES = AUDIT_DIR / "cpt_insurance_rules.yaml"
DEFAULT_ICD_RULES = AUDIT_DIR / "icd_denial_rules.yaml"


@dataclass
class NoteRecord:
    daily_note_id: str
    patient_id: str = ""
    patient_name: str = ""
    date_of_daily_note: str = ""
    facility_name: str = ""
    insurance_name: str = ""
    visit_no: str = ""
    note_file: str = ""
    diagnosis_icd_codes: str = ""
    cpt_codes: set[str] = field(default_factory=set)
    cpt_details: list[dict[str, str]] = field(default_factory=list)


def _parse_icd_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip().upper() for c in raw.replace(",", ";").split(";") if c.strip()]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def compile_cpt_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for rule in config.get("rules") or []:
        patterns = [
            re.compile(p, re.IGNORECASE) for p in (rule.get("match_patterns") or [])
        ]
        compiled.append({**rule, "_patterns": patterns})
    return compiled


def match_insurance(
    insurance_name: str, rules: list[dict[str, Any]]
) -> dict[str, Any] | None:
    text = (insurance_name or "").strip()
    if not text:
        return None
    for rule in rules:
        for pat in rule["_patterns"]:
            if pat.search(text):
                return rule
    return None


def load_notes(extracted: Path) -> dict[str, NoteRecord]:
    notes: dict[str, NoteRecord] = {}

    daily_path = extracted / "daily_notes.csv"
    if daily_path.exists():
        with daily_path.open(encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                nid = (row.get("daily_note_id") or "").strip()
                if not nid:
                    continue
                notes[nid] = NoteRecord(
                    daily_note_id=nid,
                    patient_id=(row.get("patient_id") or "").strip(),
                    patient_name=(row.get("patient_name") or "").strip(),
                    date_of_daily_note=(row.get("date_of_daily_note") or "").strip(),
                    facility_name=(row.get("facility_name") or "").strip(),
                    insurance_name=(row.get("insurance_name") or "").strip(),
                    visit_no=(row.get("visit_no") or "").strip(),
                    note_file=(row.get("note_file") or "").strip(),
                    diagnosis_icd_codes=(row.get("diagnosis_icd_codes") or "").strip(),
                )

    cpt_path = extracted / "cpt_codes.csv"
    with cpt_path.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            nid = (row.get("daily_note_id") or "").strip()
            if not nid:
                continue
            if nid not in notes:
                notes[nid] = NoteRecord(
                    daily_note_id=nid,
                    patient_id=(row.get("patient_id") or "").strip(),
                    patient_name=(row.get("patient_name") or "").strip(),
                    date_of_daily_note=(row.get("date_of_daily_note") or "").strip(),
                    insurance_name=(row.get("insurance_name") or "").strip(),
                    visit_no=(row.get("visit_no") or "").strip(),
                    note_file=(row.get("note_file") or "").strip(),
                    diagnosis_icd_codes=(row.get("diagnosis_icd_codes") or "").strip(),
                )
            note = notes[nid]
            if not note.insurance_name:
                note.insurance_name = (row.get("insurance_name") or "").strip()
            if not note.diagnosis_icd_codes:
                note.diagnosis_icd_codes = (row.get("diagnosis_icd_codes") or "").strip()
            cpt = (row.get("cpt_code") or "").strip().upper()
            if cpt:
                note.cpt_codes.add(cpt)
                note.cpt_details.append(
                    {
                        "cpt_code": cpt,
                        "units": (row.get("units") or "").strip(),
                        "description": (row.get("description") or "").strip(),
                        "modifier": (row.get("modifier") or "").strip(),
                    }
                )
    return notes


def _condition_matches(codes: set[str], cond: dict[str, Any]) -> bool:
    if "exact" in cond:
        return any(c in codes for c in cond["exact"])
    if "any_prefix" in cond:
        prefixes = cond["any_prefix"]
        return any(any(c.startswith(p) for p in prefixes) for c in codes)
    if "any_of_groups" in cond:
        return any(_condition_matches(codes, g) for g in cond["any_of_groups"])
    return False


def _matching_codes(codes: set[str], cond: dict[str, Any]) -> list[str]:
    found: list[str] = []
    if "exact" in cond:
        found.extend(c for c in cond["exact"] if c in codes)
    if "any_prefix" in cond:
        prefixes = cond["any_prefix"]
        found.extend(c for c in sorted(codes) if any(c.startswith(p) for p in prefixes))
    if "any_of_groups" in cond:
        for g in cond["any_of_groups"]:
            found.extend(_matching_codes(codes, g))
    return found


# Laterality families from Denial Matrix examples (joint pain M25.5xx, etc.).
# Require 3+ chars after decimal so M54.51/M54.59 (LBP subtypes) are not treated as sides.
_LATERALITY_CODE = re.compile(r"^[A-Z]\d{2}\.\d{2,}[129]$")


def check_lateralization(codes: set[str]) -> list[str] | None:
    """Return conflicting codes if unspecified (...9) coexists with side-specific (...1/...2)."""
    by_stem: dict[str, set[str]] = defaultdict(set)
    for code in codes:
        if not _LATERALITY_CODE.match(code):
            continue
        stem = code[:-1]
        by_stem[stem].add(code)
    conflicts: list[str] = []
    for _stem, group in by_stem.items():
        ends = {c[-1] for c in group}
        if "9" in ends and (("1" in ends) or ("2" in ends)):
            conflicts.extend(sorted(group))
    return conflicts or None


def audit_icd(
    note: NoteRecord, icd_rules: list[dict[str, Any]]
) -> list[dict[str, str]]:
    codes = set(_parse_icd_list(note.diagnosis_icd_codes))
    if not codes:
        return []
    violations: list[dict[str, str]] = []
    for rule in icd_rules:
        conflicting: list[str] = []
        if rule.get("check") == "lateralization_unspecified":
            hit = check_lateralization(codes)
            if not hit:
                continue
            conflicting = hit
        else:
            conditions = rule.get("all_of") or []
            if not conditions:
                continue
            if not all(_condition_matches(codes, cond) for cond in conditions):
                continue
            for cond in conditions:
                conflicting.extend(_matching_codes(codes, cond))
            # de-dupe preserve order
            seen: set[str] = set()
            ordered: list[str] = []
            for c in conflicting:
                if c not in seen:
                    seen.add(c)
                    ordered.append(c)
            conflicting = ordered

        violations.append(
            {
                "daily_note_id": note.daily_note_id,
                "patient_id": note.patient_id,
                "patient_name": note.patient_name,
                "date_of_daily_note": note.date_of_daily_note,
                "facility_name": note.facility_name,
                "insurance_name": note.insurance_name,
                "visit_no": note.visit_no,
                "note_file": note.note_file,
                "diagnosis_icd_codes": note.diagnosis_icd_codes,
                "rule_id": rule["id"],
                "category": rule.get("category") or "",
                "severity": rule.get("severity") or "error",
                "conflicting_codes": "; ".join(conflicting),
                "description": rule.get("description") or "",
                "correct_approach": rule.get("correct_approach") or "",
            }
        )
    return violations


def audit_cpt(
    note: NoteRecord,
    rule: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, str]]:
    cpts = {c.upper() for c in note.cpt_codes}
    if not cpts:
        return []

    strapping_codes = {c.upper() for c in (config.get("strapping_codes") or [])}
    timed_codes = {c.upper() for c in (config.get("timed_treatment_codes") or [])}
    eval_codes = {c.upper() for c in (config.get("eval_codes") or [])}
    reeval_code = (config.get("reeval_code") or "97164").upper()
    massage_code = (config.get("massage_code") or "97124").upper()
    ta_code = (config.get("therapeutic_activity_code") or "97530").upper()
    estim_97014 = (config.get("estim_97014") or "97014").upper()
    estim_g0283 = (config.get("estim_g0283") or "G0283").upper()

    has_strap = bool(cpts & strapping_codes)
    has_timed = bool(cpts & timed_codes)
    has_eval_only = bool(cpts & eval_codes) and not has_timed and reeval_code not in cpts
    strap_present = sorted(cpts & strapping_codes)

    violations: list[dict[str, str]] = []

    def add(
        rule_id: str,
        severity: str,
        detail: str,
        *,
        expected: str = "",
        found: str = "",
    ) -> None:
        violations.append(
            {
                "daily_note_id": note.daily_note_id,
                "patient_id": note.patient_id,
                "patient_name": note.patient_name,
                "date_of_daily_note": note.date_of_daily_note,
                "facility_name": note.facility_name,
                "insurance_name": note.insurance_name,
                "matched_rule": rule.get("name") or "",
                "visit_no": note.visit_no,
                "note_file": note.note_file,
                "diagnosis_icd_codes": note.diagnosis_icd_codes,
                "cpt_codes": "; ".join(sorted(cpts)),
                "rule_id": rule_id,
                "severity": severity,
                "expected": expected,
                "found": found,
                "detail": detail,
            }
        )

    expected_estim = (rule.get("estim") or "").upper()
    if expected_estim == estim_g0283 and estim_97014 in cpts:
        add(
            "estim_mismatch",
            "error",
            f"Insurance rule requires G-Code e-stim ({estim_g0283}) but note has {estim_97014}.",
            expected=estim_g0283,
            found=estim_97014,
        )
    elif expected_estim == estim_97014 and estim_g0283 in cpts:
        add(
            "estim_mismatch",
            "error",
            f"Insurance rule requires {estim_97014} but note has G-Code ({estim_g0283}).",
            expected=estim_97014,
            found=estim_g0283,
        )

    strapping_policy = (rule.get("strapping") or "accepted").lower()
    if strapping_policy == "forbidden" and has_strap:
        add(
            "strapping_forbidden",
            "error",
            f"Strapping is not allowed for this insurance but found: {', '.join(strap_present)}.",
            expected="no strapping",
            found="; ".join(strap_present),
        )
    elif strapping_policy == "required" and not has_strap:
        if has_timed:
            add(
                "strapping_required_missing",
                "error",
                "Strapping is required for this insurance on treatment visits, but no strapping CPT was billed.",
                expected="strapping CPT",
                found="none",
            )
        elif has_eval_only:
            add(
                "strapping_required_missing",
                "warning",
                "Strapping is required for this insurance; eval-only visit has no strapping (confirm if initial note needs it).",
                expected="strapping CPT",
                found="none (eval-only)",
            )
        else:
            add(
                "strapping_required_missing",
                "warning",
                "Strapping is required for this insurance but no strapping CPT was billed.",
                expected="strapping CPT",
                found="none",
            )

    reeval_policy = (rule.get("reeval") or "allowed").lower()
    if reeval_policy == "forbidden" and reeval_code in cpts:
        add(
            "reeval_forbidden",
            "error",
            f"Re-eval code {reeval_code} is not allowed for this insurance.",
            expected="no re-eval",
            found=reeval_code,
        )

    if rule.get("prefer_manual_over_massage") and massage_code in cpts:
        add(
            "massage_instead_of_manual",
            "error",
            f"Massage ({massage_code}) billed; guide requires Manual Therapy instead of massage for this insurance.",
            expected="97140 (manual)",
            found=massage_code,
        )

    if (rule.get("timed_codes") or "") == "therapeutic_activities":
        if has_timed and ta_code not in cpts:
            add(
                "missing_therapeutic_activity",
                "error",
                f"Insurance expects Therapeutic Activities ({ta_code}) among timed codes, but it is missing.",
                expected=ta_code,
                found="; ".join(sorted(cpts & timed_codes)) or "none",
            )

    return violations


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_excel(
    path: Path,
    summary_rows: list[dict[str, str]],
    cpt_rows: list[dict[str, str]],
    icd_rows: list[dict[str, str]],
    unmapped_rows: list[dict[str, str]],
) -> None:
    wb = Workbook()
    sheets = {
        "summary": (summary_rows, ["section", "rule_id", "severity", "count", "notes"]),
        "cpt_violations": (
            cpt_rows,
            [
                "daily_note_id",
                "patient_id",
                "patient_name",
                "date_of_daily_note",
                "facility_name",
                "insurance_name",
                "matched_rule",
                "visit_no",
                "note_file",
                "diagnosis_icd_codes",
                "cpt_codes",
                "rule_id",
                "severity",
                "expected",
                "found",
                "detail",
            ],
        ),
        "icd_violations": (
            icd_rows,
            [
                "daily_note_id",
                "patient_id",
                "patient_name",
                "date_of_daily_note",
                "facility_name",
                "insurance_name",
                "visit_no",
                "note_file",
                "diagnosis_icd_codes",
                "rule_id",
                "category",
                "severity",
                "conflicting_codes",
                "description",
                "correct_approach",
            ],
        ),
        "unmapped_insurance": (
            unmapped_rows,
            ["insurance_name", "note_count", "example_patient", "example_note_id"],
        ),
    }

    first = True
    for name, (rows, cols) in sheets.items():
        ws = wb.active if first else wb.create_sheet(name)
        if first:
            ws.title = name
            first = False
        ws.append(cols)
        for row in rows:
            ws.append([row.get(c, "") for c in cols])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def run_audit(
    extracted: Path,
    out_dir: Path,
    cpt_rules_path: Path,
    icd_rules_path: Path,
) -> dict[str, Any]:
    cpt_config = load_yaml(cpt_rules_path)
    icd_config = load_yaml(icd_rules_path)
    cpt_rules = compile_cpt_rules(cpt_config)
    icd_rules = list(icd_config.get("rules") or [])

    notes = load_notes(extracted)
    cpt_violations: list[dict[str, str]] = []
    icd_violations: list[dict[str, str]] = []
    unmapped_counter: Counter[str] = Counter()
    unmapped_examples: dict[str, tuple[str, str]] = {}
    mapped_notes = 0
    unmapped_notes = 0
    empty_insurance = 0

    for note in notes.values():
        icd_violations.extend(audit_icd(note, icd_rules))

        ins = (note.insurance_name or "").strip()
        if not ins:
            empty_insurance += 1
            continue
        if not note.cpt_codes:
            continue

        rule = match_insurance(ins, cpt_rules)
        if rule is None:
            unmapped_notes += 1
            unmapped_counter[ins] += 1
            unmapped_examples.setdefault(ins, (note.patient_name, note.daily_note_id))
            continue

        mapped_notes += 1
        cpt_violations.extend(audit_cpt(note, rule, cpt_config))

    unmapped_rows = [
        {
            "insurance_name": name,
            "note_count": str(count),
            "example_patient": unmapped_examples.get(name, ("", ""))[0],
            "example_note_id": unmapped_examples.get(name, ("", ""))[1],
        }
        for name, count in unmapped_counter.most_common()
    ]

    cpt_counts = Counter((r["rule_id"], r["severity"]) for r in cpt_violations)
    icd_counts = Counter((r["rule_id"], r["severity"]) for r in icd_violations)

    summary_rows: list[dict[str, str]] = [
        {
            "section": "meta",
            "rule_id": "total_notes",
            "severity": "",
            "count": str(len(notes)),
            "notes": "All daily notes loaded",
        },
        {
            "section": "meta",
            "rule_id": "notes_with_cpt_mapped",
            "severity": "",
            "count": str(mapped_notes),
            "notes": "Notes with CPT lines matched to an insurance rule",
        },
        {
            "section": "meta",
            "rule_id": "notes_with_cpt_unmapped_insurance",
            "severity": "",
            "count": str(unmapped_notes),
            "notes": "Notes with CPT but insurance not in guide rules",
        },
        {
            "section": "meta",
            "rule_id": "notes_empty_insurance",
            "severity": "",
            "count": str(empty_insurance),
            "notes": "Notes with blank insurance_name",
        },
        {
            "section": "meta",
            "rule_id": "cpt_violation_rows",
            "severity": "",
            "count": str(len(cpt_violations)),
            "notes": "Total CPT violation rows",
        },
        {
            "section": "meta",
            "rule_id": "icd_violation_rows",
            "severity": "",
            "count": str(len(icd_violations)),
            "notes": "Total ICD violation rows",
        },
    ]
    for (rule_id, severity), count in sorted(cpt_counts.items(), key=lambda x: (-x[1], x[0])):
        summary_rows.append(
            {
                "section": "cpt",
                "rule_id": rule_id,
                "severity": severity,
                "count": str(count),
                "notes": "",
            }
        )
    for (rule_id, severity), count in sorted(icd_counts.items(), key=lambda x: (-x[1], x[0])):
        summary_rows.append(
            {
                "section": "icd",
                "rule_id": rule_id,
                "severity": severity,
                "count": str(count),
                "notes": "",
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        out_dir / "summary.csv",
        summary_rows,
        ["section", "rule_id", "severity", "count", "notes"],
    )
    write_csv(
        out_dir / "cpt_violations.csv",
        cpt_violations,
        [
            "daily_note_id",
            "patient_id",
            "patient_name",
            "date_of_daily_note",
            "facility_name",
            "insurance_name",
            "matched_rule",
            "visit_no",
            "note_file",
            "diagnosis_icd_codes",
            "cpt_codes",
            "rule_id",
            "severity",
            "expected",
            "found",
            "detail",
        ],
    )
    write_csv(
        out_dir / "icd_violations.csv",
        icd_violations,
        [
            "daily_note_id",
            "patient_id",
            "patient_name",
            "date_of_daily_note",
            "facility_name",
            "insurance_name",
            "visit_no",
            "note_file",
            "diagnosis_icd_codes",
            "rule_id",
            "category",
            "severity",
            "conflicting_codes",
            "description",
            "correct_approach",
        ],
    )
    write_csv(
        out_dir / "unmapped_insurance.csv",
        unmapped_rows,
        ["insurance_name", "note_count", "example_patient", "example_note_id"],
    )
    write_excel(
        out_dir / "audit_report.xlsx",
        summary_rows,
        cpt_violations,
        icd_violations,
        unmapped_rows,
    )

    return {
        "notes": len(notes),
        "mapped": mapped_notes,
        "unmapped": unmapped_notes,
        "cpt_violations": len(cpt_violations),
        "icd_violations": len(icd_violations),
        "unmapped_insurances": len(unmapped_rows),
        "out_dir": str(out_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="CPT + ICD billing audit for extracted WebPT notes")
    parser.add_argument(
        "--extracted",
        type=Path,
        required=True,
        help="Path to extracted folder containing cpt_codes.csv and daily_notes.csv",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for audit_report.xlsx and CSVs",
    )
    parser.add_argument(
        "--cpt-rules",
        type=Path,
        default=DEFAULT_CPT_RULES,
        help="Path to cpt_insurance_rules.yaml",
    )
    parser.add_argument(
        "--icd-rules",
        type=Path,
        default=DEFAULT_ICD_RULES,
        help="Path to icd_denial_rules.yaml",
    )
    args = parser.parse_args()

    result = run_audit(args.extracted, args.out, args.cpt_rules, args.icd_rules)
    print(
        f"Audited {result['notes']} notes | "
        f"mapped={result['mapped']} unmapped_ins={result['unmapped']} | "
        f"CPT violations={result['cpt_violations']} | "
        f"ICD violations={result['icd_violations']} | "
        f"unique unmapped insurances={result['unmapped_insurances']}"
    )
    print(f"Report written to {result['out_dir']}")


if __name__ == "__main__":
    main()
