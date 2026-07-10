"""Tests for denial_details and denials_list parsers."""

import unittest
from pathlib import Path

from denial_details import parse_denial_review
from denials_list import parse_money, parse_workcenter_grid

FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"
REVIEW_HTML = (FIXTURE_DIR / "denial_review_sample.html").read_text(encoding="utf-8")
WORKCENTER_HTML = Path(__file__).resolve().parent / "output/explore_denials/workcenter_page1.html"


class TestDenialDetails(unittest.TestCase):
    def test_parse_review_lines(self) -> None:
        result = parse_denial_review(REVIEW_HTML, denial_id="3652667586")
        self.assertTrue(result["found_remits"])
        self.assertEqual(result["denial_id"], "3652667586")
        self.assertEqual(result["line_count"], 3)
        lines = result["lines"]
        self.assertEqual(lines[0]["proc_code"], "29540")
        self.assertEqual(lines[0]["carc_codes"], "CO-45")
        self.assertEqual(lines[0]["adjustment_status"], "Closed")
        self.assertEqual(lines[0]["resolution_action"], "WriteOff")
        self.assertEqual(lines[1]["remark_codes"], "M15")
        self.assertEqual(lines[1]["carc_codes"], "CO-97")
        self.assertEqual(lines[2]["adjustment_status"], "Active")
        self.assertEqual(lines[2]["carc_codes"], "PI-94")
        self.assertAlmostEqual(lines[2]["adjustment_amount"], -4.0)


class TestDenialsList(unittest.TestCase):
    def test_parse_money_parentheses(self) -> None:
        self.assertEqual(parse_money("($4.00)"), -4.0)
        self.assertEqual(parse_money("$120.00"), 120.0)

    def test_parse_workcenter_grid_from_capture(self) -> None:
        if not WORKCENTER_HTML.exists():
            self.skipTest("explore_denials workcenter capture not available")
        html = WORKCENTER_HTML.read_text(encoding="utf-8")
        # Parse only the grid portion inside #rightSide if present
        rows = parse_workcenter_grid(html)
        if not rows:
            right_start = html.find('id="rightSide"')
            if right_start >= 0:
                rows = parse_workcenter_grid(html[right_start:])
        self.assertGreaterEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["denial_id"])
        self.assertTrue(row["claim_id"])
        self.assertTrue(row["patient_name"])


if __name__ == "__main__":
    unittest.main()
