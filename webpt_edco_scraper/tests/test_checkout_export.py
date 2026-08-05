"""Tests for Checked Out visit extraction and status parsing."""
from __future__ import annotations

from datetime import date

from export_utils import (
    CHECKOUT_EXPORT_FIELDNAMES,
    SCHEDULE_EXPORT_FIELDNAMES,
    build_checkout_export_row,
)
from scheduler_api import (
    CheckoutVisit,
    extract_checkout_visits,
    extract_schedule_visits,
    is_checked_out,
    parse_visit_status,
)


def _event(
    *,
    p_id: int = 1,
    case_id: int = 100,
    title: str = "Doe, Jane - 01/01/1990 - (Neck)",
    start_date: str = "2026-07-23 10:00:00",
    status: int | None = 4,
    checkin_time: str | None = "9:50 am",
    checkout_time: str | None = "10:45 am",
    ins_name: str = "Aetna",
    appointment_id: int = 111,
) -> dict:
    return {
        "p_id": p_id,
        "case_id": case_id,
        "title": title,
        "start_date": start_date,
        "status": status,
        "checkin_time": checkin_time,
        "checkout_time": checkout_time,
        "ins_name": ins_name,
        "appointment_id": appointment_id,
    }


def test_is_checked_out_via_checkout_time():
    assert is_checked_out(_event(status=5, checkout_time="10:45 am"))
    assert not is_checked_out(_event(status=5, checkout_time=None))
    assert not is_checked_out(_event(status=6, checkin_time=None, checkout_time=None))


def test_is_checked_out_via_status_code_4():
    assert is_checked_out(_event(status=4, checkout_time=None))
    assert not is_checked_out(_event(status=5, checkout_time=None))


def test_parse_visit_status_labels():
    assert parse_visit_status(_event(status=4, checkout_time="1:00 pm")) == "Checked Out"
    assert (
        parse_visit_status(_event(status=5, checkin_time="9:00 am", checkout_time=None))
        == "Checked In"
    )
    assert (
        parse_visit_status(_event(status=6, checkin_time=None, checkout_time=None))
        == "Cancelled/No Show"
    )
    assert (
        parse_visit_status(_event(status=None, checkin_time=None, checkout_time=None))
        == "Other"
    )


def test_extract_checkout_visits_filters_date_status_and_case():
    events = [
        # Checked out yesterday — keep (case 200)
        _event(
            p_id=1,
            case_id=200,
            title="Doe, Jane - 01/01/1990 - (Neck '26)",
            start_date="2026-07-23 11:00:00",
            status=4,
            checkout_time="11:40 am",
        ),
        # Same patient older case, different day — drop
        _event(
            p_id=1,
            case_id=100,
            title="Doe, Jane - 01/01/1990 - (Hip)",
            start_date="2026-07-22 11:00:00",
            status=4,
            checkout_time="11:40 am",
        ),
        # Yesterday but checked in only — drop
        _event(
            p_id=2,
            case_id=300,
            title="Smith, Bob - 02/02/1980 - (Knee)",
            start_date="2026-07-23 09:00:00",
            status=5,
            checkin_time="8:50 am",
            checkout_time=None,
        ),
        # Yesterday cancelled — drop
        _event(
            p_id=3,
            case_id=400,
            title="Lee, Ann - 03/03/1970 - (Back)",
            start_date="2026-07-23 14:00:00",
            status=6,
            checkin_time=None,
            checkout_time=None,
        ),
        # Duplicate slot — keep once
        _event(
            p_id=1,
            case_id=200,
            title="Doe, Jane - 01/01/1990 - (Neck '26)",
            start_date="2026-07-23 11:00:00",
            status=4,
            checkout_time="11:40 am",
        ),
        # Non-patient block — drop
        {
            "p_id": 0,
            "case_id": 0,
            "title": "Lunch",
            "start_date": "2026-07-23 12:00:00",
            "status": 4,
            "checkout_time": "12:30 pm",
        },
    ]

    visits = extract_checkout_visits(
        events,
        facility_id=30874,
        service_date=date(2026, 7, 23),
    )
    assert len(visits) == 1
    v = visits[0]
    assert v.patient_id == 1
    assert v.facility_id == 30874
    assert v.case_id == 200
    assert v.case_label == "(Neck '26)"
    assert v.appointment_at == "2026-07-23 11:00:00"
    assert v.visit_status == "Checked Out"
    assert v.checkout_time == "11:40 am"


