"""Unit tests for Daily Note header and CPT parsing."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chart_notes_parse import (
    daily_note_id_from_filename,
    extract_daily_note,
    format_icd_full,
    parse_daily_note_cpt,
    parse_daily_note_header,
    parse_icd_entries,
    us_date_to_iso,
)

SAMPLE_HEADER = """
Bay Ridge
8403 3rd Ave
Brooklyn, NY 11209-4601
Phone: (718)921-9721
Fax: (855)955-3899
Daily Note /
Billing Sheet
Patient Name:  Hegazy, Magda A.
Date of Daily Note:  06/22/2026
Date of Birth:  01/22/1955
Injury/Onset/Change of Status Date:  03/01/2026  Insidious
Referring Physician/NPP:  Lazzara, John DO
Diagnosis:   ICD10: G81.94: Hemiplegia, unspecified affecting
left nondominant side, I48.0: Paroxysmal atrial fibrillation
Date of Original Eval:  06/01/2026
Visit No.:  5
Treatment Diagnosis:   ICD10: G81.94: Hemiplegia,
unspecified affecting left nondominant side, I48.0: Paroxysmal
atrial fibrillation
Insurance Name:  Healthfirst-Medicare
Subjective
Treatment Side:  Left
"""

SAMPLE_CPT = """
CPT Code
Direct Timed Codes
Units
GP:97112
Neuromuscular Re-Education
1
detail line
GP:97140
Manual Therapy
1
GP:97530
Therapeutic Activity/Kinetic
2
GP:97014
E-Stim Unattended
1
"""

SAMPLE_CPT_KX = """
Direct Timed Codes
Units
GP:97110.KX
Therapeutic Exercise ( Therapist - 15 mins.)
1
GP:97140
Manual Therapy
1
"""


def test_us_date_to_iso() -> None:
    assert us_date_to_iso("06/22/2026") == "2026-06-22"
    assert us_date_to_iso("02/07/1974") == "1974-02-07"


def test_daily_note_id_from_filename() -> None:
    assert daily_note_id_from_filename("2026-06-22_DailyNote_DN1018323227.pdf") == "DN1018323227"


def test_parse_icd_entries() -> None:
    entries = parse_icd_entries(
        "ICD10: M25.511: Pain in right shoulder, I48.0: Paroxysmal atrial fibrillation"
    )
    assert entries[0][0] == "M25.511"
    assert "shoulder" in entries[0][1]
    assert entries[1][0] == "I48.0"


def test_parse_daily_note_header() -> None:
    header = parse_daily_note_header(SAMPLE_HEADER)
    assert header["facility_name"] == "Bay Ridge"
    assert header["facility_phone"] == "(718)921-9721"
    assert header["patient_name"] == "Hegazy, Magda A."
    assert header["date_of_daily_note"] == "2026-06-22"
    assert header["injury_onset_date"] == "2026-03-01"
    assert header["injury_onset_qualifier"] == "Insidious"
    assert "G81.94" in header["diagnosis_icd_codes"]
    assert "I48.0" in header["diagnosis_icd_codes"]
    assert header["visit_no"] == "5"
    assert header["insurance_name"] == "Healthfirst-Medicare"


def test_parse_daily_note_cpt() -> None:
    lines = parse_daily_note_cpt(SAMPLE_CPT)
    assert len(lines) == 4
    assert lines[0].modifier_cpt == "GP:97112"
    assert lines[0].units == "1"
    assert lines[0].description == "Neuromuscular Re-Education"
    assert lines[2].modifier_cpt == "GP:97530"
    assert lines[2].units == "2"


def test_parse_daily_note_cpt_kx_suffix() -> None:
    lines = parse_daily_note_cpt(SAMPLE_CPT_KX)
    assert len(lines) == 2
    assert lines[0].modifier_cpt == "GP:97110.KX"
    assert lines[0].billing_modifier_suffix == "KX"
    assert lines[0].units == "1"
    assert lines[1].modifier_cpt == "GP:97140"


def test_parse_daily_note_cpt_with_kx_pdf() -> None:
    pdf = ROOT / (
        "output/recent_10d_fast_chartnotes/edocs/33855694/chart_notes/"
        "2026-05-29_DailyNote_DN1013426138.pdf"
    )
    if not pdf.exists():
        return
    extract = extract_daily_note(pdf, patient_id="33855694")
    assert len(extract.cpt_lines) >= 2
    assert any(line.cpt_code == "97110" for line in extract.cpt_lines)


def test_extract_real_daily_note_pdf() -> None:
    pdf = ROOT / "output/recent_10d_fast_chartnotes/edocs/40876814/chart_notes/2026-06-22_DailyNote_DN1018323227.pdf"
    if not pdf.exists():
        return
    extract = extract_daily_note(pdf, patient_id="40876814")
    assert not extract.error
    assert extract.daily_note_id == "DN1018323227"
    assert extract.patient_name == "Hegazy, Magda A."
    assert "G81.94" in extract.diagnosis_icd_codes
    assert len(extract.cpt_lines) >= 3
    assert any(line.modifier_cpt == "GP:97140" for line in extract.cpt_lines)


if __name__ == "__main__":
    test_us_date_to_iso()
    test_daily_note_id_from_filename()
    test_parse_icd_entries()
    test_parse_daily_note_header()
    test_parse_daily_note_cpt()
    test_parse_daily_note_cpt_kx_suffix()
    test_parse_daily_note_cpt_with_kx_pdf()
    test_extract_real_daily_note_pdf()
    print("All chart notes parse tests passed.")
