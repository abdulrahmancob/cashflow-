"""Tests for scheduler date range, title parse, case preference, and export repair."""
from __future__ import annotations

from datetime import date

from export_utils import (
    merge_pass_summary_fields,
    patients_export_filename_from_input,
    repair_patient_export_row,
)
from scheduler_api import (
    extract_patients_from_events,
    parse_patient_title,
    reclassify_appointment_dates,
    resolve_date_range,
)


def test_resolve_date_range_as_of_independent_of_end_date():
    start, end, ref = resolve_date_range(
        days=10,
        end_date=date(2026, 9, 30),
        timezone="US/Eastern",
        lookahead_days=0,
        as_of=date(2026, 7, 21),
    )
    assert start == date(2026, 9, 21)
    assert end == date(2026, 9, 30)
    assert ref == date(2026, 7, 21)


def test_resolve_date_range_default_as_of_is_today_not_end_date(monkeypatch):
    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            from zoneinfo import ZoneInfo

            return datetime(2026, 7, 21, 12, 0, 0, tzinfo=ZoneInfo("US/Eastern"))

    import scheduler_api as sa

    monkeypatch.setattr(sa, "datetime", _FixedDateTime)
    start, end, ref = resolve_date_range(
        days=3,
        end_date=date(2026, 9, 30),
        timezone="US/Eastern",
        lookahead_days=0,
        as_of=None,
    )
    assert end == date(2026, 9, 30)
    assert ref == date(2026, 7, 21)
    assert start == date(2026, 9, 28)


def test_parse_patient_title_collections_and_missing_dob():
    name, dob, case = parse_patient_title(
        "Reis, Claudette S. - 04/07/1948 - (L.Shoulder) *COLLECTIONS*"
    )
    assert name == "Reis, Claudette S."
    assert dob == "04/07/1948"
    assert case == "(L.Shoulder)"

    name2, dob2, case2 = parse_patient_title("Ali, Abdulmalek - (R shoulder)")
    assert name2 == "Ali, Abdulmalek"
    assert dob2 == ""
    assert case2 == "(R shoulder)"


def test_extract_prefers_latest_case_and_unique_counts():
    events = [
        {
            "p_id": 1,
            "case_id": 100,
            "title": "Doe, Jane - 01/01/1990 - (Old)",
            "ins_name": "Aetna",
            "start_date": "2026-07-01 10:00:00",
        },
        {
            "p_id": 1,
            "case_id": 100,
            "title": "Doe, Jane - 01/01/1990 - (Old)",
            "ins_name": "Aetna",
            "start_date": "2026-07-01 10:00:00",  # duplicate slot
        },
        {
            "p_id": 1,
            "case_id": 200,
            "title": "Doe, Jane - 01/01/1990 - (New)",
            "ins_name": "Cigna",
            "start_date": "2026-07-20 11:00:00",
        },
        {
            "p_id": 1,
            "case_id": 200,
            "title": "Doe, Jane - 01/01/1990 - (New)",
            "ins_name": "Cigna",
            "start_date": "2026-07-25 09:00:00",
        },
    ]
    patients = extract_patients_from_events(
        events, facility_id=1, reference_date=date(2026, 7, 21)
    )
    assert len(patients) == 1
    p = patients[0]
    assert p.case_id == 200
    assert p.ins_name == "Cigna"
    assert p.appointment_count == 3
    assert len(p.appointment_dates) == 3
    assert p.appointments_past_count == 2
    assert p.appointments_upcoming_count == 1
    assert p.appointments_upcoming_dates == ["2026-07-25 09:00:00"]


def test_reclassify_appointment_dates():
    past, up, pn, un = reclassify_appointment_dates(
        [
            "2026-07-20 10:00:00",
            "2026-07-21 10:00:00",
            "2026-07-22 10:00:00",
            "2026-07-20 10:00:00",
        ],
        reference_date=date(2026, 7, 21),
    )
    assert pn == 1
    assert un == 2
    assert past == ["2026-07-20 10:00:00"]
    assert up == ["2026-07-21 10:00:00", "2026-07-22 10:00:00"]


