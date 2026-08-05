"""Case-centric selection rules and migration presence (no Postgres required)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from cashflow_db.loaders.base import (
    AppointmentCandidate,
    appointment_from_schedule_row,
    map_visit_status,
    normalize_payment_category,
    select_clinical_appointment,
)
from cashflow_db.loaders.load_schedule import load_schedule


def _cand(
    hour: int,
    minute: int = 0,
    *,
    status: str = "completed",
    status_raw: str = "Checked Out",
    span_min: float | None = 60,
    has_note: bool = False,
    has_cpt: bool = False,
) -> AppointmentCandidate:
    appt = datetime(2026, 4, 8, hour, minute, 0)
    check_in = appt
    check_out = None
    if span_min is not None:
        check_out = datetime(
            2026, 4, 8, hour, minute, 0
        ) + __import__("datetime").timedelta(minutes=span_min)
    return AppointmentCandidate(
        appointment_at=appt,
        service_date=date(2026, 4, 8),
        status_raw=status_raw,
        status=status,
        check_in_at=check_in,
        check_out_at=check_out,
        has_daily_note=has_note,
        has_cpt=has_cpt,
        case_webpt_id="CASE1",
        patient_webpt_id="P1",
        facility_webpt_id="F1",
    )


def test_map_visit_status():
    assert map_visit_status("Checked Out") == "completed"
    assert map_visit_status("Checked In") == "unchecked_out"
    assert map_visit_status("Cancelled/No Show") == "no_show"
    assert map_visit_status("1") == "scheduled"


def test_select_prefers_checked_out_over_checked_in():
    winner = select_clinical_appointment(
        [
            _cand(9, status="unchecked_out", status_raw="Checked In", span_min=90),
            _cand(11, status="completed", status_raw="Checked Out", span_min=30),
        ]
    )
    assert winner is not None
    assert winner.appointment_at.hour == 11


def test_select_prefers_note_then_cpt_then_longest_then_earliest():
    # Two checked out: note wins over longer span without note
    winner = select_clinical_appointment(
        [
            _cand(9, span_min=120, has_note=False, has_cpt=False),
            _cand(14, span_min=20, has_note=True, has_cpt=False),
        ]
    )
    assert winner is not None
    assert winner.appointment_at.hour == 14

    # Same note flag: CPT wins
    winner = select_clinical_appointment(
        [
            _cand(9, span_min=120, has_note=True, has_cpt=False),
            _cand(14, span_min=20, has_note=True, has_cpt=True),
        ]
    )
    assert winner is not None
    assert winner.appointment_at.hour == 14

    # Same docs: longest span
    winner = select_clinical_appointment(
        [
            _cand(9, span_min=30, has_note=True, has_cpt=True),
            _cand(14, span_min=90, has_note=True, has_cpt=True),
        ]
    )
    assert winner is not None
    assert winner.appointment_at.hour == 14

    # Same span: earliest
    winner = select_clinical_appointment(
        [
            _cand(14, span_min=60, has_note=True, has_cpt=True),
            _cand(9, span_min=60, has_note=True, has_cpt=True),
        ]
    )
    assert winner is not None
    assert winner.appointment_at.hour == 9


def test_same_day_multi_appt_one_clinical_winner():
    """Chained two checkouts → one clinical visit selection."""
    morning = _cand(8, span_min=85)
    trailing = _cand(10, span_min=20)
    winner = select_clinical_appointment([morning, trailing])
    assert winner is morning


def test_appointment_from_schedule_row_rejects_blank_case():
    row = {
        "facility_id": "1",
        "patient_id": "2",
        "case_id": "",
        "appointment_at": "2026-01-09 10:00:00",
        "visit_status": "Checked Out",
    }
    assert appointment_from_schedule_row(row) is None


def test_appointment_from_schedule_row_ok():
    row = {
        "facility_id": "30874",
        "facility_name": "Allerton",
        "patient_id": "53257728",
        "patient_name": "Test, Patient",
        "case_id": "67548668",
        "appointment_at": "2026-01-06 10:30:00",
        "visit_status": "Checked In",
        "checkin_time": "10:35 am",
        "checkout_time": "",
        "ins_name": "BCBS",
        "appointment_id": "",
    }
    cand = appointment_from_schedule_row(row)
    assert cand is not None
    assert cand.case_webpt_id == "67548668"
    assert cand.status == "unchecked_out"
    assert cand.service_date == date(2026, 1, 6)
    assert cand.check_in_at is not None


def test_normalize_payment_category():
    assert normalize_payment_category("Copay") == "Copay"
    assert normalize_payment_category("Internal Payment") == "Internal Payment"
    assert normalize_payment_category("Refund") is None


def test_sql_012_present():
    sql_dir = Path(__file__).resolve().parents[1] / "sql"
    text = (sql_dir / "012_case_centric.sql").read_text(encoding="utf-8")
    assert "core.schedule_appointment" in text
    assert "billing.patient_payment" in text
    assert "analytics.snowflake_visit_kpi" in text
    assert "mart.v_sf_vs_case_coverage" in text
    assert "mart.v_schedule_appointment_facts" in text


def test_fail_closed_blank_case_id():
    """Blank case_id never becomes an AppointmentCandidate (fail-closed)."""
    assert (
        appointment_from_schedule_row(
            {
                "facility_id": "1",
                "patient_id": "100",
                "case_id": "",
                "appointment_at": "2026-01-01 10:00:00",
                "visit_status": "Checked Out",
            }
        )
        is None
    )
    assert callable(load_schedule)
