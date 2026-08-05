"""Unit tests for Daily Note header and CPT parsing."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chart_notes_parse import (
    daily_note_id_from_filename,
    extract_daily_note,
    extract_plan_of_care,
    format_icd_full,
    parse_daily_note_cpt,
    parse_daily_note_header,
    parse_icd_entries,
    parse_plan_of_care,
    parse_poc_goals,
    poc_id_from_filename,
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

# Bare CPT (no GP: prefix) as seen on Track F note_exists_cpt_missing PDFs
SAMPLE_CPT_BARE = """
Direct Timed Codes
Units
97110
Therapeutic Exercise
2
1 of 5

Bushwick
Patient Name: Jamison, Freddie
Document Date: 07/10/2026
Daily Note /
Billing Sheet
97140
Should not parse this narrative code
"""

SAMPLE_POC_FULL = """
Plan of Care
Date of Plan of Care:  04/06/2026
Patient Name:  Doe, Jane
Goals
Functional Scale from 38 to 70 to reflect improved independency during ADL. |
Plan
Frequency:  2-3 times a week
Duration:  10 weeks
Plan:  Begin Plan as Outlined
Treatment to be provided:
Procedures
Therapeutic Exercises (ROM, Strength), Hot Packs (Duration: 15 minutes)
"""

SAMPLE_POC_NO_PLAN_LINE = """
Plan of Care
Date of Plan of Care:  06/30/2026
Plan
Frequency:  1 time a week
Duration:  8 weeks
Treatment to be provided:
Procedures
Hot Packs (Duration: 15 minutes)
"""

SAMPLE_POC_GOALS_WITH_PCT = """
Plan of Care
Date of Plan of Care:  07/23/2026
Patient Name:  Stewart, Millicent J
Short Term Goals:
1: (4 Weeks) | 0% | Patient will report decreased bilateral shoulder pain from 9/10 to ≤6/10 to improve tolerance to basic
activities. |
2: (4 Weeks) | 50% | Patient will improve shoulder AROM (flexion/abduction) from ~70° to ≥90° to assist with light reaching
tasks. |
3: (4 Weeks) | 60% | Patient will demonstrate improved upper extremity strength from 2/5–3/5 to ≥3/5 to support basic ADLs. |
4: (4 Weeks) | 50% | Patient will report improved sleep with reduced nighttime awakenings due to pain (≤2 interruptions per
night). |
5: (4 Weeks) | 60% | Patient will demonstrate compliance with home exercise program with proper technique and minimal
cueing. |
Long Term Goals:
1: (8 Weeks) | 0% | Patient will reduce bilateral shoulder pain to ≤3–4/10 to allow improved comfort during daily activities. |
2: (8 Weeks) | 25% | Patient will achieve shoulder AROM (flexion/abduction) ≥120° to perform functional reaching and self-care
tasks. |
3: (8 Weeks) | 25% | Patient will improve upper extremity strength to ≥4-/5 to safely perform ADLs such as dressing and light
lifting. |
4: (8 Weeks) | 40% | Patient will improve sleep quality with minimal to no disruptions from shoulder pain. |
5: (8 Weeks) | 40% | Patient will improve functional independence as evidenced by decreased QuickDASH score from 77.27 to
≤40. |
Plan
Frequency:  2-3 times a week
Duration:  8 weeks
Plan:  Begin Plan as Outlined
Treatment to be provided:
"""

SAMPLE_POC_GOALS_NO_PCT = """
Plan of Care
Date of Plan of Care:  01/06/2026
Short Term Goals:
1: (4 Weeks)  | Within 4 weeks, the patient will report a reduction in right shoulder pain from 9/10 to ≤6/10 on the NPRS during
functional activities. |
2: (4 Weeks)  | Within 4 weeks, the patient will improve right shoulder muscle strength by at least one MMT grade to facilitate
performance of basic ADLs. |
3: (4 Weeks)  | Within 4 weeks, the patient will demonstrate a minimum 10–15% improvement in the Upper DASH score,
indicating improved upper extremity function. |
Long Term Goals:
1: (8 Weeks)  | Within 8 weeks, the patient will report right shoulder pain of ≤3/10 on the NPRS during daily and household
activities. |
2: (8 Weeks)  | Within 8 weeks, the patient will demonstrate right shoulder muscle strength of ≥4/5 on MMT, allowing
independent performance of ADLs without increased pain. |
3: (8 Weeks)  | Within 8 weeks, the patient will demonstrate a ≥25–30% improvement in the Upper DASH score, reflecting
meaningful functional improvement. |
Plan
Frequency:  2 times a week
Duration:  8 weeks
Plan:  Begin Plan as Outlined
"""

SAMPLE_POC_GOALS_PAGE_BREAK = """
Plan of Care
Date of Plan of Care:  07/20/2026
Short Term Goals:
1: (5 Weeks)  | 50% | Patient will increase left shoulder active flexion from 20° to 40° in order to allow for improved forward
reaching during table-top functional activities. |
2: (5 Weeks)  | 50% | Patient will increase left shoulder musculature strength from 2-/5 to 2+/5 to improve dynamic joint stability
as bone healing progresses. |
3: (5 Weeks)  | 25% | Patient will decrease left shoulder pain scale from 8/10 to 4/10 to allow for improved comfort during sleep
and sling adjustments. |
1 of 2

