"""Tests for insurance payment behavior helpers."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from cashflow_reconcile.insurance_behavior import (
    build_checks_timeline,
    build_insurance_behavior,
    build_payor_summaries,
    label_cadence,
)
from openpyxl import Workbook


class CadenceLabelTests(unittest.TestCase):
    def test_weekly_friday(self):
        # Five consecutive Fridays
        start = date(2026, 1, 2)  # Friday
        deposits = [start + timedelta(days=7 * i) for i in range(5)]
        label, top_day, profile = label_cadence(deposits)
        self.assertEqual(top_day, "Fri")
        self.assertTrue(label.startswith("weekly"))
        self.assertIn("fri", label)
        self.assertIn("Fri", profile)

    def test_biweekly(self):
        start = date(2026, 1, 6)  # Tuesday
        deposits = [start + timedelta(days=14 * i) for i in range(5)]
        label, top_day, _profile = label_cadence(deposits)
        self.assertEqual(top_day, "Tue")
        self.assertTrue(label.startswith("biweekly"))

    def test_insufficient(self):
        label, top_day, _profile = label_cadence([date(2026, 1, 2)])
        self.assertEqual(label, "insufficient_history")
        self.assertEqual(top_day, "Fri")

    def test_near_daily(self):
        # 12 consecutive weekdays with gap median 1
        start = date(2026, 1, 5)  # Monday
        deposits: list[date] = []
        cur = start
        while len(deposits) < 12:
            if cur.weekday() < 5:
                deposits.append(cur)
            cur += timedelta(days=1)
        label, _top, profile = label_cadence(deposits)
        self.assertEqual(label, "near_daily")
        self.assertTrue(profile)

    def test_multi_weekday_tue_thu(self):
        deposits: list[date] = []
        # Alternate Tue/Thu over several weeks (neither alone >= 45%)
        for week in range(8):
            monday = date(2026, 1, 5) + timedelta(days=7 * week)
            deposits.append(monday + timedelta(days=1))  # Tue
            deposits.append(monday + timedelta(days=3))  # Thu
        label, top_day, profile = label_cadence(deposits)
        self.assertTrue(label.startswith("multi_weekday_"))
        self.assertIn("tue", label)
        self.assertIn("thu", label)
        self.assertIn(top_day, {"Tue", "Thu"})
        self.assertIn("%", profile)


class TimelineLagTests(unittest.TestCase):
    def test_eob_to_deposit_lag_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payments = tmp_path / "payments.csv"
            with payments.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "payor",
                        "check_eft_num",
                        "eob_date",
                        "date_of_service",
                        "paid_amount",
                        "webpt_patient_id",
                    ],
                )
                writer.writeheader()
                # Fri EOB -> Mon deposit (+3)
                writer.writerow(
                    {
                        "payor": "AETNA",
                        "check_eft_num": "CHECK-A",
                        "eob_date": "01/02/2026",
                        "date_of_service": "2025-12-15",
                        "paid_amount": "100.00",
                        "webpt_patient_id": "1",
                    }
                )
                writer.writerow(
                    {
                        "payor": "AETNA",
                        "check_eft_num": "CHECK-B",
                        "eob_date": "01/09/2026",
                        "date_of_service": "2025-12-20",
                        "paid_amount": "50.00",
                        "webpt_patient_id": "1",
                    }
                )

            deposit_dates = {
                "CHECK-A": date(2026, 1, 5),  # Mon
                "CHECK-B": date(2026, 1, 12),  # Mon
            }
            checks = build_checks_timeline(
                payments_path=payments,
                deposit_dates=deposit_dates,
                ins_by_check={"CHECK-A": "Aetna", "CHECK-B": "Aetna"},
                patient_ins={},
                dos_samples={
                    "CHECK-A": [18],
                    "CHECK-B": [20],
                },
            )
            self.assertEqual(len(checks), 2)
            by_check = {row["check_eft_num"]: row for row in checks}
            self.assertEqual(by_check["CHECK-A"]["eob_weekday"], "Fri")
            self.assertEqual(by_check["CHECK-A"]["deposit_weekday"], "Mon")
            self.assertEqual(by_check["CHECK-A"]["check_to_deposit_days"], "3")

            summaries = build_payor_summaries(checks)
            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertEqual(summary["payor"], "AETNA")
            self.assertEqual(summary["payer_org_code"], "AETNA")
            self.assertEqual(summary["eob_to_deposit_median"], 3)
            self.assertEqual(summary["top_eob_weekday"], "Fri")
            self.assertEqual(summary["top_deposit_weekday"], "Mon")
            self.assertEqual(summary["deposit_coverage_pct"], "100.0")
            # DOS medians 18 and 20 -> median 19; + deposit 3 => velocity 22
            self.assertEqual(summary["cash_velocity_median"], 22)
            self.assertEqual(summary["avg_paid_per_check"], "75.00")

    def test_no_double_count_lines_on_same_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payments = tmp_path / "payments.csv"
            with payments.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "payor",
                        "check_eft_num",
                        "eob_date",
                        "date_of_service",
                        "paid_amount",
                        "webpt_patient_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "payor": "UHC",
                        "check_eft_num": "SAME",
                        "eob_date": "01/02/2026",
                        "date_of_service": "2025-12-01",
                        "paid_amount": "40.00",
                        "webpt_patient_id": "1",
                    }
                )
                writer.writerow(
                    {
                        "payor": "UHC",
                        "check_eft_num": "SAME",
                        "eob_date": "01/02/2026",
                        "date_of_service": "2025-12-01",
                        "paid_amount": "60.00",
                        "webpt_patient_id": "1",
                    }
                )

            checks = build_checks_timeline(
                payments_path=payments,
                deposit_dates={"SAME": date(2026, 1, 5)},
                ins_by_check={},
                patient_ins={},
                dos_samples={},
            )
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["paid_amount_sum"], "100.00")
            self.assertEqual(checks[0]["line_count"], 2)

            summaries = build_payor_summaries(checks)
            self.assertEqual(summaries[0]["paid_amount_sum"], "100.00")
            self.assertEqual(summaries[0]["n_checks"], 1)


class EndToEndBehaviorTests(unittest.TestCase):
    def test_build_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payments = tmp_path / "payments.csv"
            lines = tmp_path / "lines.csv"
            tracker = tmp_path / "tracker.xlsx"
            out = tmp_path / "out"

            with payments.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "payor",
                        "check_eft_num",
                        "eob_date",
                        "date_of_service",
                        "paid_amount",
                        "webpt_patient_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "payor": "UHC",
                        "check_eft_num": "111",
                        "eob_date": "01/02/2026",
                        "date_of_service": "2025-12-10",
                        "paid_amount": "10.00",
                        "webpt_patient_id": "9",
                    }
                )

            with lines.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "status",
                        "check_eft_num",
                        "ins_name",
                        "date_of_service",
                        "eob_date",
                        "webpt_patient_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "status": "paid",
                        "check_eft_num": "111",
                        "ins_name": "United Healthcare",
                        "date_of_service": "2025-12-10",
                        "eob_date": "01/02/2026",
                        "webpt_patient_id": "9",
                    }
                )

            wb = Workbook()
            ws = wb.active
            ws.title = "January"
            ws.append(
                [
                    "Payment ID",
                    "Month",
                    "Date",
                    "Amount",
                    "EFT_1",
                    "EFT_2",
                    "#Check/Reference",
                ]
            )
            ws.append(
                [
                    "Jan-001",
                    "January",
                    date(2026, 1, 5),
                    10.0,
                    "111",
                    None,
                    None,
                ]
            )
            wb.save(tracker)

            summary = build_insurance_behavior(
                payments_path=payments,
                lines_path=lines,
                transaction_tracker=tracker,
                output_dir=out,
            )
            self.assertEqual(summary["n_checks"], 1)
            self.assertTrue((out / "checks_timeline.csv").exists())
            self.assertTrue((out / "payor_behavior_summary.csv").exists())
            self.assertTrue((out / "payor_weekday_heatmap.csv").exists())


if __name__ == "__main__":
    unittest.main()
