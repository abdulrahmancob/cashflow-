import unittest
from datetime import date
from pathlib import Path

import numpy as np

from cashflow_reconcile.insurance_map import load_insurance_rules, map_insurance_to_payors
from cashflow_reconcile.load_transaction_tracker import load_deposit_dates
from cashflow_reconcile.load_webpt import WebptLine, load_webpt_lines
from cashflow_reconcile.matcher import (
    MatchedLine,
    aggregate_visits,
    match_lines,
    optimal_assignment,
)
from cashflow_reconcile.normalize import (
    name_key_from_revflow,
    name_key_from_webpt,
    parse_date,
    parse_money,
)
from cashflow_reconcile.parse_revflow_eob import (
    PaymentLine,
    is_bonus_payment,
    parse_revflow_csv,
)
from cashflow_reconcile.reconcile import (
    _checks_not_in_tracker_rows,
    _partition_payments_by_tracker,
    _payment_rows,
)


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

    def test_parse_ash_bonus_carcs(self):
        lines = parse_revflow_csv(FIXTURES / "AMERICAN SPECIALTY HEALTH - 109748248.csv")
        bonuses = [line for line in lines if is_bonus_payment(line)]
        self.assertEqual(len(bonuses), 2)
        by_carc = {line.carcs: line.paid_amount for line in bonuses}
        self.assertAlmostEqual(by_carc["OA-161"], 0.99)
        self.assertAlmostEqual(by_carc["OA-144"], 2.48)
        self.assertTrue(all(line.date_of_service == "" for line in bonuses))
        self.assertTrue(all(line.cpt_code == "" for line in bonuses))
        # Non-bonus empty-DOS rows are still ignored.
        self.assertFalse(any(line.carcs == "OA-18" for line in lines))
        service = [line for line in lines if line.cpt_code]
        self.assertEqual(len(service), 4)  # 97162, 97112, two 97110 units rolled
        self.assertAlmostEqual(sum(line.paid_amount for line in service), 99.32)


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
        self.assertEqual(lines[0].facility_name, "Bay Ridge")

    def test_facility_fallback_from_daily_notes(self):
        """When patients_export is missing, facility_name comes from daily_notes."""
        lines = load_webpt_lines(
            FIXTURES,
            patients_export_path=FIXTURES / "does_not_exist.csv",
            service_from=date(2026, 6, 1),
            service_to=date(2026, 7, 31),
        )
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.facility_name == "Bay Ridge" for line in lines))
        self.assertEqual(lines[0].ins_name, "Aetna Commercial")


