"""Load WebPT extracted billing lines and patient enrichment."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .normalize import (
    format_date,
    name_key_from_webpt,
    parse_date,
    pick_first,
)

log = logging.getLogger(__name__)


@dataclass
class PatientEnrichment:
    patient_id: str
    patient_name: str = ""
    dob: str = ""
    case_id: str = ""
    facility_id: str = ""
    facility_name: str = ""
    ins_name: str = ""
    diagnosis: str = ""
    deductible: str = ""
    copay: str = ""
    limit_per_year: str = ""
    referral_required: str = ""
    assigned_therapist: str = ""
    auth_ins_visits: str = ""
    cancel_no_show: str = ""
    visits_in_case: str = ""
    edoc_ocr_name_match: str = ""
    edoc_ocr_id_match: str = ""


@dataclass
class WebptLine:
    patient_id: str
    daily_note_id: str
    patient_name: str
    name_key: str
    date_of_service: str
    cpt_code: str
    modifier: str
    units: str
    description: str
    insurance_note: str
    visit_no: str = ""
    diagnosis_icd_codes: str = ""
    note_file: str = ""
    dob: str = ""
    case_id: str = ""
    facility_id: str = ""
    facility_name: str = ""
    ins_name: str = ""
    diagnosis: str = ""
    expected_deductible: str = ""
    expected_copay: str = ""
    limit_per_year: str = ""
    referral_required: str = ""
    assigned_therapist: str = ""
    auth_ins_visits: str = ""
    cancel_no_show: str = ""
    visits_in_case: str = ""
    edoc_ocr_name_match: str = ""
    edoc_ocr_id_match: str = ""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_patients_export(path: Path) -> dict[str, PatientEnrichment]:
    if not path.exists():
        log.warning("Patients export not found: %s", path)
        return {}

    patients: dict[str, PatientEnrichment] = {}
    for row in _read_csv(path):
        patient_id = str(row.get("patient_id") or "").strip()
        if not patient_id:
            continue
        dob = format_date(parse_date(row.get("dob"))) or format_date(
            parse_date(row.get("date_of_birth"))
        )
        patients[patient_id] = PatientEnrichment(
            patient_id=patient_id,
            patient_name=str(row.get("patient_name") or "").strip(),
            dob=dob,
            case_id=str(row.get("case_id") or "").strip(),
            facility_id=str(row.get("facility_id") or "").strip(),
            facility_name=str(row.get("facility_name") or "").strip(),
            ins_name=str(row.get("ins_name") or "").strip(),
            diagnosis=str(row.get("diagnosis") or "").strip(),
            deductible=str(row.get("deductible") or "").strip(),
            copay=str(row.get("copay") or "").strip(),
            limit_per_year=str(row.get("limit_per_year") or "").strip(),
            referral_required=str(row.get("referral_required") or "").strip(),
            assigned_therapist=str(row.get("assigned_therapist") or "").strip(),
            auth_ins_visits=str(row.get("auth_ins_visits") or "").strip(),
            cancel_no_show=str(row.get("cancel_no_show") or "").strip(),
            visits_in_case=str(row.get("visits_in_case") or "").strip(),
            edoc_ocr_name_match=str(row.get("edoc_ocr_name_match") or "").strip(),
            edoc_ocr_id_match=str(row.get("edoc_ocr_id_match") or "").strip(),
        )
    return patients


def load_daily_notes_index(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    index: dict[str, dict[str, str]] = {}
    for row in _read_csv(path):
        note_id = str(row.get("daily_note_id") or "").strip()
        if note_id:
            index[note_id] = row
    return index


def load_webpt_lines(
    webpt_dir: Path,
    *,
    patients_export_path: Path | None = None,
    service_from: date | None = None,
    service_to: date | None = None,
) -> list[WebptLine]:
    cpt_path = webpt_dir / "cpt_codes.csv"
    daily_notes_path = webpt_dir / "daily_notes.csv"
    if not cpt_path.exists():
        raise FileNotFoundError(f"Missing cpt_codes.csv in {webpt_dir}")

    if patients_export_path is None:
        parent = webpt_dir.parent
        candidates = sorted(parent.glob("patients_export*.csv"))
        patients_export_path = candidates[0] if candidates else None

    patients = (
        load_patients_export(patients_export_path)
        if patients_export_path is not None
        else {}
    )
    notes = load_daily_notes_index(daily_notes_path)

    lines: list[WebptLine] = []
    missing_patient_ids: set[str] = set()

    for row in _read_csv(cpt_path):
        dos = parse_date(row.get("date_of_daily_note"))
        if dos is None:
            continue
        if service_from and dos < service_from:
            continue
        if service_to and dos > service_to:
            continue

        patient_id = str(row.get("patient_id") or "").strip()
        patient_name = str(row.get("patient_name") or "").strip()
        note_id = str(row.get("daily_note_id") or "").strip()
        note = notes.get(note_id, {})
        enrichment = patients.get(patient_id)
        if enrichment is None and patient_id:
            missing_patient_ids.add(patient_id)

        dob = ""
        if enrichment and enrichment.dob:
            dob = enrichment.dob
        else:
            dob = format_date(parse_date(note.get("date_of_birth")))

        insurance_note = str(row.get("insurance_name") or note.get("insurance_name") or "").strip()

        lines.append(
            WebptLine(
                patient_id=patient_id,
                daily_note_id=note_id,
                patient_name=patient_name,
                name_key=name_key_from_webpt(patient_name),
                date_of_service=format_date(dos),
                cpt_code=str(row.get("cpt_code") or "").strip(),
                modifier=str(row.get("modifier") or "").strip(),
                units=str(row.get("units") or "").strip(),
                description=str(row.get("description") or "").strip(),
                insurance_note=insurance_note,
                visit_no=str(note.get("visit_no") or row.get("visit_no") or "").strip(),
                diagnosis_icd_codes=str(
                    row.get("diagnosis_icd_codes")
                    or note.get("diagnosis_icd_codes")
                    or ""
                ).strip(),
                note_file=str(row.get("note_file") or note.get("note_file") or "").strip(),
                dob=dob,
                case_id=pick_first(enrichment.case_id if enrichment else ""),
                facility_id=pick_first(enrichment.facility_id if enrichment else ""),
                facility_name=pick_first(enrichment.facility_name if enrichment else ""),
                ins_name=pick_first(
                    enrichment.ins_name if enrichment else "",
                    insurance_note,
                ),
                diagnosis=pick_first(
                    enrichment.diagnosis if enrichment else "",
                    note.get("diagnosis_raw"),
                ),
                expected_deductible=pick_first(enrichment.deductible if enrichment else ""),
                expected_copay=pick_first(enrichment.copay if enrichment else ""),
                limit_per_year=pick_first(enrichment.limit_per_year if enrichment else ""),
                referral_required=pick_first(enrichment.referral_required if enrichment else ""),
                assigned_therapist=pick_first(enrichment.assigned_therapist if enrichment else ""),
                auth_ins_visits=pick_first(enrichment.auth_ins_visits if enrichment else ""),
                cancel_no_show=pick_first(enrichment.cancel_no_show if enrichment else ""),
                visits_in_case=pick_first(enrichment.visits_in_case if enrichment else ""),
                edoc_ocr_name_match=pick_first(enrichment.edoc_ocr_name_match if enrichment else ""),
                edoc_ocr_id_match=pick_first(enrichment.edoc_ocr_id_match if enrichment else ""),
            )
        )

    if missing_patient_ids:
        log.warning(
            "%d patient_id(s) missing from patients export (first 5): %s",
            len(missing_patient_ids),
            sorted(missing_patient_ids)[:5],
        )

    return lines
