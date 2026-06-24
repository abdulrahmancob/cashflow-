from typing import Any

from scheduler_api import SchedulerPatient

EDOC_STATUS_DESCRIPTIONS: dict[str, str] = {
    "ok": "Downloaded successfully (new file)",
    "skipped": "Already on disk (--skip-existing); not an error",
    "no_docs": "Patient has no eDocs in WebPT",
    "error": "Download failed; see error column",
}

PATIENT_EDOC_STATUS_DESCRIPTIONS: dict[str, str] = {
    "complete": "All eDocs downloaded or already present",
    "partial": "Some eDocs failed; see edoc_errors",
    "failed": "All eDoc downloads failed",
    "no_docs": "No eDocs found for patient",
    "pending": "eDocs not processed yet",
}

CHART_NOTE_STATUS_DESCRIPTIONS: dict[str, str] = {
    "complete": "All chart notes downloaded or already present",
    "partial": "Some chart notes failed; see chart_notes_errors",
    "failed": "All chart note downloads failed",
    "no_notes": "No printable chart notes found for case",
    "no_case": "No case_id from scheduler; chart notes skipped",
    "pending": "Chart notes not processed yet",
}


def describe_edoc_file_status(status: str) -> str:
    return EDOC_STATUS_DESCRIPTIONS.get(status, status)


def describe_chart_note_file_status(status: str) -> str:
    return EDOC_STATUS_DESCRIPTIONS.get(status, status)


def summarize_chart_notes_downloads(
    *,
    notes_count: int,
    results: list[dict[str, Any]] | None,
    processed: bool,
    no_case: bool = False,
) -> dict[str, Any]:
    if no_case:
        return {
            "chart_notes_status": "no_case",
            "chart_notes_total": 0,
            "chart_notes_downloaded": 0,
            "chart_notes_skipped": 0,
            "chart_notes_failed": 0,
            "chart_notes_errors": "",
        }
    if not processed:
        return {
            "chart_notes_status": "pending",
            "chart_notes_total": notes_count,
            "chart_notes_downloaded": 0,
            "chart_notes_skipped": 0,
            "chart_notes_failed": 0,
            "chart_notes_errors": "",
        }
    if notes_count == 0:
        return {
            "chart_notes_status": "no_notes",
            "chart_notes_total": 0,
            "chart_notes_downloaded": 0,
            "chart_notes_skipped": 0,
            "chart_notes_failed": 0,
            "chart_notes_errors": "",
        }

    results = results or []
    downloaded = sum(
        1 for r in results if r.get("downloaded") and not r.get("skipped")
    )
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = sum(1 for r in results if r.get("error") and not r.get("downloaded"))
    errors = [
        str(r.get("error"))
        for r in results
        if r.get("error") and not r.get("downloaded")
    ]

    if failed == 0:
        status = "complete"
    elif downloaded + skipped == 0:
        status = "failed"
    else:
        status = "partial"

    return {
        "chart_notes_status": status,
        "chart_notes_total": notes_count,
        "chart_notes_downloaded": downloaded,
        "chart_notes_skipped": skipped,
        "chart_notes_failed": failed,
        "chart_notes_errors": " | ".join(errors[:3]),
    }


def summarize_edoc_downloads(
    *,
    docs_count: int,
    results: list[dict[str, Any]] | None,
    processed: bool,
) -> dict[str, Any]:
    if not processed:
        return {
            "edoc_status": "pending",
            "edoc_files_total": docs_count,
            "edoc_files_downloaded": 0,
            "edoc_files_skipped": 0,
            "edoc_files_failed": 0,
            "edoc_errors": "",
        }
    if docs_count == 0:
        return {
            "edoc_status": "no_docs",
            "edoc_files_total": 0,
            "edoc_files_downloaded": 0,
            "edoc_files_skipped": 0,
            "edoc_files_failed": 0,
            "edoc_errors": "",
        }

    results = results or []
    downloaded = sum(
        1 for r in results if r.get("downloaded") and not r.get("skipped")
    )
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = sum(1 for r in results if r.get("error") and not r.get("downloaded"))
    errors = [
        str(r.get("error"))
        for r in results
        if r.get("error") and not r.get("downloaded")
    ]

    if failed == 0:
        status = "complete"
    elif downloaded + skipped == 0:
        status = "failed"
    else:
        status = "partial"

    return {
        "edoc_status": status,
        "edoc_files_total": docs_count,
        "edoc_files_downloaded": downloaded,
        "edoc_files_skipped": skipped,
        "edoc_files_failed": failed,
        "edoc_errors": " | ".join(errors[:3]),
    }