class InsuranceMapTests(unittest.TestCase):
    def test_aetna_mapping(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("Aetna Commercial – (Zaya)", rules)
        self.assertIn("AETNA", mapped)

    def test_wellcare_mapping(self):
        rules = load_insurance_rules()
        mapped = map_insurance_to_payors("Wellcare", rules)
        self.assertIn("FIDELIS CAREWELLCARE BY FIDELIS CARE", mapped)
        self.assertIn("NEW YORK NETWORK IPA", mapped)

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


class PaymentRowsTests(unittest.TestCase):
    def test_webpt_patient_id_matched_and_orphan(self):
        rules = load_insurance_rules()
        webpt_lines = load_webpt_lines(
            FIXTURES,
            patients_export_path=FIXTURES / "sample_patients_export.csv",
            service_from=date(2026, 6, 1),
            service_to=date(2026, 7, 31),
        )
        payments = parse_revflow_csv(FIXTURES / "WELLPOINT FEDERAL - 816760326.csv")
        result = match_lines(webpt_lines, payments, rules)
        payment_to_webpt = {
            id(item.payment): item.webpt.patient_id
            for item in result.lines
            if item.payment is not None
        }
        payment_to_facility = {
            id(item.payment): item.webpt.facility_name
            for item in result.lines
            if item.payment is not None and item.webpt.facility_name
        }
        patient_facility = {
            line.patient_id: line.facility_name
            for line in webpt_lines
            if line.facility_name
        }
        name_key_facility = {
            line.name_key: line.facility_name
            for line in webpt_lines
            if line.facility_name and line.name_key
        }
        orphan = PaymentLine(
            revflow_patient_id="orphan-1",
            first_name="NO",
            last_name="MATCH",
            name_key="MATCHNO",
            date_of_service="06/01/2026",
            cpt_code="97001",
            modifier="",
            units=1,
            billed_amount=10.0,
            allowed_amount=10.0,
            paid_amount=5.0,
            adjustment_amount=0.0,
            deductible_amount=0.0,
            carcs="",
            payor="TEST",
            check_eft_num="",
            eob_date="07/01/2026",
            report_from="",
            report_to="",
            source_file="orphan.csv",
        )
        # Orphan sharing a known name_key should pick up facility via name fallback
        orphan_named = PaymentLine(
            revflow_patient_id="orphan-named",
            first_name="Alejandro",
            last_name="Martinez",
            name_key="MARTINEZALEJANDRO",
            date_of_service="06/01/2026",
            cpt_code="97001",
            modifier="",
            units=1,
            billed_amount=10.0,
            allowed_amount=10.0,
            paid_amount=5.0,
            adjustment_amount=0.0,
            deductible_amount=0.0,
            carcs="",
            payor="TEST",
            check_eft_num="",
            eob_date="07/01/2026",
            report_from="",
            report_to="",
            source_file="orphan.csv",
        )
        rows = _payment_rows(
            [*payments, orphan, orphan_named],
            payment_to_webpt,
            payment_to_facility=payment_to_facility,
            patient_facility=patient_facility,
            name_key_facility=name_key_facility,
        )
        matched_rows = [
            row
            for row in rows
            if row["revflow_patient_id"] not in {"orphan-1", "orphan-named"}
        ]
        self.assertEqual(len(matched_rows), len(payments))
        self.assertTrue(all(row["webpt_patient_id"] == "999001" for row in matched_rows))
        self.assertTrue(all(row["facility_name"] == "Bay Ridge" for row in matched_rows))
        orphan_row = next(row for row in rows if row["revflow_patient_id"] == "orphan-1")
        self.assertEqual(orphan_row["webpt_patient_id"], "")
        self.assertEqual(orphan_row["facility_name"], "")
        named_row = next(row for row in rows if row["revflow_patient_id"] == "orphan-named")
        self.assertEqual(named_row["facility_name"], "Bay Ridge")


def _make_webpt(
    patient_id: str,
    dos: str,
    cpt: str,
    *,
    patient_name: str = "Martinez, Alejandro",
    name_key: str = "MARTINEZALEJANDRO",
    facility_name: str = "Bay Ridge",
    insurance_note: str = "Aetna",
    ins_name: str = "Aetna",
    case_id: str = "",
    facility_id: str = "",
) -> WebptLine:
    return WebptLine(
        patient_id=patient_id,
        daily_note_id="DN1",
        patient_name=patient_name,
        name_key=name_key,
        date_of_service=dos,
        cpt_code=cpt,
        modifier="GP",
        units="1",
        description="",
        insurance_note=insurance_note,
        facility_name=facility_name,
        dob="1980-01-15",
        ins_name=ins_name,
        case_id=case_id,
        facility_id=facility_id,
    )


def _make_payment(
    *,
    cpt: str,
    paid: float,
    check: str,
    eob_date: str,
    date_of_service: str = "06/11/2026",
    name_key: str = "MARTINEZALEJANDRO",
    revflow_patient_id: str = "1",
    first_name: str = "ALEJANDRO",
    last_name: str = "MARTINEZ",
    payor: str = "AETNA",
    carcs: str = "",
    source_file: str = "test.csv",
    units: int = 1,
) -> PaymentLine:
    return PaymentLine(
        revflow_patient_id=revflow_patient_id,
        first_name=first_name,
        last_name=last_name,
        name_key=name_key,
        date_of_service=date_of_service,
        cpt_code=cpt,
        modifier="GP" if cpt else "",
        units=units,
        billed_amount=paid,
        allowed_amount=paid,
        paid_amount=paid,
        adjustment_amount=0.0,
        deductible_amount=0.0,
        carcs=carcs,
        payor=payor,
        check_eft_num=check,
        eob_date=eob_date,
        report_from="",
        report_to="",
        source_file=source_file,
    )


class AggregateVisitsCheckTests(unittest.TestCase):
    def test_primary_and_secondary_checks(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97163"),
                payment=_make_payment(
                    cpt="97163", paid=86.36, check="CHECK-A", eob_date="07/02/2026"
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97110"),
                payment=_make_payment(
                    cpt="97110", paid=38.81, check="CHECK-B", eob_date="07/10/2026"
                ),
                status="paid",
            ),
        ]
        visits = aggregate_visits(lines)
        self.assertEqual(len(visits), 1)
        visit = visits[0]
        self.assertEqual(visit["primary_check_number"], "CHECK-A")
        self.assertEqual(visit["primary_check_date"], "07/02/2026")
        self.assertEqual(visit["primary_check_amount"], "86.36")
        self.assertEqual(visit["secondary_check_number"], "CHECK-B")
        self.assertEqual(visit["secondary_check_date"], "07/10/2026")
        self.assertEqual(visit["secondary_check_amount"], "38.81")
        self.assertEqual(visit["total_paid"], "125.17")
        self.assertEqual(visit["matched_paid"], "125.17")
        self.assertEqual(visit["bonus_paid"], "0.00")
        self.assertEqual(visit["unmatched_paid"], "0.00")
        self.assertEqual(visit["visit_paid_total"], "125.17")
        self.assertEqual(visit["unmatched_cpts"], "")

    def test_same_check_rolls_up_amount(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97163"),
                payment=_make_payment(
                    cpt="97163", paid=50.0, check="SAME", eob_date="07/02/2026"
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97110"),
                payment=_make_payment(
                    cpt="97110", paid=25.0, check="SAME", eob_date="07/02/2026"
                ),
                status="paid",
            ),
        ]
        visit = aggregate_visits(lines)[0]
        self.assertEqual(visit["primary_check_number"], "SAME")
        self.assertEqual(visit["primary_check_amount"], "75.00")
        self.assertEqual(visit["secondary_check_number"], "")
        self.assertEqual(visit["secondary_check_amount"], "")

    def test_fixture_match_fills_primary_check(self):
        rules = load_insurance_rules()
        webpt_lines = load_webpt_lines(
            FIXTURES,
            patients_export_path=FIXTURES / "sample_patients_export.csv",
            service_from=date(2026, 6, 1),
            service_to=date(2026, 7, 31),
        )
        payments = parse_revflow_csv(FIXTURES / "WELLPOINT FEDERAL - 816760326.csv")
        result = match_lines(webpt_lines, payments, rules)
        visits = aggregate_visits(result.lines, result.orphan_payments)
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["primary_check_number"], "816760326")
        self.assertTrue(visits[0]["primary_check_date"])
        self.assertEqual(visits[0]["secondary_check_number"], "")
        self.assertEqual(visits[0]["matched_paid"], visits[0]["total_paid"])
        self.assertEqual(visits[0]["visit_paid_total"], visits[0]["total_paid"])

    def test_orphan_attaches_unmatched_paid_and_cpts(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("55240937", "2026-06-16", "97110"),
                payment=_make_payment(
                    cpt="97110",
                    paid=35.64,
                    check="91685559",
                    eob_date="06/22/2026",
                    date_of_service="06/16/2026",
                    name_key="AHMADMAQSOOD",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt("55240937", "2026-06-16", "97112"),
                payment=_make_payment(
                    cpt="97112",
                    paid=26.68,
                    check="91685559",
                    eob_date="06/22/2026",
                    date_of_service="06/16/2026",
                    name_key="AHMADMAQSOOD",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt("55240937", "2026-06-16", "97140"),
                payment=_make_payment(
                    cpt="97140",
                    paid=17.03,
                    check="91685559",
                    eob_date="06/22/2026",
                    date_of_service="06/16/2026",
                    name_key="AHMADMAQSOOD",
                ),
                status="paid",
            ),
        ]
        # Override name_key on webpt lines to match Ahmad
        for item in lines:
            item.webpt.name_key = "AHMADMAQSOOD"
            item.webpt.patient_name = "Ahmad, Maqsood"

        orphan = _make_payment(
            cpt="G0283",
            paid=7.77,
            check="91685559",
            eob_date="06/22/2026",
            date_of_service="06/16/2026",
            name_key="AHMADMAQSOOD",
        )
        visit = aggregate_visits(lines, [orphan])[0]
        self.assertEqual(visit["matched_paid"], "79.35")
        self.assertEqual(visit["total_paid"], "79.35")
        self.assertEqual(visit["bonus_paid"], "0.00")
        self.assertEqual(visit["unmatched_paid"], "7.77")
        self.assertEqual(visit["visit_paid_total"], "87.12")
        self.assertEqual(visit["unmatched_cpts"], "G0283=7.77")
        self.assertEqual(visit["primary_check_amount"], "87.12")

    def test_orphan_same_revflow_different_dos_not_dumped(self):
        """Cross-DOS orphans must not dump onto the only matched visit (Carmelo bug)."""
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "54984287",
                    "2026-06-11",
                    "97110",
                    patient_name="Rosa, Carmelo",
                    name_key="ROSACARMELO",
                    facility_name="Riverdale",
                ),
                payment=_make_payment(
                    cpt="97110",
                    paid=49.76,
                    check="MATCHED",
                    eob_date="06/20/2026",
                    date_of_service="2026-06-11",
                    name_key="ROSACARMELO",
                    revflow_patient_id="10384356",
                    first_name="CARMELO",
                    last_name="ROSA",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt(
                    "54984287",
                    "2026-06-23",
                    "97110",
                    patient_name="Rosa, Carmelo",
                    name_key="ROSACARMELO",
                    facility_name="Riverdale",
                ),
                payment=None,
                status="pending",
            ),
        ]
        cross_dos_orphans = [
            _make_payment(
                cpt="97110",
                paid=110.76,
                check="APRIL",
                eob_date="05/04/2026",
                date_of_service="2026-04-15",
                name_key="ROSACARMELO",
                revflow_patient_id="10384356",
                first_name="CARMELO",
                last_name="ROSA",
            ),
            _make_payment(
                cpt="97112",
                paid=37.24,
                check="MAY",
                eob_date="05/19/2026",
                date_of_service="2026-05-11",
                name_key="ROSACARMELO",
                revflow_patient_id="10384356",
                first_name="CARMELO",
                last_name="ROSA",
            ),
        ]
        visits = aggregate_visits(lines, cross_dos_orphans)
        by_dos = {v["date_of_service"]: v for v in visits}
        self.assertEqual(by_dos["2026-06-11"]["matched_paid"], "49.76")
        self.assertEqual(by_dos["2026-06-11"]["unmatched_paid"], "0.00")
        self.assertEqual(by_dos["2026-06-11"]["unmatched_cpts"], "")
        self.assertEqual(by_dos["2026-06-11"]["visit_paid_total"], "49.76")
        self.assertEqual(by_dos["2026-06-23"]["unmatched_paid"], "0.00")

    def test_orphan_same_revflow_same_dos_still_attaches(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "54984287",
                    "2026-06-11",
                    "97110",
                    patient_name="Rosa, Carmelo",
                    name_key="ROSACARMELO",
                ),
                payment=_make_payment(
                    cpt="97110",
                    paid=49.76,
                    check="MATCHED",
                    eob_date="06/20/2026",
                    date_of_service="2026-06-11",
                    name_key="ROSACARMELO",
                    revflow_patient_id="10384356",
                    first_name="CARMELO",
                    last_name="ROSA",
                ),
                status="paid",
            ),
        ]
        orphan = _make_payment(
            cpt="G0283",
            paid=8.32,
            check="SAME-DAY",
            eob_date="06/20/2026",
            date_of_service="2026-06-11",
            name_key="ROSACARMELO",
            revflow_patient_id="10384356",
            first_name="CARMELO",
            last_name="ROSA",
        )
        visit = aggregate_visits(lines, [orphan])[0]
        self.assertEqual(visit["matched_paid"], "49.76")
        self.assertEqual(visit["unmatched_paid"], "8.32")
        self.assertEqual(visit["unmatched_cpts"], "G0283=8.32")
        self.assertEqual(visit["visit_paid_total"], "58.08")

    def test_ash_bonus_included_in_total_paid(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "55200111",
                    "2026-03-14",
                    "97162",
                    patient_name="Marin, Maria",
                    name_key="MARINMARIA",
                ),
                payment=_make_payment(
                    cpt="97162",
                    paid=31.32,
                    check="109748248",
                    eob_date="05/13/2026",
                    date_of_service="03/14/2026",
                    name_key="MARINMARIA",
                    revflow_patient_id="10284960",
                    first_name="MARIA",
                    last_name="MARIN",
                    payor="AMERICAN SPECIALTY HEALTH",
                    source_file="AMERICAN SPECIALTY HEALTH - 109748248.csv",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt(
                    "55200111",
                    "2026-03-14",
                    "97112",
                    patient_name="Marin, Maria",
                    name_key="MARINMARIA",
                ),
                payment=_make_payment(
                    cpt="97112",
                    paid=31.22,
                    check="109748248",
                    eob_date="05/13/2026",
                    date_of_service="03/14/2026",
                    name_key="MARINMARIA",
                    revflow_patient_id="10284960",
                    first_name="MARIA",
                    last_name="MARIN",
                    payor="AMERICAN SPECIALTY HEALTH",
                    source_file="AMERICAN SPECIALTY HEALTH - 109748248.csv",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt(
                    "55200111",
                    "2026-03-14",
                    "97110",
                    patient_name="Marin, Maria",
                    name_key="MARINMARIA",
                ),
                payment=_make_payment(
                    cpt="97110",
                    paid=36.78,
                    check="109748248",
                    eob_date="05/13/2026",
                    date_of_service="03/14/2026",
                    name_key="MARINMARIA",
                    revflow_patient_id="10284960",
                    first_name="MARIA",
                    last_name="MARIN",
                    payor="AMERICAN SPECIALTY HEALTH",
                    source_file="AMERICAN SPECIALTY HEALTH - 109748248.csv",
                ),
                status="paid",
            ),
        ]
        bonuses = [
            _make_payment(
                cpt="",
                paid=0.99,
                check="109748248",
                eob_date="05/13/2026",
                date_of_service="",
                name_key="MARINMARIA",
                revflow_patient_id="10284960",
                first_name="MARIA",
                last_name="MARIN",
                payor="AMERICAN SPECIALTY HEALTH",
                carcs="OA-161",
                source_file="AMERICAN SPECIALTY HEALTH - 109748248.csv",
                units=0,
            ),
            _make_payment(
                cpt="",
                paid=2.48,
                check="109748248",
                eob_date="05/13/2026",
                date_of_service="",
                name_key="MARINMARIA",
                revflow_patient_id="10284960",
                first_name="MARIA",
                last_name="MARIN",
                payor="AMERICAN SPECIALTY HEALTH",
                carcs="OA-144",
                source_file="AMERICAN SPECIALTY HEALTH - 109748248.csv",
                units=0,
            ),
        ]
        visit = aggregate_visits(lines, bonuses)[0]
        self.assertEqual(visit["matched_paid"], "99.32")
        self.assertEqual(visit["bonus_paid"], "3.47")
        self.assertEqual(visit["total_paid"], "102.79")
        self.assertEqual(visit["unmatched_paid"], "0.00")
        self.assertEqual(visit["visit_paid_total"], "102.79")
        self.assertEqual(visit["primary_check_amount"], "102.79")

    def test_bonus_prefers_same_source_file_then_earliest_dos(self):
        early = MatchedLine(
            webpt=_make_webpt(
                "55200111",
                "2026-03-01",
                "97110",
                patient_name="Marin, Maria",
                name_key="MARINMARIA",
            ),
            payment=_make_payment(
                cpt="97110",
                paid=20.0,
                check="CHECK-EARLY",
                eob_date="04/01/2026",
                date_of_service="03/01/2026",
                name_key="MARINMARIA",
                revflow_patient_id="10284960",
                first_name="MARIA",
                last_name="MARIN",
                source_file="other.csv",
            ),
            status="paid",
        )
        late = MatchedLine(
            webpt=_make_webpt(
                "55200111",
                "2026-03-14",
                "97112",
                patient_name="Marin, Maria",
                name_key="MARINMARIA",
            ),
            payment=_make_payment(
                cpt="97112",
                paid=30.0,
                check="109748248",
                eob_date="05/13/2026",
                date_of_service="03/14/2026",
                name_key="MARINMARIA",
                revflow_patient_id="10284960",
                first_name="MARIA",
                last_name="MARIN",
                source_file="ash.csv",
            ),
            status="paid",
        )
        bonus = _make_payment(
            cpt="",
            paid=2.48,
            check="109748248",
            eob_date="05/13/2026",
            date_of_service="",
            name_key="MARINMARIA",
            revflow_patient_id="10284960",
            first_name="MARIA",
            last_name="MARIN",
            carcs="OA-144",
            source_file="ash.csv",
            units=0,
        )
        visits = {
            row["date_of_service"]: row
            for row in aggregate_visits([early, late], [bonus])
        }
        self.assertEqual(visits["2026-03-14"]["bonus_paid"], "2.48")
        self.assertEqual(visits["2026-03-14"]["total_paid"], "32.48")
        self.assertEqual(visits["2026-03-01"]["bonus_paid"], "0.00")
        self.assertEqual(visits["2026-03-01"]["total_paid"], "20.00")

    def test_bonus_without_matched_visit_stays_unattached(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("55200111", "2026-03-14", "97110"),
                payment=_make_payment(
                    cpt="97110",
                    paid=10.0,
                    check="A",
                    eob_date="05/13/2026",
                    revflow_patient_id="OTHER",
                ),
                status="paid",
            )
        ]
        bonus = _make_payment(
            cpt="",
            paid=0.99,
            check="109748248",
            eob_date="05/13/2026",
            date_of_service="",
            revflow_patient_id="10284960",
            carcs="OA-161",
            units=0,
        )
        visit = aggregate_visits(lines, [bonus])[0]
        self.assertEqual(visit["bonus_paid"], "0.00")
        self.assertEqual(visit["total_paid"], "10.00")

    def test_bonus_requires_same_check_or_source_file(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "55200111",
                    "2026-03-14",
                    "97110",
                    patient_name="Marin, Maria",
                    name_key="MARINMARIA",
                ),
                payment=_make_payment(
                    cpt="97110",
                    paid=30.0,
                    check="OTHER-CHECK",
                    eob_date="05/13/2026",
                    date_of_service="03/14/2026",
                    name_key="MARINMARIA",
                    revflow_patient_id="10284960",
                    first_name="MARIA",
                    last_name="MARIN",
                    source_file="other.csv",
                ),
                status="paid",
            )
        ]
        bonus = _make_payment(
            cpt="",
            paid=2.48,
            check="109748248",
            eob_date="05/13/2026",
            date_of_service="",
            name_key="MARINMARIA",
            revflow_patient_id="10284960",
            first_name="MARIA",
            last_name="MARIN",
            carcs="OA-144",
            source_file="ash.csv",
            units=0,
        )
        visit = aggregate_visits(lines, [bonus])[0]
        self.assertEqual(visit["bonus_paid"], "0.00")
        self.assertEqual(visit["total_paid"], "30.00")

    def test_deposit_dates_override_eob_and_reorder(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97163"),
                payment=_make_payment(
                    cpt="97163", paid=86.36, check="CHECK-A", eob_date="06/01/2026"
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97110"),
                payment=_make_payment(
                    cpt="97110", paid=38.81, check="CHECK-B", eob_date="06/02/2026"
                ),
                status="paid",
            ),
        ]
        # Without deposits: CHECK-A (06/01) is primary.
        visit = aggregate_visits(lines)[0]
        self.assertEqual(visit["primary_check_number"], "CHECK-A")
        self.assertEqual(visit["primary_check_date"], "06/01/2026")

        # Bank dates flip chronology: CHECK-B deposited earlier than CHECK-A.
        deposit_dates = {
            "CHECK-A": "06/12/2026",
            "CHECK-B": "06/10/2026",
        }
        visit = aggregate_visits(lines, deposit_dates=deposit_dates)[0]
        self.assertEqual(visit["primary_check_number"], "CHECK-B")
        self.assertEqual(visit["primary_check_date"], "06/10/2026")
        self.assertEqual(visit["secondary_check_number"], "CHECK-A")
        self.assertEqual(visit["secondary_check_date"], "06/12/2026")

    def test_deposit_date_miss_keeps_eob_date(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt("999001", "2026-06-11", "97163"),
                payment=_make_payment(
                    cpt="97163", paid=50.0, check="UNKNOWN", eob_date="07/02/2026"
                ),
                status="paid",
            )
        ]
        visit = aggregate_visits(lines, deposit_dates={"OTHER": "07/10/2026"})[0]
        self.assertEqual(visit["primary_check_date"], "07/02/2026")

    def test_orphan_does_not_attach_when_name_dos_ambiguous(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "55159334",
                    "2026-06-19",
                    "97110",
                    patient_name="Rodriguez, Maria",
                    name_key="RODRIGUEZMARIA",
                    facility_name="Central Harlem",
                ),
                payment=_make_payment(
                    cpt="97110",
                    paid=30.0,
                    check="A",
                    eob_date="07/01/2026",
                    date_of_service="2026-06-19",
                    name_key="RODRIGUEZMARIA",
                    revflow_patient_id="RF-A",
                    first_name="MARIA",
                    last_name="RODRIGUEZ",
                ),
                status="paid",
            ),
            MatchedLine(
                webpt=_make_webpt(
                    "55159999",
                    "2026-06-19",
                    "97530",
                    patient_name="Rodriguez, Maria",
                    name_key="RODRIGUEZMARIA",
                    facility_name="Bay Ridge",
                ),
                payment=_make_payment(
                    cpt="97530",
                    paid=40.0,
                    check="B",
                    eob_date="07/01/2026",
                    date_of_service="2026-06-19",
                    name_key="RODRIGUEZMARIA",
                    revflow_patient_id="RF-B",
                    first_name="MARIA",
                    last_name="RODRIGUEZ",
                ),
                status="paid",
            ),
        ]
        orphan = _make_payment(
            cpt="G0283",
            paid=8.32,
            check="ORPHAN",
            eob_date="07/01/2026",
            date_of_service="2026-06-19",
            name_key="RODRIGUEZMARIA",
            revflow_patient_id="RF-OTHER",
            first_name="MARIA",
            last_name="RODRIGUEZ",
        )
        visits = aggregate_visits(lines, [orphan])
        self.assertEqual(len(visits), 2)
        self.assertTrue(all(v["unmatched_paid"] == "0.00" for v in visits))
        self.assertTrue(all(v["unmatched_cpts"] == "" for v in visits))