Bay Ridge
8403 3rd Ave
Brooklyn, NY 11209-4601
Phone: (718)921-9721
Fax: (855)955-3899
Patient Name: Saad, Aiva
Date of Birth: 04/30/1970
Document Date: 07/20/2026
Plan of Care
4: (5 Weeks)  | 55% | Decrease Upper Extremity Quick DASH from 47.73% to 30% to reflect improved independency during
ADL. |
Long Term Goals:
1: (10 Weeks)  | 20% | Patient will increase left shoulder active flexion from 20° to 60° in order to allow for improved forward
reaching during table-top functional activities. |
2: (10 Weeks)  | 25% | Patient will increase left shoulder musculature strength from 2-/5 to 3/5 to improve dynamic joint stability
as bone healing progresses. |
Plan
Frequency:  2-3 times a week
Duration:  10 weeks
Plan:  Begin Plan as Outlined
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


def test_parse_daily_note_cpt_bare_codes() -> None:
    lines = parse_daily_note_cpt(SAMPLE_CPT_BARE)
    assert len(lines) == 1
    assert lines[0].cpt_code == "97110"
    assert lines[0].modifier == ""
    assert lines[0].units == "2"
    assert lines[0].description == "Therapeutic Exercise"


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


def test_poc_id_from_filename() -> None:
    assert (
        poc_id_from_filename("2026-04-06-23863267-30984911-PO1001982396-.pdf")
        == "PO1001982396"
    )


def test_parse_plan_of_care_full() -> None:
    parsed = parse_plan_of_care(SAMPLE_POC_FULL)
    assert parsed["date_of_plan_of_care"] == "2026-04-06"
    assert parsed["frequency"] == "2-3 times a week"
    assert parsed["duration"] == "10 weeks"
    assert parsed["plan"] == "Begin Plan as Outlined"


def test_parse_plan_of_care_missing_plan_line() -> None:
    parsed = parse_plan_of_care(SAMPLE_POC_NO_PLAN_LINE)
    assert parsed["date_of_plan_of_care"] == "2026-06-30"
    assert parsed["frequency"] == "1 time a week"
    assert parsed["duration"] == "8 weeks"
    assert parsed["plan"] == ""


def test_parse_plan_of_care_ignores_procedure_duration() -> None:
    parsed = parse_plan_of_care(SAMPLE_POC_FULL)
    assert parsed["duration"] == "10 weeks"
    assert "15 minutes" not in parsed["duration"]


def test_parse_poc_goals_legacy_goals_empty() -> None:
    assert parse_poc_goals(SAMPLE_POC_FULL) == []


def test_parse_poc_goals_with_progress_pct() -> None:
    goals = parse_poc_goals(SAMPLE_POC_GOALS_WITH_PCT)
    assert len(goals) == 10
    short = [g for g in goals if g.goal_type == "short_term"]
    long = [g for g in goals if g.goal_type == "long_term"]
    assert len(short) == 5
    assert len(long) == 5
    assert short[0].goal_number == "1"
    assert short[0].weeks == "4"
    assert short[0].progress_pct == "0"
    assert "bilateral shoulder pain" in short[0].goal_text
    assert "\n" not in short[0].goal_text
    assert short[1].progress_pct == "50"
    assert long[0].weeks == "8"
    assert long[0].progress_pct == "0"
    assert long[4].progress_pct == "40"
    assert "QuickDASH" in long[4].goal_text


def test_parse_poc_goals_without_progress_pct() -> None:
    goals = parse_poc_goals(SAMPLE_POC_GOALS_NO_PCT)
    assert len(goals) == 6
    assert all(g.progress_pct == "" for g in goals)
    assert goals[0].goal_type == "short_term"
    assert goals[0].weeks == "4"
    assert "right shoulder pain" in goals[0].goal_text
    assert goals[3].goal_type == "long_term"
    assert goals[3].weeks == "8"
    assert goals[3].goal_number == "1"


def test_parse_poc_goals_page_break() -> None:
    goals = parse_poc_goals(SAMPLE_POC_GOALS_PAGE_BREAK)
    short = [g for g in goals if g.goal_type == "short_term"]
    long = [g for g in goals if g.goal_type == "long_term"]
    assert len(short) == 4
    assert len(long) == 2
    assert short[3].goal_number == "4"
    assert short[3].progress_pct == "55"
    assert "Quick DASH" in short[3].goal_text
    assert "Bay Ridge" not in short[3].goal_text
    assert "Plan of Care" not in short[3].goal_text
    assert long[0].weeks == "10"
    assert long[0].progress_pct == "20"


def test_extract_real_plan_of_care_pdf() -> None:
    pdf = ROOT / (
        "output/jun_jul_2026/edocs/23863267/chart_notes/"
        "2026-04-06-23863267-30984911-PO1001982396-.pdf"
    )
    if not pdf.exists():
        return
    extract = extract_plan_of_care(pdf, patient_id="23863267")
    assert not extract.error
    assert extract.poc_id == "PO1001982396"
    assert extract.frequency == "2-3 times a week"
    assert extract.duration == "10 weeks"
    assert extract.plan == "Begin Plan as Outlined"
    assert extract.date_of_plan_of_care == "2026-04-06"


if __name__ == "__main__":
    test_us_date_to_iso()
    test_daily_note_id_from_filename()
    test_parse_icd_entries()
    test_parse_daily_note_header()
    test_parse_daily_note_cpt()
    test_parse_daily_note_cpt_kx_suffix()
    test_parse_daily_note_cpt_with_kx_pdf()
    test_extract_real_daily_note_pdf()
    test_poc_id_from_filename()
    test_parse_plan_of_care_full()
    test_parse_plan_of_care_missing_plan_line()
    test_parse_plan_of_care_ignores_procedure_duration()
    test_parse_poc_goals_legacy_goals_empty()
    test_parse_poc_goals_with_progress_pct()
    test_parse_poc_goals_without_progress_pct()
    test_parse_poc_goals_page_break()
    test_extract_real_plan_of_care_pdf()
    print("All chart notes parse tests passed.")
