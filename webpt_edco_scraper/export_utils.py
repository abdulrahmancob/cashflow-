import re
from datetime import date
from pathlib import Path
from typing import Any

from scheduler_api import (
    CheckoutVisit,
    SchedulerPatient,
    parse_patient_title,
    reclassify_appointment_dates,
)

_DAYS_IN_STEM = re.compile(r"(\d+)d", re.IGNORECASE)

EDOC_SUMMARY_KEYS = (
    "edoc_status",
    "edoc_files_total",
    "edoc_files_downloaded",
    "edoc_files_skipped",
    "edoc_files_failed",
    "edoc_errors",
)
CHART_NOTES_SUMMARY_KEYS = (
    "chart_notes_status",
    "chart_notes_total",
    "chart_notes_downloaded",
    "chart_notes_skipped",
    "chart_notes_failed",
    "chart_notes_errors",
)
OCR_SUMMARY_KEYS = (
    "edoc_ocr_name",
    "edoc_ocr_name_match",
    "edoc_ocr_patient_id",
    "edoc_ocr_id_match",
    "edoc_ocr_diagnosis",
    "edoc_ocr_diagnosis_match",
    "edoc_ocr_source_files",
    "edoc_ocr_file_hints",
    "edoc_ocr_errors",
    # Extended PDF/OCR fields (Phase 4–5)
    "edoc_ocr_physician",
    "edoc_ocr_npi",
    "edoc_ocr_dob",
    "edoc_ocr_insurance",
    "edoc_ocr_frequency",
    "edoc_ocr_visits",
    "edoc_ocr_poc_date",
    "edoc_ocr_certification",
    "edoc_ocr_goals",
    "edoc_ocr_precautions",
    "edoc_ocr_rom",
    "edoc_ocr_pain",
    "edoc_ocr_signature",
    "edoc_ocr_icd_codes",
)

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
        "edoc_ocr_physician": "",
        "edoc_ocr_npi": "",
        "edoc_ocr_dob": "",
        "edoc_ocr_insurance": "",
        "edoc_ocr_frequency": "",
        "edoc_ocr_visits": "",
        "edoc_ocr_poc_date": "",
        "edoc_ocr_certification": "",
        "edoc_ocr_goals": "",
        "edoc_ocr_precautions": "",
        "edoc_ocr_rom": "",
        "edoc_ocr_pain": "",
        "edoc_ocr_signature": "",
        "edoc_ocr_icd_codes": "",
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
    "edoc_ocr_physician",
    "edoc_ocr_npi",
    "edoc_ocr_dob",
    "edoc_ocr_insurance",
    "edoc_ocr_frequency",
    "edoc_ocr_visits",
    "edoc_ocr_poc_date",
    "edoc_ocr_certification",
    "edoc_ocr_goals",
    "edoc_ocr_precautions",
    "edoc_ocr_rom",
    "edoc_ocr_pain",
    "edoc_ocr_signature",
    "edoc_ocr_icd_codes",
]

PATIENT_RECENT_FIELDNAMES = PATIENT_EXPORT_FIELDNAMES

CHECKOUT_EXPORT_FIELDNAMES = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "case_id",
    "case_label",
    "appointment_at",
    "visit_status",
    "checkin_time",
    "checkout_time",
    "ins_name",
    "auth_ins_visits",
    "copay",
    "deductible",
]

# Same columns as checkout export; used for full-range schedule dumps.
SCHEDULE_EXPORT_FIELDNAMES = CHECKOUT_EXPORT_FIELDNAMES


