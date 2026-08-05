"""Parse WebPT Daily Note PDFs: billing header fields and CPT lines."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from edoc_ocr import classify_edoc_filename, extract_pdf_text, parse_icd_codes, pdf_to_text
from logging_config import get_logger

log = get_logger("chart_notes_parse")

DAILY_NOTE_ID_RE = re.compile(r"(DN\d+)", re.IGNORECASE)
POC_ID_RE = re.compile(r"(PO\d+)", re.IGNORECASE)
POC_PLAN_SECTION_RE = re.compile(
    r"(?:^|\n)\s*Plan\s*\n(.*?)(?=\n\s*(?:Treatment to be provided|Procedures)\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)
POC_DATE_RE = re.compile(
    r"Date of Plan of Care:\s*(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
POC_FREQUENCY_RE = re.compile(r"Frequency:\s*(.+)", re.IGNORECASE)
POC_DURATION_RE = re.compile(r"^Duration:\s*(.+)", re.IGNORECASE | re.MULTILINE)
POC_PLAN_LINE_RE = re.compile(r"^Plan:\s*(.+)", re.IGNORECASE | re.MULTILINE)
POC_SHORT_TERM_RE = re.compile(
    r"Short\s+Term\s+Goals\s*:\s*(.*?)(?="
    r"\n\s*Long\s+Term\s+Goals\s*:|"
    r"\n\s*Plan\s*\n|"
    r"\n\s*Frequency\s*:|"
    r"\n\s*Treatment to be provided\b|"
    r"\Z)",
    re.IGNORECASE | re.DOTALL,
)
POC_LONG_TERM_RE = re.compile(
    r"Long\s+Term\s+Goals\s*:\s*(.*?)(?="
    r"\n\s*Short\s+Term\s+Goals\s*:|"
    r"\n\s*Plan\s*\n|"
    r"\n\s*Frequency\s*:|"
    r"\n\s*Treatment to be provided\b|"
    r"\Z)",
    re.IGNORECASE | re.DOTALL,
)
POC_PAGE_HEADER_RE = re.compile(
    r"(?:^|\n)\s*\d+\s+of\s+\d+\s*\n.*?\n\s*Plan of Care\s*(?=\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)
POC_GOAL_LINE_RE = re.compile(
    r"(?P<n>\d+):\s*\((?P<weeks>\d+)\s*Weeks?\)\s*\|\s*"
    r"(?:(?P<pct>\d+)\s*%\s*\|\s*)?(?P<text>.+?)\s*\|",
    re.IGNORECASE | re.DOTALL,
)
MODIFIER_CPT_RE = re.compile(r"^([A-Z]{2}):(\d{5})(?:\.([A-Z0-9]+))?\s*$")
# Bare CPT under Direct Timed Codes (no GP:/GO: prefix), e.g. "97110"
BARE_CPT_RE = re.compile(r"^(\d{5})\s*$")
# End of billing CPT block before narrative / page chrome
CPT_SECTION_STOP_RE = re.compile(
    r"^(?:\d+\s+of\s+\d+|Patient Name:|Phone:|Fax:|Document Date:|"
    r"Daily Note\s*/|Subjective|Objective|Assessment|Plan\b)",
    re.IGNORECASE,
)
US_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
PHONE_RE = re.compile(r"Phone:\s*(.+)", re.IGNORECASE)
FAX_RE = re.compile(r"Fax:\s*(.+)", re.IGNORECASE)
ICD_ENTRY_START_RE = re.compile(r"([A-Z]\d{2}(?:\.\d+)?)\s*:\s*", re.IGNORECASE)

HEADER_LABELS: list[str] = [
    "Patient Name",
    "Date of Daily Note",
    "Date of Birth",
    "Injury/Onset/Change of Status Date",
    "Referring Physician/NPP",
    "Diagnosis",
    "Date of Original Eval",
    "Visit No.",
    "Treatment Diagnosis",
    "Insurance Name",
]

DAILY_NOTES_FIELDNAMES: list[str] = [
    "patient_id",
    "daily_note_id",
    "note_file",
    "facility_name",
    "facility_address",
    "facility_phone",
    "facility_fax",
    "patient_name",
    "date_of_daily_note",
    "date_of_birth",
    "injury_onset_date",
    "injury_onset_qualifier",
    "referring_physician",
    "diagnosis_raw",
    "diagnosis_icd_codes",
    "diagnosis_icd_full",
    "date_of_original_eval",
    "visit_no",
    "treatment_diagnosis_raw",
    "treatment_diagnosis_icd_codes",
    "treatment_diagnosis_icd_full",
    "insurance_name",
    "cpt_summary",
    "extraction_method",
    "error",
]

CPT_CODES_FIELDNAMES: list[str] = [
    "patient_id",
    "daily_note_id",
    "date_of_daily_note",
    "patient_name",
    "diagnosis_icd_codes",
    "insurance_name",
    "visit_no",
    "note_file",
    "modifier",
    "cpt_code",
    "billing_modifier_suffix",
    "modifier_cpt",
    "units",
    "description",
]

# Case-centric schemas (required facility_id + case_id). Prefer case_extract.export_case_daily_notes.
CASE_REQUIRED_COLUMNS = (
    "facility_id",
    "case_id",
    "patient_id",
    "date_of_daily_note",
    "daily_note_id",
    "note_file",
    "source_url",
    "downloaded_at",
)

REFERRAL_ICD_FIELDNAMES: list[str] = [
    "patient_id",
    "filename",
    "path",
    "icd_codes",
    "extraction_method",
    "error",
]

PLANS_OF_CARE_FIELDNAMES: list[str] = [
    "patient_id",
    "poc_id",
    "note_file",
    "date_of_plan_of_care",
    "frequency",
    "duration",
    "plan",
    "extraction_method",
    "error",
]

POC_GOALS_FIELDNAMES: list[str] = [
    "patient_id",
    "poc_id",
    "note_file",
    "date_of_plan_of_care",
    "goal_type",
    "goal_number",
    "weeks",
    "progress_pct",
    "goal_text",
]


@dataclass
class CptLine:
    modifier: str = ""
    cpt_code: str = ""
    billing_modifier_suffix: str = ""
    units: str = ""
    description: str = ""

    @property
    def modifier_cpt(self) -> str:
        if self.modifier and self.cpt_code:
            base = f"{self.modifier}:{self.cpt_code}"
            if self.billing_modifier_suffix:
                return f"{base}.{self.billing_modifier_suffix}"
            return base
        return self.cpt_code


@dataclass
class DailyNoteExtract:
    patient_id: str = ""
    daily_note_id: str = ""
    note_file: str = ""
    facility_name: str = ""
    facility_address: str = ""
    facility_phone: str = ""
    facility_fax: str = ""
    patient_name: str = ""
    date_of_daily_note: str = ""
    date_of_birth: str = ""
    injury_onset_date: str = ""
    injury_onset_qualifier: str = ""
    referring_physician: str = ""
    diagnosis_raw: str = ""
    diagnosis_icd_codes: str = ""
    diagnosis_icd_full: str = ""
    date_of_original_eval: str = ""
    visit_no: str = ""
    treatment_diagnosis_raw: str = ""
    treatment_diagnosis_icd_codes: str = ""
    treatment_diagnosis_icd_full: str = ""
    insurance_name: str = ""
    cpt_lines: list[CptLine] = field(default_factory=list)
    extraction_method: str = "native_text"
    error: str = ""

    @property
    def cpt_summary(self) -> str:
        parts: list[str] = []
        for line in self.cpt_lines:
            key = line.modifier_cpt
            if line.units:
                parts.append(f"{key}x{line.units}")
            elif key:
                parts.append(key)
        return "; ".join(parts)


def daily_note_id_from_filename(filename: str) -> str:
    match = DAILY_NOTE_ID_RE.search(filename)
    return match.group(1).upper() if match else ""


def poc_id_from_filename(filename: str) -> str:
    match = POC_ID_RE.search(filename)
    return match.group(1).upper() if match else ""


@dataclass
class PocGoal:
    goal_type: str = ""
    goal_number: str = ""
    weeks: str = ""
    progress_pct: str = ""
    goal_text: str = ""


@dataclass
class PlanOfCareExtract:
    patient_id: str = ""
    poc_id: str = ""
    note_file: str = ""
    date_of_plan_of_care: str = ""
    frequency: str = ""
    duration: str = ""
    plan: str = ""
    goals: list[PocGoal] = field(default_factory=list)
    extraction_method: str = "native_text"
    error: str = ""


def parse_plan_of_care(text: str) -> dict[str, str]:
    """Extract Frequency / Duration / Plan from Plan of Care PDF text."""
    date_match = POC_DATE_RE.search(text or "")
    section_match = POC_PLAN_SECTION_RE.search(text or "")
    block = section_match.group(1) if section_match else (text or "")

    freq_match = POC_FREQUENCY_RE.search(block) or POC_FREQUENCY_RE.search(text or "")
    duration_match = POC_DURATION_RE.search(block)
    if not duration_match and not section_match:
        # Fallback only when we could not isolate the Plan section.
        duration_match = POC_DURATION_RE.search(text or "")
    plan_match = POC_PLAN_LINE_RE.search(block) or POC_PLAN_LINE_RE.search(text or "")

    return {
        "date_of_plan_of_care": (
            us_date_to_iso(date_match.group(1)) if date_match else ""
        ),
        "frequency": (freq_match.group(1).strip() if freq_match else ""),
        "duration": (duration_match.group(1).strip() if duration_match else ""),
        "plan": (plan_match.group(1).strip() if plan_match else ""),
    }


def _strip_poc_page_headers(section: str) -> str:
    return POC_PAGE_HEADER_RE.sub("\n", section or "")


def _normalize_goal_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _parse_goals_in_section(section: str, goal_type: str) -> list[PocGoal]:
    cleaned = _strip_poc_page_headers(section)
    goals: list[PocGoal] = []
    for match in POC_GOAL_LINE_RE.finditer(cleaned):
        pct = match.group("pct")
        goals.append(
            PocGoal(
                goal_type=goal_type,
                goal_number=match.group("n"),
                weeks=match.group("weeks"),
                progress_pct=pct if pct is not None else "",
                goal_text=_normalize_goal_text(match.group("text")),
            )
        )
    return goals


def parse_poc_goals(text: str) -> list[PocGoal]:
    """Extract Short/Long Term Goals (weeks, optional progress %, text)."""
    source = text or ""
    goals: list[PocGoal] = []
    short_match = POC_SHORT_TERM_RE.search(source)
    if short_match:
        goals.extend(_parse_goals_in_section(short_match.group(1), "short_term"))
    long_match = POC_LONG_TERM_RE.search(source)
    if long_match:
        goals.extend(_parse_goals_in_section(long_match.group(1), "long_term"))
    return goals


def us_date_to_iso(value: str) -> str:
    match = US_DATE_RE.search(value or "")
    if not match:
        return (value or "").strip()
    month, day, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def parse_icd_entries(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    cleaned = re.sub(r"ICD10:\s*", "", text, flags=re.IGNORECASE).strip()
    if not cleaned:
        return []
    positions = list(ICD_ENTRY_START_RE.finditer(cleaned))
    if not positions:
        return []
    entries: list[tuple[str, str]] = []
    for i, match in enumerate(positions):
        code = match.group(1).upper()
        start = match.end()
        end = positions[i + 1].start() if i + 1 < len(positions) else len(cleaned)
        desc = cleaned[start:end].strip().rstrip(",").strip()
        entries.append((code, desc))
    return entries


def format_icd_codes(codes: list[str]) -> str:
    return "; ".join(codes)


def format_icd_full(entries: list[tuple[str, str]]) -> str:
    return "; ".join(f"{code}: {desc}" for code, desc in entries)


def _billing_header_section(text: str) -> str:
    match = re.search(r"\nSubjective\b", text, re.IGNORECASE)
    if match:
        return text[:match.start()]
    return text


def _cpt_section(text: str) -> str:
    timed = re.search(r"Direct Timed Codes", text, re.IGNORECASE)
    if timed:
        chunk = text[timed.start():]
    else:
        doc_dates = list(re.finditer(r"Document Date:", text, re.IGNORECASE))
        if doc_dates:
            chunk = text[doc_dates[-1].start():]
        else:
            matches = list(re.finditer(r"CPT.?\s*Code", text, re.IGNORECASE))
            chunk = text[matches[0].start():] if matches else text

    end = len(chunk)
    for marker in ("CPT copyright", "CPT® copyright", "All rights reserved"):
        idx = chunk.find(marker)
        if idx > 0:
            end = min(end, idx)
    return chunk[:end]


def _label_value_map(section: str) -> dict[str, str]:
    label_alt = "|".join(re.escape(label) for label in HEADER_LABELS)
    pattern = re.compile(
        rf"(?P<label>{label_alt}):\s*(?P<value>.*?)(?=\n(?:{label_alt}):|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    result: dict[str, str] = {}
    for match in pattern.finditer(section):
        label = match.group("label").strip()
        value = re.sub(r"\s+", " ", match.group("value")).strip()
        result[label.lower()] = value
    return result


def _parse_facility_block(section: str) -> dict[str, str]:
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
    phone_match = PHONE_RE.search(section)
    fax_match = FAX_RE.search(section)
    facility_name = ""
    facility_address = ""
    for line in lines:
        lower = line.lower()
        if lower.startswith("phone:") or lower.startswith("fax:"):
            continue
        if "daily note" in lower or "billing sheet" in lower:
            break
        if not facility_name:
            facility_name = line
        elif not facility_address and re.search(r"\d", line):
            facility_address = line
    return {
        "facility_name": facility_name,
        "facility_address": facility_address,
        "facility_phone": phone_match.group(1).strip() if phone_match else "",
        "facility_fax": fax_match.group(1).strip() if fax_match else "",
    }


def _split_onset_date_and_qualifier(value: str) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not value:
        return "", ""
    date_match = US_DATE_RE.search(value)
    if not date_match:
        return value, ""
    iso = us_date_to_iso(date_match.group(0))
    remainder = value[date_match.end():].strip()
    return iso, remainder


def _trim_soc_suffix(value: str) -> str:
    return re.split(r"\s+SOC Date:", value, flags=re.IGNORECASE)[0].strip()


def parse_daily_note_header(text: str) -> dict[str, str]:
    section = _billing_header_section(text)
    labels = _label_value_map(section)
    facility = _parse_facility_block(section)

    diagnosis_raw = labels.get("diagnosis", "")
    treatment_raw = _trim_soc_suffix(labels.get("treatment diagnosis", ""))
    diagnosis_entries = parse_icd_entries(diagnosis_raw)
    treatment_entries = parse_icd_entries(treatment_raw)
    onset_iso, onset_qualifier = _split_onset_date_and_qualifier(
        labels.get("injury/onset/change of status date", "")
    )

    return {
        **facility,
        "patient_name": labels.get("patient name", ""),
        "date_of_daily_note": us_date_to_iso(labels.get("date of daily note", "")),
        "date_of_birth": us_date_to_iso(labels.get("date of birth", "")),
        "injury_onset_date": onset_iso,
        "injury_onset_qualifier": onset_qualifier,
        "referring_physician": labels.get("referring physician/npp", ""),
        "diagnosis_raw": diagnosis_raw,
        "diagnosis_icd_codes": format_icd_codes(parse_icd_codes(diagnosis_raw)),
        "diagnosis_icd_full": format_icd_full(diagnosis_entries),
        "date_of_original_eval": us_date_to_iso(labels.get("date of original eval", "")),
        "visit_no": labels.get("visit no.", ""),
        "treatment_diagnosis_raw": treatment_raw,
        "treatment_diagnosis_icd_codes": format_icd_codes(parse_icd_codes(treatment_raw)),
        "treatment_diagnosis_icd_full": format_icd_full(treatment_entries),
        "insurance_name": labels.get("insurance name", ""),
    }


def _is_cpt_code_line(line: str) -> bool:
    return bool(MODIFIER_CPT_RE.match(line) or BARE_CPT_RE.match(line))


def parse_daily_note_cpt(text: str) -> list[CptLine]:
    section = _cpt_section(text)
    if not section:
        return []

    lines = section.splitlines()
    cpt_lines: list[CptLine] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if CPT_SECTION_STOP_RE.match(line):
            break

        mod_match = MODIFIER_CPT_RE.match(line)
        bare_match = BARE_CPT_RE.match(line) if not mod_match else None
        if not mod_match and not bare_match:
            i += 1
            continue

        if mod_match:
            modifier, cpt_code = mod_match.group(1).upper(), mod_match.group(2)
            billing_suffix = (mod_match.group(3) or "").upper()
        else:
            assert bare_match is not None
            modifier, cpt_code = "", bare_match.group(1)
            billing_suffix = ""

        description = ""
        units = ""
        j = i + 1
        if j < len(lines):
            nxt = lines[j].strip()
            if (
                not _is_cpt_code_line(nxt)
                and not CPT_SECTION_STOP_RE.match(nxt)
                and not re.fullmatch(r"\d+", nxt)
            ):
                description = nxt
                j += 1
        while j < len(lines):
            candidate = lines[j].strip()
            if CPT_SECTION_STOP_RE.match(candidate):
                break
            if _is_cpt_code_line(candidate):
                break
            if not units and re.fullmatch(r"\d+", candidate):
                units = candidate
                j += 1
                break
            j += 1

        cpt_lines.append(
            CptLine(
                modifier=modifier,
                cpt_code=cpt_code,
                billing_modifier_suffix=billing_suffix,
                units=units,
                description=description,
            )
        )
        i = j if j > i + 1 else i + 1

    return cpt_lines


def pdf_to_plain_text(path: Path) -> str:
    doc = fitz.open(path)
    chunks: list[str] = []
    try:
        for page in doc:
            chunks.append(page.get_text() or "")
    finally:
        doc.close()
    return "\n".join(chunks)


def extract_daily_note(
    path: Path,
    *,
    patient_id: str = "",
) -> DailyNoteExtract:
    result = DailyNoteExtract(
        patient_id=patient_id,
        daily_note_id=daily_note_id_from_filename(path.name),
        note_file=path.name,
    )
    if not path.exists():
        result.error = "file missing"
        return result

    try:
        text = pdf_to_plain_text(path)
    except Exception as exc:
        result.error = str(exc)
        log.warning("Failed to read %s: %s", path, exc)
        return result

    if not text.strip():
        result.error = "empty text"
        return result

    header = parse_daily_note_header(text)
    for key, value in header.items():
        if hasattr(result, key):
            setattr(result, key, value)

    result.cpt_lines = parse_daily_note_cpt(text)
    return result


def extract_plan_of_care(
    path: Path,
    *,
    patient_id: str = "",
    tesseract_cmd: str | None = None,
    ocr_dpi: int = 200,
) -> PlanOfCareExtract:
    result = PlanOfCareExtract(
        patient_id=patient_id,
        poc_id=poc_id_from_filename(path.name),
        note_file=path.name,
    )
    if not path.exists():
        result.error = "file missing"
        return result

    try:
        native = pdf_to_plain_text(path).strip()
        if len(native) >= 50:
            text = native
            method = "native_text"
        else:
            text, method = extract_pdf_text(
                path,
                dpi=ocr_dpi,
                tesseract_cmd=tesseract_cmd,
            )
            text = (text or "").strip()
            if not method or method == "none":
                method = "ocr" if text else "none"
    except Exception as exc:
        result.error = str(exc)
        log.warning("Failed to read Plan of Care %s: %s", path, exc)
        return result

    result.extraction_method = method
    if not text:
        result.error = "empty text"
        return result

    parsed = parse_plan_of_care(text)
    result.date_of_plan_of_care = parsed["date_of_plan_of_care"]
    result.frequency = parsed["frequency"]
    result.duration = parsed["duration"]
    result.plan = parsed["plan"]
    result.goals = parse_poc_goals(text)
    return result


def plan_of_care_row(extract: PlanOfCareExtract) -> dict[str, str]:
    return {
        "patient_id": extract.patient_id,
        "poc_id": extract.poc_id,
        "note_file": extract.note_file,
        "date_of_plan_of_care": extract.date_of_plan_of_care,
        "frequency": extract.frequency,
        "duration": extract.duration,
        "plan": extract.plan,
        "extraction_method": extract.extraction_method,
        "error": extract.error,
    }


def poc_goal_rows(extract: PlanOfCareExtract) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for goal in extract.goals:
        rows.append(
            {
                "patient_id": extract.patient_id,
                "poc_id": extract.poc_id,
                "note_file": extract.note_file,
                "date_of_plan_of_care": extract.date_of_plan_of_care,
                "goal_type": goal.goal_type,
                "goal_number": goal.goal_number,
                "weeks": goal.weeks,
                "progress_pct": goal.progress_pct,
                "goal_text": goal.goal_text,
            }
        )
    return rows


def daily_note_row(extract: DailyNoteExtract) -> dict[str, str]:
    return {
        "patient_id": extract.patient_id,
        "daily_note_id": extract.daily_note_id,
        "note_file": extract.note_file,
        "facility_name": extract.facility_name,
        "facility_address": extract.facility_address,
        "facility_phone": extract.facility_phone,
        "facility_fax": extract.facility_fax,
        "patient_name": extract.patient_name,
        "date_of_daily_note": extract.date_of_daily_note,
        "date_of_birth": extract.date_of_birth,
        "injury_onset_date": extract.injury_onset_date,
        "injury_onset_qualifier": extract.injury_onset_qualifier,
        "referring_physician": extract.referring_physician,
        "diagnosis_raw": extract.diagnosis_raw,
        "diagnosis_icd_codes": extract.diagnosis_icd_codes,
        "diagnosis_icd_full": extract.diagnosis_icd_full,
        "date_of_original_eval": extract.date_of_original_eval,
        "visit_no": extract.visit_no,
        "treatment_diagnosis_raw": extract.treatment_diagnosis_raw,
        "treatment_diagnosis_icd_codes": extract.treatment_diagnosis_icd_codes,
        "treatment_diagnosis_icd_full": extract.treatment_diagnosis_icd_full,
        "insurance_name": extract.insurance_name,
        "cpt_summary": extract.cpt_summary,
        "extraction_method": extract.extraction_method,
        "error": extract.error,
    }


def cpt_code_rows(extract: DailyNoteExtract) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in extract.cpt_lines:
        rows.append(
            {
                "patient_id": extract.patient_id,
                "daily_note_id": extract.daily_note_id,
                "date_of_daily_note": extract.date_of_daily_note,
                "patient_name": extract.patient_name,
                "diagnosis_icd_codes": extract.diagnosis_icd_codes,
                "insurance_name": extract.insurance_name,
                "visit_no": extract.visit_no,
                "note_file": extract.note_file,
                "modifier": line.modifier,
                "cpt_code": line.cpt_code,
                "billing_modifier_suffix": line.billing_modifier_suffix,
                "modifier_cpt": line.modifier_cpt,
                "units": line.units,
                "description": line.description,
            }
        )
    return rows


def iter_daily_note_pdfs(edocs_dir: Path) -> list[Path]:
    pattern = "*DailyNote*.pdf"
    return sorted(edocs_dir.glob(f"*/chart_notes/{pattern}"))


def iter_poc_pdfs(edocs_dir: Path) -> list[Path]:
    return sorted(edocs_dir.glob("*/chart_notes/*-PO*.pdf"))


def export_plans_of_care(
    edocs_dir: Path,
    output_dir: Path,
    *,
    tesseract_cmd: str | None = None,
    ocr_dpi: int = 200,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plans_path = output_dir / "plans_of_care.csv"
    goals_path = output_dir / "poc_goals.csv"

    rows: list[dict[str, str]] = []
    goal_rows: list[dict[str, str]] = []
    errors: list[str] = []

    pdfs = iter_poc_pdfs(edocs_dir)
    log.info("Found %d Plan of Care PDF(s) under %s", len(pdfs), edocs_dir)

    for pdf_path in pdfs:
        patient_id = pdf_path.parent.parent.name
        extract = extract_plan_of_care(
            pdf_path,
            patient_id=patient_id,
            tesseract_cmd=tesseract_cmd,
            ocr_dpi=ocr_dpi,
        )
        if extract.error:
            errors.append(f"{pdf_path.name}: {extract.error}")
        rows.append(plan_of_care_row(extract))
        goal_rows.extend(poc_goal_rows(extract))

    _write_csv(plans_path, rows, PLANS_OF_CARE_FIELDNAMES)
    _write_csv(goals_path, goal_rows, POC_GOALS_FIELDNAMES)

    summary = {
        "plans_of_care_count": len(rows),
        "poc_goals_count": len(goal_rows),
        "errors": errors,
        "plans_of_care_path": str(plans_path),
        "poc_goals_path": str(goals_path),
    }
    log.info(
        "Exported %d Plan of Care row(s) and %d goal row(s) to %s",
        len(rows),
        len(goal_rows),
        output_dir,
    )
    return summary


def export_daily_notes(
    edocs_dir: Path,
    output_dir: Path,
    *,
    include_referral_icd: bool = False,
    tesseract_cmd: str | None = None,
    ocr_dpi: int = 200,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    daily_notes_path = output_dir / "daily_notes.csv"
    cpt_codes_path = output_dir / "cpt_codes.csv"
    referral_icd_path = output_dir / "referral_icd.csv"

    daily_rows: list[dict[str, str]] = []
    cpt_rows: list[dict[str, str]] = []
    referral_rows: list[dict[str, str]] = []
    errors: list[str] = []

    pdfs = iter_daily_note_pdfs(edocs_dir)
    log.info("Found %d Daily Note PDF(s) under %s", len(pdfs), edocs_dir)

    for pdf_path in pdfs:
        patient_id = pdf_path.parent.parent.name
        extract = extract_daily_note(pdf_path, patient_id=patient_id)
        if extract.error:
            errors.append(f"{pdf_path.name}: {extract.error}")
        daily_rows.append(daily_note_row(extract))
        cpt_rows.extend(cpt_code_rows(extract))

    _write_csv(daily_notes_path, daily_rows, DAILY_NOTES_FIELDNAMES)
    _write_csv(cpt_codes_path, cpt_rows, CPT_CODES_FIELDNAMES)

    if include_referral_icd:
        referral_rows = extract_referral_icd_from_edocs(
            edocs_dir,
            tesseract_cmd=tesseract_cmd,
            dpi=ocr_dpi,
        )
        _write_csv(referral_icd_path, referral_rows, REFERRAL_ICD_FIELDNAMES)

    summary = {
        "daily_notes_count": len(daily_rows),
        "cpt_lines_count": len(cpt_rows),
        "referral_icd_count": len(referral_rows),
        "errors": errors,
        "daily_notes_path": str(daily_notes_path),
        "cpt_codes_path": str(cpt_codes_path),
        "referral_icd_path": str(referral_icd_path) if include_referral_icd else "",
    }
    log.info(
        "Exported %d daily note(s), %d CPT line(s) to %s",
        len(daily_rows),
        len(cpt_rows),
        output_dir,
    )
    return summary


def extract_referral_icd_from_edocs(
    edocs_dir: Path,
    *,
    tesseract_cmd: str | None = None,
    dpi: int = 200,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for patient_dir in sorted(edocs_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        patient_id = patient_dir.name
        for pdf_path in sorted(patient_dir.glob("*.pdf")):
            flags = classify_edoc_filename(pdf_path.name)
            if not flags.get("has_referral"):
                continue
            method = "native_text"
            error = ""
            try:
                native_len = len(pdf_to_plain_text(pdf_path).strip())
                if native_len < 50:
                    text = pdf_to_text(
                        pdf_path,
                        dpi=dpi,
                        tesseract_cmd=tesseract_cmd,
                    )
                    method = "ocr"
                else:
                    text = pdf_to_plain_text(pdf_path)
                codes = format_icd_codes(parse_icd_codes(text))
            except Exception as exc:
                text = ""
                codes = ""
                error = str(exc)
                log.warning("Referral ICD failed for %s: %s", pdf_path, exc)

            rows.append(
                {
                    "patient_id": patient_id,
                    "filename": pdf_path.name,
                    "path": str(pdf_path),
                    "icd_codes": codes,
                    "extraction_method": method,
                    "error": error,
                }
            )
    return rows


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


VALIDATION_REPORT_FIELDNAMES = [
    "patient_id",
    "rel_path",
    "doc_category",
    "on_disk",
    "in_ocr_csv",
    "in_daily_notes_csv",
    "has_cpt_lines",
    "extraction_method",
    "text_chars",
    "ocr_error",
    "referral_icd_codes",
    "status",
]


def _pdf_rel_path(pdf_path: Path, patient_id: str) -> str:
    if pdf_path.parent.name == "chart_notes":
        return f"chart_notes/{pdf_path.name}"
    return pdf_path.name


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _classify_pdf_rel_path(rel_path: str, filename: str) -> str:
    if rel_path.startswith("chart_notes/"):
        if "dailynote" in filename.lower():
            return "daily_note"
        return "chart_note"
    flags = classify_edoc_filename(filename)
    if flags.get("has_referral"):
        return "referral"
    if flags.get("has_intake"):
        return "intake"
    if flags.get("has_mri"):
        return "mri"
    if flags.get("has_insurance_id"):
        return "insurance_id"
    return "edoc"


def run_validate_extraction(
    edocs_dir: Path,
    extracted_dir: Path,
) -> dict[str, Any]:
    """Compare on-disk PDFs with extraction CSVs and write validation_report.csv."""
    ocr_rows = _load_csv_rows(extracted_dir / "ocr_all_files.csv")
    dn_rows = _load_csv_rows(extracted_dir / "daily_notes.csv")
    cpt_rows = _load_csv_rows(extracted_dir / "cpt_codes.csv")
    ref_rows = _load_csv_rows(extracted_dir / "referral_icd.csv")

    ocr_by_key = {(r["patient_id"], r["rel_path"]): r for r in ocr_rows}
    dn_by_file = {r["note_file"]: r for r in dn_rows}
    cpt_dn_ids = {r["daily_note_id"] for r in cpt_rows}
    ref_by_key = {(r["patient_id"], r["filename"]): r for r in ref_rows}

    disk_files: list[tuple[str, str, Path]] = []
    for patient_dir in sorted(edocs_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        pid = patient_dir.name
        for pdf in sorted(patient_dir.glob("*.pdf")):
            disk_files.append((pid, _pdf_rel_path(pdf, pid), pdf))
        chart_dir = patient_dir / "chart_notes"
        if chart_dir.is_dir():
            for pdf in sorted(chart_dir.glob("*.pdf")):
                disk_files.append((pid, _pdf_rel_path(pdf, pid), pdf))

    report_rows: list[dict[str, str]] = []
    status_counts: dict[str, int] = {}

    for patient_id, rel_path, _ in disk_files:
        filename = rel_path.split("/", 1)[-1]
        category = _classify_pdf_rel_path(rel_path, filename)
        ocr = ocr_by_key.get((patient_id, rel_path), {})
        in_ocr = "yes" if ocr else "no"
        ocr_error = ocr.get("error", "")
        extraction_method = ocr.get("extraction_method", "")
        text_chars = ocr.get("text_chars", "")

        in_dn = "no"
        has_cpt = "no"
        if category == "daily_note":
            dn = dn_by_file.get(filename, {})
            in_dn = "yes" if dn else "no"
            dn_id = dn.get("daily_note_id", "") or daily_note_id_from_filename(filename)
            if dn.get("cpt_summary") or dn_id in cpt_dn_ids:
                has_cpt = "yes"

        referral_icd = ""
        if category == "referral":
            ref = ref_by_key.get((patient_id, filename), {})
            referral_icd = ref.get("icd_codes", "")

        if in_ocr == "no":
            status = "missing_from_export"
        elif ocr_error == "no text extracted" or (
            ocr_error and ocr_error != "no text extracted"
        ):
            status = "no_text" if ocr_error == "no text extracted" else "ocr_error"
        elif category == "daily_note" and has_cpt == "no":
            status = "no_cpt"
        elif category == "referral" and not referral_icd and in_ocr == "yes":
            status = "no_icd_referral"
        else:
            status = "ok"

        status_counts[status] = status_counts.get(status, 0) + 1
        report_rows.append(
            {
                "patient_id": patient_id,
                "rel_path": rel_path,
                "doc_category": category,
                "on_disk": "yes",
                "in_ocr_csv": in_ocr,
                "in_daily_notes_csv": in_dn,
                "has_cpt_lines": has_cpt,
                "extraction_method": extraction_method,
                "text_chars": text_chars,
                "ocr_error": ocr_error,
                "referral_icd_codes": referral_icd,
                "status": status,
            }
        )

    report_path = extracted_dir / "validation_report.csv"
    _write_csv(report_path, report_rows, VALIDATION_REPORT_FIELDNAMES)

    summary = {
        "disk_patients": len({d.name for d in edocs_dir.iterdir() if d.is_dir()}),
        "disk_files": len(disk_files),
        "ocr_csv_files": len(ocr_rows),
        "daily_notes_csv": len(dn_rows),
        "cpt_lines": len(cpt_rows),
        "status_counts": status_counts,
        "validation_report_path": str(report_path),
    }
    log.info(
        "Validation: disk %d files, ocr csv %d | status: %s",
        len(disk_files),
        len(ocr_rows),
        status_counts,
    )
    return summary
