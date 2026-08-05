"""S0–S6 style invariants for the Case-centric pipeline."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
sys.path[:0] = [str(ROOT), str(SCRAPER), str(ROOT / "snowflake_pull" / "scripts")]

from build_case_schedule import (  # noqa: E402
    build_case_schedule_from_rows,
    validate_case_schedule_row,
    write_schedule_artifacts,
)
from case_paths import ensure_case_layout, parse_facility_case_from_path  # noqa: E402
from chart_notes_api import assert_opened_case_id, extract_case_id_from_url  # noqa: E402
from snowflake_pull.case_merge import merge_case_extracted  # noqa: E402
from snowflake_pull.case_unit_state import CaseUnitStateStore, make_case_unit_id  # noqa: E402
from validate_case_schedule import (  # noqa: E402
    edge_case_inventory,
    hard_checks,
    validate_schedule_artifacts,
)


def test_s0_rejects_missing_case_id():
    rows = [
        {
            "facility_id": "1",
            "facility_name": "A",
            "patient_id": "100",
            "case_id": "",
            "appointment_at": "2026-03-15 10:00:00",
            "visit_status": "Checked Out",
        },
        {
            "facility_id": "1",
            "facility_name": "A",
            "patient_id": "100",
            "case_id": "999",
            "appointment_at": "2026-03-16 10:00:00",
            "visit_status": "Checked Out",
        },
    ]
    accepted, rejects, summary = build_case_schedule_from_rows(rows)
    assert len(accepted) == 1
    assert accepted[0]["case_id"] == "999"
    assert summary["case_missing_count"] == 1
    assert rejects[0]["reject_reason"] == "CaseMissingOnSchedule"


def test_s0_validate_helper():
    assert validate_case_schedule_row(
        {"facility_id": "1", "patient_id": "2", "dos": "2026-01-01", "case_id": ""}
    ) == "CaseMissingOnSchedule"


def test_s1_case_mismatch():
    with pytest.raises(ValueError, match="CaseMismatch"):
        assert_opened_case_id("111", "222")
    assert_opened_case_id("222", "222")
    assert extract_case_id_from_url(
        "https://app.webpt.com/patientChartNote.php?ID=1&CaseID=70972991"
    ) == "70972991"


def test_unit_id_and_fsm(tmp_path: Path):
    uid = make_case_unit_id(10, 20, 30, "2026-04-01")
    assert uid == "10:20:30:2026-04-01"
    store = CaseUnitStateStore(tmp_path / "case_units.sqlite")
    try:
        n = store.upsert_units(
            [
                {
                    "unit_id": uid,
                    "batch_id": "b1",
                    "facility_id": "10",
                    "case_id": "20",
                    "patient_id": "30",
                    "dos": "2026-04-01",
                }
            ]
        )
        assert n == 1
        unit = store.claim_next(batch_id="b1")
        assert unit is not None
        assert unit.state == "in_progress"
        store.transition(uid, "failed_terminal", error_type="CaseMismatch", opened_case_id="99")
        unit = store.units_in_states(["failed_terminal"])[0]
        assert unit.error_type == "CaseMismatch"
        assert unit.opened_case_id == "99"
    finally:
        store.close()


def test_path_derives_facility_case(tmp_path: Path):
    root = ensure_case_layout(tmp_path, "55", "66")
    pdf = root / "daily_notes" / "2026-01-01_DailyNote_DN1.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF")
    fid, cid = parse_facility_case_from_path(pdf)
    assert fid == "55"
    assert cid == "66"


def test_s4_merge_keeps_dual_case(tmp_path: Path):
    batch = tmp_path / "batch"
    batch.mkdir()
    side = tmp_path / "side"
    fields = [
        "facility_id",
        "case_id",
        "patient_id",
        "date_of_daily_note",
        "daily_note_id",
        "note_file",
    ]
    with (batch / "daily_notes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow(
            {
                "facility_id": "1",
                "case_id": "A",
                "patient_id": "P",
                "date_of_daily_note": "2026-07-20",
                "daily_note_id": "DN1",
                "note_file": "a.pdf",
            }
        )
        w.writerow(
            {
                "facility_id": "1",
                "case_id": "B",
                "patient_id": "P",
                "date_of_daily_note": "2026-07-20",
                "daily_note_id": "DN2",
                "note_file": "b.pdf",
            }
        )
        w.writerow(
            {
                "facility_id": "1",
                "case_id": "",
                "patient_id": "P",
                "date_of_daily_note": "2026-07-20",
                "daily_note_id": "DN3",
                "note_file": "c.pdf",
            }
        )
    with (batch / "cpt_codes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "facility_id",
                "case_id",
                "patient_id",
                "date_of_daily_note",
                "cpt_code",
                "daily_note_id",
            ],
        )
        w.writeheader()
        w.writerow(
            {
                "facility_id": "1",
                "case_id": "A",
                "patient_id": "P",
                "date_of_daily_note": "2026-07-20",
                "cpt_code": "97110",
                "daily_note_id": "DN1",
            }
        )
        w.writerow(
            {
                "facility_id": "1",
                "case_id": "B",
                "patient_id": "P",
                "date_of_daily_note": "2026-07-20",
                "cpt_code": "97110",
                "daily_note_id": "DN2",
            }
        )

    stats = merge_case_extracted(side, batch, seed="empty")
    assert stats["notes_added"] == 2
    assert stats["rejected_no_case"] >= 1
    assert stats["cpt_added"] == 2
    notes = list(csv.DictReader((side / "daily_notes.csv").open(encoding="utf-8")))
    cases = {r["case_id"] for r in notes}
    assert cases == {"A", "B"}


def test_schedule_dual_case_same_patient_dos():
    rows = [
        {
            "facility_id": "42",
            "patient_id": "52985234",
            "case_id": "70972991",
            "appointment_at": "2026-07-20 10:00:00",
            "visit_status": "Checked Out",
        },
        {
            "facility_id": "42",
            "patient_id": "52985234",
            "case_id": "70000001",
            "appointment_at": "2026-07-20 14:00:00",
            "visit_status": "Checked Out",
        },
    ]
    accepted, rejects, _ = build_case_schedule_from_rows(rows)
    assert rejects == []
    assert len(accepted) == 2
    assert {r["case_id"] for r in accepted} == {"70972991", "70000001"}


def test_schedule_same_patient_two_facilities():
    rows = [
        {
            "facility_id": "21533",
            "patient_id": "52985234",
            "case_id": "67208911",
            "appointment_at": "2026-06-02 09:00:00",
            "visit_status": "Checked Out",
        },
        {
            "facility_id": "31674",
            "patient_id": "52985234",
            "case_id": "70501666",
            "appointment_at": "2026-06-02 11:00:00",
            "visit_status": "Checked Out",
        },
    ]
    accepted, _, _ = build_case_schedule_from_rows(rows)
    assert len(accepted) == 2
    assert {r["facility_id"] for r in accepted} == {"21533", "31674"}
    edges = edge_case_inventory(accepted)
    assert edges["patients_with_multi_facility"] == 1
    assert edges["patient_dos_with_multi_case"] == 1


def test_qa_hard_fail_empty_case_and_dupes(tmp_path: Path):
    bad_accepted = [
        {
            "facility_id": "1",
            "case_id": "",
            "patient_id": "P",
            "dos": "2026-01-01",
            "unit_id": "1::P:2026-01-01",
        },
        {
            "facility_id": "1",
            "case_id": "C",
            "patient_id": "P",
            "dos": "2026-01-02",
            "unit_id": "1:C:P:2026-01-02",
        },
        {
            "facility_id": "1",
            "case_id": "C",
            "patient_id": "P",
            "dos": "2026-01-02",
            "unit_id": "1:C:P:2026-01-02",
        },
    ]
    result = hard_checks(bad_accepted, [], {"input_rows": 3, "case_missing_count": 0})
    assert result["pass"] is False
    assert result["empty_case_count"] == 1
    assert result["dup_count"] == 1


def test_qa_pass_on_written_artifacts(tmp_path: Path):
    rows = [
        {
            "facility_id": "1",
            "patient_id": "10",
            "case_id": "20",
            "appointment_at": "2026-03-01 10:00:00",
            "visit_status": "Checked Out",
        }
    ]
    accepted, rejects, summary = build_case_schedule_from_rows(rows)
    write_schedule_artifacts(tmp_path, accepted, rejects, summary)
    report, _ = validate_schedule_artifacts(tmp_path)
    assert report["pass"] is True
    assert report["hard_checks"]["empty_case_count"] == 0
    assert report["hard_checks"]["dup_count"] == 0


def test_counts_by_error_type(tmp_path: Path):
    store = CaseUnitStateStore(tmp_path / "case_units.sqlite")
    try:
        store.upsert_units(
            [
                {
                    "unit_id": "1:1:1:2026-01-01",
                    "batch_id": "b1",
                    "facility_id": "1",
                    "case_id": "1",
                    "patient_id": "1",
                    "dos": "2026-01-01",
                },
                {
                    "unit_id": "1:2:1:2026-01-02",
                    "batch_id": "b1",
                    "facility_id": "1",
                    "case_id": "2",
                    "patient_id": "1",
                    "dos": "2026-01-02",
                },
                {
                    "unit_id": "1:3:1:2026-01-03",
                    "batch_id": "b1",
                    "facility_id": "1",
                    "case_id": "3",
                    "patient_id": "1",
                    "dos": "2026-01-03",
                },
            ]
        )
        for uid, err in (
            ("1:1:1:2026-01-01", "CaseMismatch"),
            ("1:2:1:2026-01-02", "DownloadEmpty"),
            ("1:3:1:2026-01-03", "CaseMismatch"),
        ):
            store.claim_next(batch_id="b1")
            store.transition(uid, "failed_terminal", error_type=err)
        counts = store.counts_by_error_type(batch_id="b1")
        assert counts["CaseMismatch"] == 2
        assert counts["DownloadEmpty"] == 1
    finally:
        store.close()
