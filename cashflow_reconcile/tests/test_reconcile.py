import unittest
from datetime import date
from pathlib import Path

from cashflow_reconcile.insurance_map import load_insurance_rules, map_insurance_to_payors
from cashflow_reconcile.load_webpt import load_webpt_lines
from cashflow_reconcile.matcher import match_lines
from cashflow_reconcile.normalize import (
    name_key_from_revflow,
    name_key_from_webpt,
    parse_date,
    parse_money,
)
from cashflow_reconcile.parse_revflow_eob import parse_revflow_csv


FIXTURES = Path(__file__).parent / "fixtures"


class NormalizeTests(unittest.TestCase):
    def test_webpt_name_key(self):
        self.assertEqual(name_key_from_webpt("Panek, Zofia"), "PANEKZOFIA")
        self.assertEqual(name_key_from_revflow("PANEK", "ZOFIA"), "PANEKZOFIA")

    def test_parse_date(self):
        self.assertEqual(parse_date("2026-06-11"), date(2026, 6, 11))
        self.assertEqual(parse_date("06/11/2026"), date(2026, 6, 11))
        self.assertEqual(parse_date("01/15/1980"), date(1980, 1, 15))

    def test_parse_money(self):
        self.assertEqual(parse_money("$1,234.56"), 1234.56)
        self.assertEqual(parse_money("($120.00)"), -120.0)


class RevflowParserTests(unittest.TestCase):
    def test_parse_sample_eob(self):
        lines = parse_revflow_csv(FIXTURES / "WELLPOINT FEDERAL - 816760326.csv")
        self.assertEqual(len(lines), 2)
        eval_line = next(item for item in lines if item.cpt_code == "97163")
        self.assertEqual(eval_line.payor, "WELLPOINT FEDERAL")
        self.assertEqual(eval_line.eob_date, "07/02/2026")
        self.assertAlmostEqual(eval_line.paid_amount, 86.36)
        self.assertIn("CO-45", eval_line.carcs)
        self.assertIn("CO-253", eval_line.carcs)
        self.assertIn("PR-2", eval_line.carcs)


class LoadWebptTests(unittest.TestCase):
    def test_load_with_patient_export(self):
        lines = load_webpt_lines(
            FIXTURES,
            patients_export_path=FIXTURES / "sample_patients_export.csv",
            service_from=date(2026, 6, 1),
            service_to=date(2026, 7, 31),
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].dob, "1980-01-15")
        self.assertEqual(lines[0].ins_name, "Aetna")
        self.assertEqual(lines[0].expected_copay, "$20")


class InsuranceMapTests(unittest.TestCase):
    def test_aetna_mapping(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("Aetna Commercial – (Zaya)", rules)
        self.assertIn("AETNA", mapped)

    def test_wellcare_mapping(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("Wellcare", rules)
        self.assertIn("FIDELIS CAREWELLCARE BY FIDELIS CARE", mapped)

    def test_ghi_mapping(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("GHI", rules)
        self.assertIn("EMBLEMHEALTH", mapped)


class MatcherTests(unittest.TestCase):
    def test_match_fixture_pair(self):
        rules = load_insurance_rules()
        webpt_lines = load_webpt_lines(
            FIXTURES,
            patients_export_path=FIXTURES / "sample_patients_export.csv",
            service_from=date(2026, 6, 1),
            service_to=date(2026, 7, 31),
        )
        payments = parse_revflow_csv(FIXTURES / "WELLPOINT FEDERAL - 816760326.csv")
        result = match_lines(webpt_lines, payments, rules)
        matched = [item for item in result.lines if item.payment is not None]
        self.assertEqual(len(matched), 2)
        self.assertTrue(all(item.status in {"paid", "secondary_pending"} for item in matched))


if __name__ == "__main__":
    unittest.main()