def test_extract_keeps_facility_case_of_yesterday_visit_only():
    """Multi-case patient: only yesterday's checked-out case is returned."""
    events = [
        _event(
            p_id=9,
            case_id=111,
            title="Pat, Multi - 05/05/1955 - (Old Case)",
            start_date="2026-07-20 10:00:00",
            status=4,
            checkout_time="10:30 am",
        ),
        _event(
            p_id=9,
            case_id=222,
            title="Pat, Multi - 05/05/1955 - (Treated Yesterday)",
            start_date="2026-07-23 15:00:00",
            status=4,
            checkout_time="15:40 pm",
        ),
    ]
    visits = extract_checkout_visits(
        events, facility_id=1, service_date=date(2026, 7, 23)
    )
    assert len(visits) == 1
    assert visits[0].case_id == 222
    assert visits[0].case_label == "(Treated Yesterday)"


def test_build_checkout_export_row_columns():
    visit = CheckoutVisit(
        patient_id=42,
        facility_id=30874,
        case_id=7147,
        case_label="(Neck '26)",
        patient_name="Doe, Jane",
        dob="01/01/1990",
        ins_name="Aetna",
        appointment_at="2026-07-23 11:00:00",
        visit_status="Checked Out",
        checkin_time="10:50 am",
        checkout_time="11:40 am",
    )
    row = build_checkout_export_row(
        clinic_name="Allerton",
        visit=visit,
        chart_fields={
            "auth_ins_visits": "26 of 30 Authorized (Expires 12/31/2026)",
            "copay": "no",
            "deductible": "no",
        },
    )
    assert list(row.keys()) == CHECKOUT_EXPORT_FIELDNAMES
    assert list(row.keys()) == SCHEDULE_EXPORT_FIELDNAMES
    assert row["facility_name"] == "Allerton"
    assert row["patient_id"] == 42
    assert row["case_id"] == 7147
    assert row["case_label"] == "(Neck '26)"
    assert row["auth_ins_visits"].startswith("26 of 30")
    assert row["copay"] == "no"
    assert row["deductible"] == "no"


def test_extract_schedule_visits_all_statuses_in_range():
    events = [
        _event(
            p_id=1,
            case_id=200,
            start_date="2026-07-23 11:00:00",
            status=4,
            checkout_time="11:40 am",
        ),
        _event(
            p_id=2,
            case_id=300,
            title="Smith, Bob - 02/02/1980 - (Knee)",
            start_date="2026-07-23 09:00:00",
            status=5,
            checkin_time="8:50 am",
            checkout_time=None,
        ),
        _event(
            p_id=3,
            case_id=400,
            title="Lee, Ann - 03/03/1970 - (Back)",
            start_date="2026-07-23 14:00:00",
            status=6,
            checkin_time=None,
            checkout_time=None,
        ),
        # Outside range
        _event(
            p_id=4,
            case_id=500,
            start_date="2026-08-01 10:00:00",
            status=4,
            checkout_time="10:30 am",
        ),
    ]
    visits = extract_schedule_visits(
        events,
        facility_id=30874,
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
        checked_out_only=False,
    )
    assert len(visits) == 3
    by_pid = {v.patient_id: v for v in visits}
    assert by_pid[1].visit_status == "Checked Out"
    assert by_pid[2].visit_status == "Checked In"
    assert by_pid[2].checkin_time == "8:50 am"
    assert by_pid[3].visit_status == "Cancelled/No Show"