class CollisionAssignmentTests(unittest.TestCase):
    def test_optimal_assignment_beats_greedy_trap(self):
        # Visit A: 95/94, Visit B: 94/10 — greedy picks 105, optimal picks 188.
        score = np.array(
            [
                [95.0, 94.0],  # Person1 -> A, B
                [94.0, 10.0],  # Person2 -> A, B
            ]
        )
        assigned = optimal_assignment(score, score_floor=0.0)
        pairs = {(row, col) for row, col, _ in assigned}
        self.assertEqual(pairs, {(0, 1), (1, 0)})
        self.assertAlmostEqual(sum(s for _, _, s in assigned), 188.0)

    def test_same_name_same_day_different_patients_partition(self):
        rules = load_insurance_rules()
        dos = "2026-06-19"
        name_key = "RODRIGUEZMARIA"
        webpt_lines = [
            _make_webpt(
                "55159334",
                dos,
                "97110",
                patient_name="Rodriguez, Maria",
                name_key=name_key,
                facility_name="Central Harlem",
            ),
            _make_webpt(
                "55159334",
                dos,
                "97530",
                patient_name="Rodriguez, Maria",
                name_key=name_key,
                facility_name="Central Harlem",
            ),
            _make_webpt(
                "55159999",
                dos,
                "97140",
                patient_name="Rodriguez, Maria",
                name_key=name_key,
                facility_name="Bay Ridge",
            ),
            _make_webpt(
                "55159999",
                dos,
                "G0283",
                patient_name="Rodriguez, Maria",
                name_key=name_key,
                facility_name="Bay Ridge",
            ),
        ]
        payments = [
            _make_payment(
                cpt="97110",
                paid=30.93,
                check="CHECK-HARLEM",
                eob_date="07/01/2026",
                date_of_service=dos,
                name_key=name_key,
                revflow_patient_id="RF-HARLEM",
                first_name="MARIA",
                last_name="RODRIGUEZ",
            ),
            _make_payment(
                cpt="97530",
                paid=18.33,
                check="CHECK-HARLEM",
                eob_date="07/01/2026",
                date_of_service=dos,
                name_key=name_key,
                revflow_patient_id="RF-HARLEM",
                first_name="MARIA",
                last_name="RODRIGUEZ",
            ),
            _make_payment(
                cpt="97140",
                paid=22.10,
                check="CHECK-BAY",
                eob_date="07/02/2026",
                date_of_service=dos,
                name_key=name_key,
                revflow_patient_id="RF-BAY",
                first_name="MARIA",
                last_name="RODRIGUEZ",
            ),
            _make_payment(
                cpt="G0283",
                paid=8.32,
                check="CHECK-BAY",
                eob_date="07/02/2026",
                date_of_service=dos,
                name_key=name_key,
                revflow_patient_id="RF-BAY",
                first_name="MARIA",
                last_name="RODRIGUEZ",
            ),
        ]
        result = match_lines(webpt_lines, payments, rules)
        by_patient: dict[str, list] = {}
        for item in result.lines:
            by_patient.setdefault(item.webpt.patient_id, []).append(item)

        harlem = by_patient["55159334"]
        bay = by_patient["55159999"]
        self.assertTrue(all(item.payment is not None for item in harlem))
        self.assertTrue(all(item.payment is not None for item in bay))
        self.assertTrue(
            all(item.payment.revflow_patient_id == "RF-HARLEM" for item in harlem)
        )
        self.assertTrue(
            all(item.payment.revflow_patient_id == "RF-BAY" for item in bay)
        )

        visits = aggregate_visits(result.lines, result.orphan_payments)
        visits_by_id = {v["webpt_patient_id"]: v for v in visits}
        self.assertEqual(visits_by_id["55159334"]["matched_paid"], "49.26")
        self.assertEqual(visits_by_id["55159999"]["matched_paid"], "30.42")
        self.assertEqual(visits_by_id["55159334"]["unmatched_paid"], "0.00")
        self.assertEqual(visits_by_id["55159999"]["unmatched_paid"], "0.00")


