"""Tests for denials transform pipeline and normalization."""

from __future__ import annotations

import csv
import unittest
from pathlib import Path

from denials_normalize import (
    build_line_key,
    extract_rarc_codes,
    format_money,
    normalize_claim_id,
    parse_date_iso,
    parse_money,
)
from transform_denials import transform_denials

TEST_DIR = Path(__file__).resolve().parent / "output" / "denials_denials_test"
MERGED_PATH = TEST_DIR / "denials_merged.csv"
LINES_PATH = TEST_DIR / "denials_lines.csv"
CLAIMS_PATH = (
    Path(__file__).resolve().parent / "output" / "claims_rejected_all" / "claims_rejected_all_merged.csv"
)


class TestDenialsNormalize(unittest.TestCase):
    def test_parse_money_formats(self) -> None:
        self.assertEqual(parse_money("$370.00"), 370.0)
        self.assertEqual(parse_money("($4.00)"), -4.0)
        self.assertEqual(parse_money(20.0), 20.0)
        self.assertEqual(format_money("$370.00"), "370.00")

    def test_parse_date_iso(self) -> None:
        self.assertEqual(parse_date_iso("03/20/26"), "2026-03-20")
        self.assertEqual(parse_date_iso("4/16/2026"), "2026-04-16")
        self.assertEqual(parse_date_iso("2026-07-09T12:21:23.882640+00:00"), "2026-07-09")

    def test_normalize_claim_id_zero(self) -> None:
        self.assertEqual(normalize_claim_id("0"), "")
        self.assertEqual(normalize_claim_id(0), "")
        self.assertEqual(normalize_claim_id("12345"), "12345")

    def test_extract_rarc_codes_polluted(self) -> None:
        polluted = "N377 N377: Payment based on contracted fee schedule"
        self.assertEqual(extract_rarc_codes(polluted), ["N377"])

    def test_build_line_key(self) -> None:
        key = build_line_key("3813659435", "4764041873", 1)
        self.assertEqual(key, "3813659435|4764041873|1")


class TestTransformDenials(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MERGED_PATH.exists() or not LINES_PATH.exists():
            raise unittest.SkipTest("denials_denials_test fixtures not available")
        claims = CLAIMS_PATH if CLAIMS_PATH.exists() else None
        cls.outputs = transform_denials(MERGED_PATH, LINES_PATH, claims, TEST_DIR)
        cls.merged_clean = cls._read_csv(cls.outputs["merged_clean"])
        cls.lines_fact = cls._read_csv(cls.outputs["lines_fact"])
        cls.lines_slim = cls._read_csv(cls.outputs["lines_slim"])

    @staticmethod
    def _read_csv(path: Path) -> list[dict]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_merged_clean_claim_id_not_zero(self) -> None:
        for row in self.merged_clean:
            self.assertNotEqual(row.get("claim_id"), "0")

    def test_line_key_uniqueness(self) -> None:
        keys = [row["line_key"] for row in self.lines_fact]
        self.assertEqual(len(keys), len(set(keys)))

    def test_actionable_lines_filter_inactive(self) -> None:
        inactive_actionable = [
            row
            for row in self.lines_fact
            if row.get("is_actionable_line") == "False" and row.get("adjustment_status") != "Active"
        ]
        self.assertGreater(len(inactive_actionable), 0)
        for row in inactive_actionable:
            self.assertEqual(row.get("recoverable_amount_line"), "0.00")

    def test_recoverable_amount_rollup(self) -> None:
        denial_id = "3813659435"
        merged = next(r for r in self.merged_clean if r["denial_id"] == denial_id)
        actionable = [
            r for r in self.lines_fact
            if r["denial_id"] == denial_id and r.get("is_actionable_line") == "True"
        ]
        expected = sum(float(r["recoverable_amount_line"]) for r in actionable)
        self.assertAlmostEqual(float(merged["recoverable_amount"]), expected, places=2)
        self.assertEqual(int(merged["open_line_count"]), len(actionable))

    def test_lines_slim_subset_of_fact(self) -> None:
        self.assertEqual(len(self.lines_slim), len(self.lines_fact))
        slim_keys = set(self.lines_slim[0].keys())
        for field in ("line_key", "denial_id", "is_actionable_line", "recoverable_amount_line"):
            self.assertIn(field, slim_keys)

    def test_iso_dates_in_fact(self) -> None:
        sample = self.lines_fact[0]
        self.assertRegex(sample["remit_received_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_carc_category_co119(self) -> None:
        co119 = [r for r in self.lines_fact if "CO-119" in (r.get("carc_codes") or "")]
        self.assertGreater(len(co119), 0)
        for row in co119:
            self.assertEqual(row["carc_category"], "Benefit maximum / limit reached")


if __name__ == "__main__":
    unittest.main()