def build_patient_export_row(
    *,
    clinic_name: str,
    patient: SchedulerPatient,
    chart_fields: dict[str, str] | None = None,
    edoc_summary: dict[str, Any] | None = None,
    chart_notes_summary: dict[str, Any] | None = None,
    ocr_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chart_fields = chart_fields or {}
    edoc_summary = edoc_summary or summarize_edoc_downloads(
        docs_count=0, results=None, processed=False
    )
    chart_notes_summary = chart_notes_summary or summarize_chart_notes_downloads(
        notes_count=0, results=None, processed=False
    )
    ocr_summary = ocr_summary or empty_ocr_summary()
    row: dict[str, Any] = {
        "facility_id": patient.facility_id,
        "facility_name": clinic_name,
        "patient_id": patient.patient_id,
        "patient_name": patient.patient_name,
        "dob": patient.dob,
        "case_id": patient.case_id or "",
        "ins_name": patient.ins_name,
        "appointments_past_count": patient.appointments_past_count,
        "appointments_past_dates": "; ".join(patient.appointments_past_dates),
        "appointments_upcoming_count": patient.appointments_upcoming_count,
        "appointments_upcoming_dates": "; ".join(patient.appointments_upcoming_dates),
        "appointment_count": patient.appointment_count,
        "appointment_dates": "; ".join(patient.appointment_dates),
    }
    row.update(chart_fields)
    row.update(edoc_summary)
    row.update(chart_notes_summary)
    row.update(ocr_summary)
    return row


def empty_ocr_summary(*, error: str = "") -> dict[str, Any]:
    return {
        "edoc_ocr_name": "",
        "edoc_ocr_name_match": "",
        "edoc_ocr_patient_id": "",
        "edoc_ocr_id_match": "",
        "edoc_ocr_diagnosis": "",
        "edoc_ocr_diagnosis_match": "",
        "edoc_ocr_source_files": "",
        "edoc_ocr_file_hints": "",
        "edoc_ocr_errors": error,
    }


PATIENT_EXPORT_FIELDNAMES = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "dob",
    "case_id",
    "ins_name",
    "appointments_past_count",
    "appointments_past_dates",
    "appointments_upcoming_count",
    "appointments_upcoming_dates",
    "appointment_count",
    "appointment_dates",
    "auth_ins_visits",
    "cancel_no_show",
    "visits_in_case",
    "assigned_therapist",
    "diagnosis",
    "deductible",
    "copay",
    "limit_per_year",
    "referral_required",
    "additional_info_raw",
    "edoc_status",
    "edoc_files_total",
    "edoc_files_downloaded",
    "edoc_files_skipped",
    "edoc_files_failed",
    "edoc_errors",
    "chart_notes_status",
    "chart_notes_total",
    "chart_notes_downloaded",
    "chart_notes_skipped",
    "chart_notes_failed",
    "chart_notes_errors",
    "edoc_ocr_name",
    "edoc_ocr_name_match",
    "edoc_ocr_patient_id",
    "edoc_ocr_id_match",
    "edoc_ocr_diagnosis",
    "edoc_ocr_diagnosis_match",
    "edoc_ocr_source_files",
    "edoc_ocr_file_hints",
    "edoc_ocr_errors",
]

PATIENT_RECENT_FIELDNAMES = PATIENT_EXPORT_FIELDNAMES

EDOC_MANIFEST_FIELDNAMES = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "doc_source",
    "ext_doc_id",
    "filename",
    "status",
    "status_description",
    "path",
    "error",
]