class TransactionTrackerLoaderTests(unittest.TestCase):
    def test_load_sample_tracker(self):
        dates = load_deposit_dates(FIXTURES / "transaction_tracker_sample.xlsx")
        self.assertEqual(dates["CHECK-A"], "06/10/2026")  # earliest of duplicates
        self.assertEqual(dates["CHECK-B"], "06/12/2026")
        self.assertEqual(dates["ALT-B"], "06/12/2026")
        self.assertEqual(dates["PAPER-1"], "06/08/2026")
        self.assertNotIn("#N/A", dates)


class TrackerPartitionTests(unittest.TestCase):
    def test_partition_keeps_only_tracked_checks(self):
        tracked = _make_payment(cpt="97110", paid=30.0, check="CHECK-A", eob_date="06/01/2026")
        missing = _make_payment(cpt="97112", paid=20.0, check="CHECK-Z", eob_date="06/02/2026")
        empty = _make_payment(cpt="97140", paid=10.0, check="", eob_date="06/03/2026")
        in_tracker, not_in_tracker = _partition_payments_by_tracker(
            [tracked, missing, empty],
            {"CHECK-A"},
        )
        self.assertEqual(in_tracker, [tracked])
        self.assertEqual(not_in_tracker, [missing, empty])

        webpt_lines = [
            _make_webpt("999001", "2026-06-11", "97110"),
            _make_webpt("999001", "2026-06-11", "97112"),
        ]
        rules = load_insurance_rules(None)
        result = match_lines(webpt_lines, in_tracker, rules)
        matched_checks = {
            item.payment.check_eft_num
            for item in result.lines
            if item.payment is not None
        }
        self.assertEqual(matched_checks, {"CHECK-A"})
        self.assertNotIn("CHECK-Z", matched_checks)

        visits = aggregate_visits(result.lines, result.orphan_payments)
        visit_checks = {
            visit["primary_check_number"]
            for visit in visits
            if visit["primary_check_number"]
        }
        self.assertTrue(visit_checks <= {"CHECK-A"})

        rows = _checks_not_in_tracker_rows(not_in_tracker)
        by_check = {row["check_eft_num"]: row for row in rows}
        self.assertIn("CHECK-Z", by_check)
        self.assertIn("(empty)", by_check)
        self.assertEqual(by_check["CHECK-Z"]["line_count"], 1)
        self.assertEqual(by_check["CHECK-Z"]["paid_amount_sum"], "20.00")


class CaseCentricVisitKeyTests(unittest.TestCase):
    def test_same_patient_dos_two_cases_two_visits(self):
        lines = [
            MatchedLine(
                webpt=_make_webpt(
                    "52985234",
                    "2026-07-20",
                    "97110",
                    case_id="70972991",
                    facility_id="42",
                ),
                status="pending",
            ),
            MatchedLine(
                webpt=_make_webpt(
                    "52985234",
                    "2026-07-20",
                    "97140",
                    case_id="70000001",
                    facility_id="42",
                ),
                status="pending",
            ),
        ]
        visits = aggregate_visits(lines)
        self.assertEqual(len(visits), 2)
        case_ids = sorted(v["case_id"] for v in visits)
        self.assertEqual(case_ids, ["70000001", "70972991"])
        for v in visits:
            self.assertEqual(v["facility_id"], "42")
            self.assertEqual(v["webpt_patient_id"], "52985234")
            self.assertEqual(v["date_of_service"], "2026-07-20")


if __name__ == "__main__":
    unittest.main()