def test_merge_pass_summary_preserves_edoc_complete():
    prior = {
        "edoc_status": "complete",
        "edoc_files_total": 5,
        "edoc_files_downloaded": 5,
        "edoc_files_skipped": 0,
        "edoc_files_failed": 0,
        "edoc_errors": "",
        "chart_notes_status": "pending",
        "edoc_ocr_name": "Smith",
    }
    new = {
        "edoc_status": "pending",
        "edoc_files_total": 0,
        "edoc_files_downloaded": 0,
        "edoc_files_skipped": 0,
        "edoc_files_failed": 0,
        "edoc_errors": "",
        "chart_notes_status": "complete",
        "chart_notes_total": 3,
        "chart_notes_downloaded": 3,
        "chart_notes_skipped": 0,
        "chart_notes_failed": 0,
        "chart_notes_errors": "",
        "edoc_ocr_name": "",
    }
    merged = merge_pass_summary_fields(new, prior)
    assert merged["edoc_status"] == "complete"
    assert merged["edoc_files_total"] == 5
    assert merged["chart_notes_status"] == "complete"
    assert merged["edoc_ocr_name"] == "Smith"


def test_patients_export_filename_from_input():
    assert patients_export_filename_from_input("patients_export_273d.csv") == (
        "patients_export_273d.csv"
    )
    assert patients_export_filename_from_input("patients_recent_61d.csv") == (
        "patients_export_61d.csv"
    )
    assert patients_export_filename_from_input("patients.csv") == "patients_export.csv"


def test_repair_patient_export_row_reclassifies_and_fixes_title():
    row = {
        "patient_name": "Reis, Claudette S. - 04/07/1948 - (L.Shoulder) *COLLECTIONS*",
        "dob": "",
        "appointment_dates": "2026-07-01 10:00:00; 2026-07-25 10:00:00",
        "appointments_past_count": 2,
        "appointments_past_dates": "2026-07-01 10:00:00; 2026-07-25 10:00:00",
        "appointments_upcoming_count": 0,
        "appointments_upcoming_dates": "",
        "appointment_count": 2,
        "edoc_status": "pending",
    }
    fixed = repair_patient_export_row(
        row,
        reference_date=date(2026, 7, 21),
        edoc_summary={
            "edoc_status": "complete",
            "edoc_files_total": 2,
            "edoc_files_downloaded": 2,
            "edoc_files_skipped": 0,
            "edoc_files_failed": 0,
            "edoc_errors": "",
        },
    )
    assert fixed["patient_name"] == "Reis, Claudette S."
    assert fixed["dob"] == "04/07/1948"
    assert int(fixed["appointments_upcoming_count"]) == 1
    assert int(fixed["appointments_past_count"]) == 1
    assert fixed["edoc_status"] == "complete"


def test_patient_chart_nav_display_patients_is_not_wrong_clinic():
    """Chart pages include a Display Patients nav link; must still parse as chart."""
    from patient_chart_api import (
        _looks_like_display_patients,
        _looks_like_patient_chart,
        parse_patient_chart_html,
    )

    html = """
    <html><head><title>WebPT - Doe/J - Patient Record</title></head>
    <body>
      <a href="/displayPatients.php">Display Patients</a>
      <table>
        <tr><td><strong>Diagnosis:</strong></td><td>ICD10: M51.26: Disc</td></tr>
        <tr><td><strong>Additional Info:</strong></td>
            <td>Deductible : no Copay: 20 Limit/Year: no Referral required: yes</td></tr>
      </table>
    </body></html>
    """
    assert _looks_like_patient_chart("https://app.webpt.com/other.php", html)
    assert not _looks_like_display_patients("https://app.webpt.com/other.php", html)
    info = parse_patient_chart_html(html)
    assert "M51.26" in info.diagnosis
    assert info.copay == "20"