STATUS_GUIDE_TEXT = """WebPT Export Status Guide
=========================

edocs_manifest.csv (per PDF file)
--------------------------------
doc_source - edoc (external document) or chart_note (signed clinical note PDF)
ok       - PDF downloaded successfully (new file)
skipped  - File already exists on disk (--skip-existing default); NOT an error
no_docs  - Patient has no eDocs in WebPT (edoc rows only)
error    - Download failed; read the error column (403 WAF, not PDF, timeout, etc.)

patients_export_10d.csv (per patient eDoc summary)
--------------------------------------------------
complete - All eDocs downloaded or already present on disk
partial  - Some files failed; see edoc_errors column
failed   - Every eDoc download failed
no_docs  - Patient has no eDocs
pending  - eDocs not processed yet (discovery-only run)

Chart notes columns (patients_export_10d.csv)
---------------------------------------------
chart_notes_status   - complete/partial/failed/no_notes/no_case/pending
chart_notes_total    - Printable chart notes found for appointment CaseID
chart_notes_*        - Same download/skip/fail counts as eDocs
Chart note PDFs saved under edocs/{patient_id}/chart_notes/

OCR validation columns (patients_export_10d.csv)
------------------------------------------------
edoc_ocr_name              - Patient name extracted from merged eDoc OCR text
edoc_ocr_name_match        - yes/no: last + first name letters found in OCR
edoc_ocr_patient_id        - EMR/patient ID digits extracted from OCR
edoc_ocr_id_match          - yes/no: expected patient_id digits found in OCR
edoc_ocr_diagnosis         - ICD-10 codes found in OCR text
edoc_ocr_diagnosis_match   - yes/no: all chart ICD-10 codes found in OCR
edoc_ocr_source_files      - PDF filenames included in OCR merge
edoc_ocr_file_hints        - Per-file flags (last/first/id/icd) e.g. intake.pdf:last+first
edoc_ocr_errors            - OCR setup/extraction issues (blank if OK)

Appointment columns
-------------------
appointments_past_*     - Visits before today (last N days window)
appointments_upcoming_* - Today and future visits (next M days window)
appointment_count       - Total appointments in scheduler range (may repeat dates)
"""


def write_status_guide(output_dir) -> None:
    from pathlib import Path

    path = Path(output_dir) / "STATUS_GUIDE.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STATUS_GUIDE_TEXT, encoding="utf-8")


def edoc_manifest_row(
    *,
    facility_id: str,
    facility_name: str,
    patient_id: int,
    patient_name: str,
    doc_source: str = "edoc",
    ext_doc_id: str = "",
    filename: str = "",
    status: str = "",
    path: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "facility_id": facility_id,
        "facility_name": facility_name,
        "patient_id": patient_id,
        "patient_name": patient_name,
        "doc_source": doc_source,
        "ext_doc_id": ext_doc_id,
        "filename": filename,
        "status": status,
        "status_description": describe_edoc_file_status(status),
        "path": path,
        "error": error,
    }


def chart_note_manifest_row(
    *,
    facility_id: str,
    facility_name: str,
    patient_id: int,
    patient_name: str,
    note_id: str = "",
    filename: str = "",
    status: str = "",
    path: str = "",
    error: str = "",
) -> dict[str, Any]:
    return edoc_manifest_row(
        facility_id=facility_id,
        facility_name=facility_name,
        patient_id=patient_id,
        patient_name=patient_name,
        doc_source="chart_note",
        ext_doc_id=note_id,
        filename=filename,
        status=status,
        path=path,
        error=error,
    )


def aggregate_edoc_summary_from_manifest(
    rows: list[dict[str, Any]],
    *,
    patient_id: int,
    facility_id: str,
) -> dict[str, Any]:
    pid = str(patient_id)
    fid = str(facility_id)
    matched = [
        r
        for r in rows
        if str(r.get("patient_id")) == pid
        and str(r.get("facility_id")) == fid
        and (not r.get("doc_source") or r.get("doc_source") == "edoc")
    ]
    if not matched:
        return summarize_edoc_downloads(docs_count=0, results=None, processed=False)
    if len(matched) == 1 and matched[0].get("status") == "no_docs":
        return summarize_edoc_downloads(docs_count=0, results=None, processed=True)

    pseudo_results: list[dict[str, Any]] = []
    for r in matched:
        st = r.get("status", "")
        pseudo_results.append(
            {
                "downloaded": st in ("ok", "skipped"),
                "skipped": st == "skipped",
                "error": r.get("error") if st == "error" else None,
            }
        )
    return summarize_edoc_downloads(
        docs_count=len(matched),
        results=pseudo_results,
        processed=True,
    )


def aggregate_chart_notes_summary_from_manifest(
    rows: list[dict[str, Any]],
    *,
    patient_id: int,
    facility_id: str,
) -> dict[str, Any]:
    pid = str(patient_id)
    fid = str(facility_id)
    matched = [
        r
        for r in rows
        if str(r.get("patient_id")) == pid
        and str(r.get("facility_id")) == fid
        and r.get("doc_source") == "chart_note"
    ]
    if not matched:
        return summarize_chart_notes_downloads(
            notes_count=0, results=None, processed=False
        )

    pseudo_results: list[dict[str, Any]] = []
    for r in matched:
        st = r.get("status", "")
        pseudo_results.append(
            {
                "downloaded": st in ("ok", "skipped"),
                "skipped": st == "skipped",
                "error": r.get("error") if st == "error" else None,
            }
        )
    return summarize_chart_notes_downloads(
        notes_count=len(matched),
        results=pseudo_results,
        processed=True,
    )
