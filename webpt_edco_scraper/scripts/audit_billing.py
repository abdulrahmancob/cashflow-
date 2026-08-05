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
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.icd10_catalog import Icd10Catalog, get_default_catalog  # noqa: E402

try:
    from cashflow_reconcile.payer_registry import resolve as resolve_payer_org
except ImportError:  # pragma: no cover - optional when run outside monorepo
    resolve_payer_org = None  # type: ignore[assignment]

AUDIT_DIR = ROOT / "audit"
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
    treatment_diagnosis_icd_codes: str = ""
    cpt_codes: set[str] = field(default_factory=set)
    cpt_details: list[dict[str, str]] = field(default_factory=list)

    def audited_icd_codes(self) -> list[str]:
        """Union of diagnosis + treatment ICD lists, stable order."""
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in (self.diagnosis_icd_codes, self.treatment_diagnosis_icd_codes):
            for code in _parse_icd_list(raw):
                if code not in seen:
                    seen.add(code)
                    ordered.append(code)
        return ordered

    def audited_icd_display(self) -> str:
        return "; ".join(self.audited_icd_codes())


def _parse_icd_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip().upper() for c in raw.replace(",", ";").split(";") if c.strip()]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _payer_org_fields(insurance_name: str) -> dict[str, str]:
    if resolve_payer_org is None:
        return {"payer_org_code": "", "payer_org": ""}
    hit = resolve_payer_org(insurance_name, "webpt") or resolve_payer_org(
        insurance_name, "any"
    )
    if hit is None:
        return {"payer_org_code": "", "payer_org": ""}
    return {"payer_org_code": hit.code, "payer_org": hit.name}


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
                    treatment_diagnosis_icd_codes=(
                        row.get("treatment_diagnosis_icd_codes") or ""
                    ).strip(),
                )

    cpt_path = extracted / "cpt_codes.csv"
    if cpt_path.exists():
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
                        treatment_diagnosis_icd_codes=(
                            row.get("treatment_diagnosis_icd_codes") or ""
                        ).strip(),
                    )
                note = notes[nid]
                if not note.insurance_name:
                    note.insurance_name = (row.get("insurance_name") or "").strip()
                if not note.diagnosis_icd_codes:
                    note.diagnosis_icd_codes = (row.get("diagnosis_icd_codes") or "").strip()
                if not note.treatment_diagnosis_icd_codes:
                    note.treatment_diagnosis_icd_codes = (
                        row.get("treatment_diagnosis_icd_codes") or ""
                    ).strip()
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


def _codes_matching_prefixes(codes: set[str], prefixes: list[str]) -> list[str]:
    return [
        c for c in sorted(codes) if any(c.startswith(p) for p in prefixes)
    ]


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


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


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


def check_site_pair_map(
    codes: set[str], pairs: list[dict[str, Any]]
) -> list[str] | None:
    """Fire when any site pair has both left and right prefix matches."""
    conflicts: list[str] = []
    for pair in pairs or []:
        left = _codes_matching_prefixes(codes, list(pair.get("left_prefixes") or []))
        right = _codes_matching_prefixes(codes, list(pair.get("right_prefixes") or []))
        if left and right:
            conflicts.extend(left)
            conflicts.extend(right)
    return _dedupe_preserve(conflicts) or None


def check_bilateral_split(
    codes: set[str], families: list[dict[str, Any]]
) -> list[str] | None:
    """Fire when both unilateral R and L codes are present for a bilateral family."""
    conflicts: list[str] = []
    for family in families or []:
        right = [c for c in (family.get("right") or []) if c in codes]
        left = [c for c in (family.get("left") or []) if c in codes]
        if right and left:
            conflicts.extend(sorted(right + left))
    return _dedupe_preserve(conflicts) or None


def check_invalid_or_nonbillable(
    codes: set[str], catalog: Icd10Catalog
) -> list[str] | None:
    bad = catalog.invalid_or_nonbillable(codes)
    return bad or None


