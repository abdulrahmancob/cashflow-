"""Case merge / extract key uniqueness."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "webpt_edco_scraper")]

from case_extract import case_cpt_key, case_note_key, require_case_columns  # noqa: E402
import pytest


def test_note_keys_differ_by_case():
    a = {
        "facility_id": "1",
        "case_id": "100",
        "patient_id": "9",
        "date_of_daily_note": "2026-07-20",
        "daily_note_id": "DN1",
    }
    b = {**a, "case_id": "200"}
    assert case_note_key(a) != case_note_key(b)


def test_cpt_keys_differ_by_case():
    a = {
        "facility_id": "1",
        "case_id": "100",
        "patient_id": "9",
        "date_of_daily_note": "2026-07-20",
        "cpt_code": "97110",
        "daily_note_id": "DN1",
    }
    b = {**a, "case_id": "200"}
    assert case_cpt_key(a) != case_cpt_key(b)


def test_require_case_columns_fail_closed():
    with pytest.raises(ValueError, match="case_id"):
        require_case_columns(
            {
                "facility_id": "1",
                "case_id": "",
                "patient_id": "9",
                "date_of_daily_note": "2026-07-20",
            }
        )
