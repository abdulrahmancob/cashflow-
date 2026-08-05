"""Tests for offline unpaid split by upcoming schedule appointments."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.split_unpaid_by_upcoming import (  # noqa: E402
    best_phone,
    build_upcoming_by_patient,
    filter_copay_rows,
    load_patient_contacts,
    split_unpaid_by_upcoming,
)


def _unpaid(
    *,
    facility_id: str = "1",
    patient_id: str,
    patient_name: str = "Test",
    dos: str,
    payment_type: str = "Copay",
) -> dict[str, str]:
    return {
        "facility_id": facility_id,
        "facility_name": "Clinic",
        "patient_id": patient_id,
        "patient_name": patient_name,
        "case_id": "100",
        "dos": dos,
        "payment_type": payment_type,
        "description": "Office Visit Copay",
        "amount_due": "15.00",
        "amount_paid": "0.00",
        "amount_owed": "15.00",
        "reason": "underpaid",
        "mobile_phone": "",
        "home_phone": "",
        "work_phone": "",
        "email": "",
        "best_phone": "",
    }


def _sched(
    *,
    facility_id: str = "1",
    patient_id: str,
    appointment_at: str,
    visit_status: str = "1",
) -> dict[str, str]:
    return {
        "facility_id": facility_id,
        "facility_name": "Clinic",
        "patient_id": patient_id,
        "patient_name": "Test",
        "case_id": "100",
        "appointment_at": appointment_at,
        "visit_status": visit_status,
    }


def test_build_upcoming_excludes_cancelled_and_past():
    as_of = date(2026, 7, 28)
    upcoming = build_upcoming_by_patient(
        [
            _sched(patient_id="A", appointment_at="2026-07-27 10:00:00"),
            _sched(
                patient_id="B",
                appointment_at="2026-07-28 09:00:00",
                visit_status="Cancelled/No Show",
            ),
            _sched(patient_id="C", appointment_at="2026-07-28 11:00:00"),
            _sched(patient_id="C", appointment_at="2026-08-01 11:00:00"),
        ],
        as_of=as_of,
    )
    assert ("1", "A") not in upcoming
    assert ("1", "B") not in upcoming
    assert upcoming[("1", "C")].upcoming_appointment_count == 2
    assert upcoming[("1", "C")].next_appointment_at == "2026-07-28 11:00:00"


def test_split_with_upcoming_keeps_all_dos_and_enriches():
    upcoming = build_upcoming_by_patient(
        [_sched(patient_id="U", appointment_at="2026-08-05 08:00:00")],
        as_of=date(2026, 7, 28),
    )
    with_up, no_up = split_unpaid_by_upcoming(
        [
            _unpaid(patient_id="U", dos="2026-01-10"),
            _unpaid(patient_id="U", dos="2026-06-15"),
        ],
        upcoming,
    )
    assert len(with_up) == 2
    assert len(no_up) == 0
    assert with_up[0]["next_appointment_at"] == "2026-08-05 08:00:00"
    assert with_up[0]["upcoming_appointment_count"] == "1"


def test_split_no_upcoming_jan_may_only():
    upcoming = build_upcoming_by_patient([], as_of=date(2026, 7, 28))
    with_up, no_up = split_unpaid_by_upcoming(
        [
            _unpaid(patient_id="N", dos="2026-05-20"),
            _unpaid(patient_id="N", dos="2026-06-01"),
            _unpaid(patient_id="N", dos="2026-03-01"),
        ],
        upcoming,
    )
    assert len(with_up) == 0
    assert [r["dos"] for r in no_up] == ["2026-03-01", "2026-05-20"]
    assert all(r["has_upcoming"] == "0" for r in no_up)


def test_split_disjoint_cohorts():
    upcoming = build_upcoming_by_patient(
        [_sched(patient_id="U", appointment_at="2026-07-29 10:00:00")],
        as_of=date(2026, 7, 28),
    )
    with_up, no_up = split_unpaid_by_upcoming(
        [
            _unpaid(patient_id="U", dos="2026-02-01"),
            _unpaid(patient_id="N", dos="2026-02-01"),
        ],
        upcoming,
    )
    with_pids = {r["patient_id"] for r in with_up}
    no_pids = {r["patient_id"] for r in no_up}
    assert with_pids == {"U"}
    assert no_pids == {"N"}
    assert with_pids.isdisjoint(no_pids)


def test_filter_copay_only():
    rows = [
        _unpaid(patient_id="1", dos="2026-01-01", payment_type="Copay"),
        _unpaid(patient_id="2", dos="2026-01-01", payment_type="Other"),
        _unpaid(patient_id="3", dos="2026-01-01", payment_type="copay"),
    ]
    assert [r["patient_id"] for r in filter_copay_rows(rows)] == ["1", "3"]


def test_split_drops_non_copay():
    upcoming = build_upcoming_by_patient(
        [_sched(patient_id="U", appointment_at="2026-08-01 09:00:00")],
        as_of=date(2026, 7, 28),
    )
    with_up, no_up = split_unpaid_by_upcoming(
        [
            _unpaid(patient_id="U", dos="2026-02-01", payment_type="Other"),
            _unpaid(patient_id="N", dos="2026-02-01", payment_type="Other"),
            _unpaid(patient_id="U", dos="2026-03-01", payment_type="Copay"),
        ],
        upcoming,
    )
    assert len(with_up) == 1
    assert with_up[0]["dos"] == "2026-03-01"
    assert len(no_up) == 0


def test_best_phone_prefers_mobile_then_home_then_work():
    assert best_phone("111", "222", "333") == "111"
    assert best_phone("", "222", "333") == "222"
    assert best_phone("", "", "333") == "333"
    assert best_phone("", "", "") == ""


def test_split_fills_contacts_from_lookup(tmp_path: Path):
    patients = tmp_path / "patients.csv"
    patients.write_text(
        "PATIENT_ID,HOME_PHONE,MOBILE_PHONE,WORK_PHONE,EMAIL_ADDRESS\n"
        "U,,555 1111111,,u@example.com\n"
        "N,555 2222222,,,,\n",
        encoding="utf-8",
    )
    contacts = load_patient_contacts(patients)
    upcoming = build_upcoming_by_patient(
        [_sched(patient_id="U", appointment_at="2026-08-01 09:00:00")],
        as_of=date(2026, 7, 28),
    )
    with_up, no_up = split_unpaid_by_upcoming(
        [
            _unpaid(patient_id="U", dos="2026-02-01"),
            _unpaid(patient_id="N", dos="2026-02-01"),
        ],
        upcoming,
        contacts=contacts,
    )
    assert with_up[0]["mobile_phone"] == "555 1111111"
    assert with_up[0]["email"] == "u@example.com"
    assert with_up[0]["best_phone"] == "555 1111111"
    assert no_up[0]["home_phone"] == "555 2222222"
    assert no_up[0]["best_phone"] == "555 2222222"