def audit_icd(
    note: NoteRecord,
    icd_rules: list[dict[str, Any]],
    *,
    site_maps: dict[str, list[dict[str, Any]]] | None = None,
    bilateral_families: list[dict[str, Any]] | None = None,
    catalog: Icd10Catalog | None = None,
) -> list[dict[str, str]]:
    codes = set(note.audited_icd_codes())
    if not codes:
        return []
    site_maps = site_maps or {}
    bilateral_families = bilateral_families or []
    display_codes = note.audited_icd_display()
    violations: list[dict[str, str]] = []
    for rule in icd_rules:
        check = rule.get("check")
        conflicting: list[str] = []
        if check == "lateralization_unspecified":
            hit = check_lateralization(codes)
            if not hit:
                continue
            conflicting = hit
        elif check == "site_pair_map":
            map_name = rule.get("site_map") or ""
            hit = check_site_pair_map(codes, site_maps.get(map_name) or [])
            if not hit:
                continue
            conflicting = hit
        elif check == "bilateral_split":
            hit = check_bilateral_split(codes, bilateral_families)
            if not hit:
                continue
            conflicting = hit
        elif check == "invalid_or_nonbillable":
            if catalog is None:
                continue
            hit = check_invalid_or_nonbillable(codes, catalog)
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
            conflicting = _dedupe_preserve(conflicting)

        correct = rule.get("correct_approach") or ""
        if check == "bilateral_split":
            # Enrich message with suggested bilateral codes when available.
            suggestions: list[str] = []
            for family in bilateral_families:
                right = [c for c in (family.get("right") or []) if c in codes]
                left = [c for c in (family.get("left") or []) if c in codes]
                if right and left:
                    suggestions.extend(family.get("bilateral") or [])
            if suggestions:
                correct = (
                    f"Use the bilateral code(s) {', '.join(_dedupe_preserve(suggestions))} "
                    "instead of separate R/L codes."
                )

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
                "diagnosis_icd_codes": display_codes or note.diagnosis_icd_codes,
                "rule_id": rule["id"],
                "category": rule.get("category") or "",
                "severity": rule.get("severity") or "error",
                "conflicting_codes": "; ".join(conflicting),
                "description": rule.get("description") or "",
                "correct_approach": correct,
            }
        )
    return violations


def _preference_present(
    item: str,
    cpts: set[str],
    strapping_codes: set[str],
) -> bool:
    token = (item or "").strip().lower()
    if not token or token == "-":
        return True
    if token == "strapping":
        return bool(cpts & strapping_codes)
    return token.upper() in cpts


def _expand_do_not_use(
    items: list[str],
    strapping_codes: set[str],
) -> list[tuple[str, set[str]]]:
    """Return list of (label, forbidden_cpt_set) from do_not_use tokens."""
    expanded: list[tuple[str, set[str]]] = []
    for raw in items or []:
        token = (raw or "").strip()
        if not token or token == "-":
            continue
        lower = token.lower()
        if lower == "strapping":
            expanded.append(("strapping", set(strapping_codes)))
        else:
            code = token.upper()
            expanded.append((code, {code}))
    return expanded