def build_checkout_export_row(
    *,
    clinic_name: str,
    visit: CheckoutVisit,
    chart_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Lean CSV row for one schedule visit (chart fields optional)."""
    chart_fields = chart_fields or {}
    return {
        "facility_id": visit.facility_id,
        "facility_name": clinic_name,
        "patient_id": visit.patient_id,
        "patient_name": visit.patient_name,
        "case_id": visit.case_id or "",
        "case_label": visit.case_label,
        "appointment_at": visit.appointment_at,
        "visit_status": visit.visit_status,
        "checkin_time": visit.checkin_time,
        "checkout_time": visit.checkout_time,
        "ins_name": visit.ins_name,
        "auth_ins_visits": chart_fields.get("auth_ins_visits", ""),
        "copay": chart_fields.get("copay", ""),
        "deductible": chart_fields.get("deductible", ""),
    }


build_schedule_export_row = build_checkout_export_row


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

Resume / rescan
---------------
checkpoint.json tracks:
  completed_facilities  - progress hint only (default runs ALWAYS re-query scheduler)
  processed_patient_ids - patients already downloaded (facility_id:patient_id)

By default, finished clinics are re-scanned for NEW appointments. Old patients are
skipped via processed_patient_ids + --skip-existing PDFs.

Discovery-only (--skip-edocs --skip-chart-notes) does NOT mark processed_patient_ids —
those rows stay pending for parallel-download.

  --rescan-facilities           Clear completed_facilities at start of this run
  --skip-completed-facilities   Old behavior: never re-open clinics in checkpoint
  --skip-ocr                    Recommended during download; run ocr-all later
  --no-parallel-pdfs            Disable concurrent PDF downloads (slower)

Date window vs past/upcoming
----------------------------
  --end-date YYYY-MM-DD   End of the scheduler fetch window (lookback ends here)
  --as-of YYYY-MM-DD      Past/upcoming cutoff (default: today US/Eastern)
  --lookahead-days N      Days AFTER the window end to also fetch

Do NOT use --end-date as "today" for classification. Historical dumps that should
mark everything past must pass --as-of equal to the window end explicitly.

  python scraper.py --headless export-recent-appointments ^
    --days 61 --lookahead-days 0 --end-date 2026-07-31 --as-of 2026-07-21 ^
    --rescan-facilities --skip-edocs --skip-chart-notes --skip-ocr ^
    --output output/jun_jul_2026

Omit --skip-chart if you need deductible/copay/diagnosis in the CSV (those come
from patientChart.php). Discovery with --skip-chart leaves those columns blank;
run enrich-patient-export or repair-patient-export later.

Offline repair (no WebPT login):
  python scraper.py repair-patient-export ^
    --input output/jun_jul_2026/patients_export_10d.csv ^
    --output output/jun_jul_2026 --as-of 2026-07-21

patient_id is WebPT EMR ID (scheduler p_id). Verify in the browser via:
  https://app.webpt.com/patientChart.php?ID={patient_id}&CaseID={case_id}
Name-box search often fails; do not confuse case_id with patient_id.

Fast two-pass download (recommended — usable eDocs first, chart notes later):
  set WEBPT_MAX_CONCURRENT_PDFS=24

  python scraper.py --headless parallel-download ^
    --input output/jun_jul_2026/patients_export_61d.csv ^
    --output output/jun_jul_2026 --skip-chart-notes

  python scraper.py --headless parallel-download ^
    --input output/jun_jul_2026/patients_export_61d.csv ^
    --output output/jun_jul_2026 --skip-edocs

Two-pass runs preserve the other pass's edoc/chart/OCR columns (no wipe to pending).
Output CSV is named from the input stem (e.g. patients_export_61d.csv), not hardcoded 10d.

Or full pass (eDocs + chart notes together):
  python scraper.py --headless parallel-download ^
    --input output/jun_jul_2026/patients_export_61d.csv ^
    --output output/jun_jul_2026

parallel-download uses 1 patient worker (WebPT single-session). Speed comes from
concurrent PDF HTTP downloads (WEBPT_MAX_CONCURRENT_PDFS, default 24) and HTTP
chart-notes listing — not --workers. --workers > 1 is ignored/clamped to 1.

If headless login fails (delegator / Auth0 / expired storage_state), refresh once:
  python scraper.py login --fresh-login
  Then re-run parallel-download with --headless.

Edocs-only (--skip-chart-notes) does NOT mark download_checkpoint done, so the
chart-notes second pass still sees those patients. --skip-existing skips PDFs
already on disk.

parallel-download also skips keys already in checkpoint.json processed_patient_ids.

edocs_manifest.csv (per PDF file)
--------------------------------
doc_source - edoc (external document) or chart_note (signed clinical note PDF)
ok       - PDF downloaded successfully (new file)
skipped  - File already exists on disk (--skip-existing default); NOT an error
no_docs  - Patient has no eDocs in WebPT (edoc rows only)
error    - Download failed; read the error column (403 WAF, not PDF, timeout, etc.)

patients_export_*.csv (per patient eDoc summary)
------------------------------------------------
complete - All eDocs downloaded or already present on disk
partial  - Some files failed; see edoc_errors column
failed   - Every eDoc download failed
no_docs  - Patient has no eDocs
pending  - eDocs not processed yet (discovery-only run)

Chart notes columns
-------------------
chart_notes_status   - complete/partial/failed/no_notes/no_case/pending
chart_notes_total    - Printable chart notes found for appointment CaseID
chart_notes_*        - Same download/skip/fail counts as eDocs
Chart note PDFs saved under edocs/{patient_id}/chart_notes/

OCR validation columns
----------------------
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
appointments_past_*     - Visits before --as-of (default today ET)
appointments_upcoming_* - --as-of and later visits
appointment_count       - Unique appointment datetimes in scheduler range
"""


def write_status_guide(output_dir) -> None:
    path = Path(output_dir) / "STATUS_GUIDE.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STATUS_GUIDE_TEXT, encoding="utf-8")


def patients_export_filename_from_input(input_csv: Path | str) -> str:
    """Derive patients_export_{N}d.csv from an input stem that contains Nd."""
    stem = Path(input_csv).stem
    match = _DAYS_IN_STEM.search(stem)
    if match:
        return f"patients_export_{match.group(1)}d.csv"
    return "patients_export.csv"


def _status_rank(status: str) -> int:
    """Higher = more informative / preferred when merging two-pass results."""
    order = {
        "complete": 50,
        "partial": 40,
        "failed": 30,
        "no_docs": 25,
        "no_notes": 25,
        "no_case": 20,
        "pending": 10,
        "": 0,
    }
    return order.get((status or "").strip().lower(), 5)


def merge_pass_summary_fields(
    new_row: dict[str, Any],
    prior_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the better edoc/chart/OCR fields when a two-pass run would wipe them."""
    if not prior_row:
        return new_row
    out = dict(new_row)

    if _status_rank(str(out.get("edoc_status") or "")) < _status_rank(
        str(prior_row.get("edoc_status") or "")
    ):
        for k in EDOC_SUMMARY_KEYS:
            if k in prior_row:
                out[k] = prior_row[k]

    if _status_rank(str(out.get("chart_notes_status") or "")) < _status_rank(
        str(prior_row.get("chart_notes_status") or "")
    ):
        for k in CHART_NOTES_SUMMARY_KEYS:
            if k in prior_row:
                out[k] = prior_row[k]

    new_ocr_empty = not any(str(out.get(k) or "").strip() for k in OCR_SUMMARY_KEYS)
    prior_ocr_filled = any(
        str(prior_row.get(k) or "").strip() for k in OCR_SUMMARY_KEYS
    )
    if new_ocr_empty and prior_ocr_filled:
        for k in OCR_SUMMARY_KEYS:
            if k in prior_row:
                out[k] = prior_row[k]

    return out


def _split_date_list(raw: str) -> list[str]:
    return [d.strip() for d in (raw or "").split(";") if d.strip()]


def repair_patient_export_row(
    row: dict[str, Any],
    *,
    reference_date: date,
    edoc_summary: dict[str, Any] | None = None,
    chart_notes_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Offline fix: reclassify appointments, reparse title junk, restore statuses."""
    out = dict(row)

    # Re-parse polluted patient_name / missing DOB from title-shaped strings.
    name_raw = str(out.get("patient_name") or "")
    dob_raw = str(out.get("dob") or "").strip()
    if (not dob_raw) or " - (" in name_raw or "*COLLECTIONS*" in name_raw.upper():
        parsed_name, parsed_dob, _case = parse_patient_title(name_raw)
        if parsed_name and (
            parsed_name != name_raw or (parsed_dob and not dob_raw)
        ):
            out["patient_name"] = parsed_name
        if parsed_dob and not dob_raw:
            out["dob"] = parsed_dob
        elif parsed_dob and not out.get("dob"):
            out["dob"] = parsed_dob

    dates = _split_date_list(str(out.get("appointment_dates") or ""))
    if not dates:
        dates = _split_date_list(str(out.get("appointments_past_dates") or ""))
        dates += _split_date_list(str(out.get("appointments_upcoming_dates") or ""))
    past, upcoming, past_n, up_n = reclassify_appointment_dates(
        dates, reference_date=reference_date
    )
    out["appointment_dates"] = "; ".join(past + upcoming)
    out["appointment_count"] = past_n + up_n
    out["appointments_past_dates"] = "; ".join(past)
    out["appointments_past_count"] = past_n
    out["appointments_upcoming_dates"] = "; ".join(upcoming)
    out["appointments_upcoming_count"] = up_n

    if edoc_summary and _status_rank(str(edoc_summary.get("edoc_status") or "")) > _status_rank(
        str(out.get("edoc_status") or "")
    ):
        out.update(edoc_summary)
    if chart_notes_summary and _status_rank(
        str(chart_notes_summary.get("chart_notes_status") or "")
    ) > _status_rank(str(out.get("chart_notes_status") or "")):
        out.update(chart_notes_summary)

    return out


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