def _timed_units_total(
    note: NoteRecord,
    timed_codes: set[str],
    excluded: set[str],
) -> int:
    total = 0
    for detail in note.cpt_details:
        code = (detail.get("cpt_code") or "").upper()
        if code in excluded or code not in timed_codes:
            continue
        try:
            units = int(float(detail.get("units") or "0"))
        except ValueError:
            units = 0
        total += max(units, 0)
    return total


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
    manual_code = (config.get("manual_code") or "97140").upper()
    ta_code = (config.get("therapeutic_activity_code") or "97530").upper()
    estim_97014 = (config.get("estim_97014") or "97014").upper()
    estim_g0283 = (config.get("estim_g0283") or "G0283").upper()

    has_timed = bool(cpts & timed_codes)
    has_eval_only = bool(cpts & eval_codes) and not has_timed and reeval_code not in cpts

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

    # Global: 97140 & 97124 can't be used together
    global_rules = config.get("global_rules") or {}
    if global_rules.get("forbid_manual_and_massage_together"):
        if manual_code in cpts and massage_code in cpts:
            add(
                "manual_massage_together",
                "error",
                f"Manual therapy ({manual_code}) and massage ({massage_code}) cannot be billed together.",
                expected=f"{manual_code} or {massage_code}",
                found=f"{manual_code}; {massage_code}",
            )

    # Accepted E-stim mismatch (only when an e-stim code is present)
    expected_estim = (rule.get("estim") or "").strip().upper()
    has_estim = estim_97014 in cpts or estim_g0283 in cpts
    if expected_estim and has_estim:
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

    # Do not use
    for label, forbidden in _expand_do_not_use(
        list(rule.get("do_not_use") or []), strapping_codes
    ):
        hit = sorted(cpts & forbidden)
        if not hit:
            continue
        if label == "strapping":
            add(
                "do_not_use_strapping",
                "error",
                f"Strapping is listed under Do not use for this insurance but found: {', '.join(hit)}.",
                expected="no strapping",
                found="; ".join(hit),
            )
        else:
            add(
                f"do_not_use_{label}",
                "error",
                f"CPT {label} is listed under Do not use for this insurance but was billed.",
                expected=f"no {label}",
                found="; ".join(hit),
            )

    # Highly preferred missing (always warning — sheet preference, not hard deny)
    for item in rule.get("highly_preferred") or []:
        if _preference_present(item, cpts, strapping_codes):
            continue
        label = item.strip()
        if has_timed:
            context = "treatment visit"
        elif has_eval_only:
            context = "eval-only visit"
        else:
            context = "visit"
        add(
            "highly_preferred_missing",
            "warning",
            f"Highly preferred '{label}' is missing on this {context}.",
            expected=label,
            found="none" if label.lower() != "strapping" else "none",
        )

    # Preferred missing (treatment visits only → warning)
    for item in rule.get("preferred") or []:
        if _preference_present(item, cpts, strapping_codes):
            continue
        if not has_timed:
            continue
        label = item.strip()
        add(
            "preferred_missing",
            "warning",
            f"Preferred '{label}' is missing on this treatment visit.",
            expected=label,
            found="none",
        )

    # Require 97530 when Use column says so
    if rule.get("require_97530") and has_timed and ta_code not in cpts:
        add(
            "missing_therapeutic_activity",
            "error",
            f"Insurance Use column requires Therapeutic Activities ({ta_code}), but it is missing.",
            expected=ta_code,
            found="; ".join(sorted(cpts & timed_codes)) or "none",
        )

    # Max timed units excluding eval/reeval
    max_units = rule.get("max_timed_units")
    if max_units is not None:
        excluded = set(eval_codes) | {reeval_code}
        total = _timed_units_total(note, timed_codes, excluded)
        try:
            limit = int(max_units)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0 and total > limit:
            add(
                "max_timed_units_exceeded",
                "error",
                f"Timed units excluding eval/reeval total {total}, but insurance max is {limit}U.",
                expected=f"<= {limit}U",
                found=f"{total}U",
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
            [
                "insurance_name",
                "note_count",
                "example_patient",
                "example_note_id",
                "payer_org_code",
                "payer_org",
            ],
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
    catalog: Icd10Catalog | None = None,
) -> dict[str, Any]:
    cpt_config = load_yaml(cpt_rules_path)
    icd_config = load_yaml(icd_rules_path)
    cpt_rules = compile_cpt_rules(cpt_config)
    icd_rules = list(icd_config.get("rules") or [])
    site_maps = dict(icd_config.get("site_maps") or {})
    bilateral_families = list(icd_config.get("bilateral_families") or [])
    icd_catalog = catalog if catalog is not None else get_default_catalog()

    notes = load_notes(extracted)
    cpt_violations: list[dict[str, str]] = []
    icd_violations: list[dict[str, str]] = []
    unmapped_counter: Counter[str] = Counter()
    unmapped_examples: dict[str, tuple[str, str]] = {}
    mapped_notes = 0
    unmapped_notes = 0
    empty_insurance = 0

    for note in notes.values():
        icd_violations.extend(
            audit_icd(
                note,
                icd_rules,
                site_maps=site_maps,
                bilateral_families=bilateral_families,
                catalog=icd_catalog,
            )
        )

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
            **_payer_org_fields(name),
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
        [
            "insurance_name",
            "note_count",
            "example_patient",
            "example_note_id",
            "payer_org_code",
            "payer_org",
        ],
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
