"""Offline smoke tests (no live RevFlow/Gmail credentials required)."""

import json
import tempfile
import unittest
from pathlib import Path

from auth import decode_jwt_payload, _is_authenticated_url, DASHBOARD_MARKERS
from export import export_filename, selection_key, _sanitize_filename_display
from reports_api import load_selections, write_eob_catalog, EobCatalogEntry
from scraper import build_parser


class SmokeTests(unittest.TestCase):
    def test_cli_parser_has_commands(self):
        parser = build_parser()
        args = parser.parse_args(["login"])
        self.assertEqual(args.command, "login")

    def test_cli_parser_has_export_all(self):
        parser = build_parser()
        args = parser.parse_args(["export-all", "--output", "output/jun_2026"])
        self.assertEqual(args.command, "export-all")
        self.assertEqual(args.output, "output/jun_2026")

    def test_authenticated_url_recognizes_dashboard(self):
        self.assertTrue(
            _is_authenticated_url("https://billing.revflow.com/dashboard")
        )
        self.assertFalse(
            _is_authenticated_url("https://billing.revflow.com/login")
        )

    def test_dashboard_markers_defined(self):
        self.assertGreater(len(DASHBOARD_MARKERS), 0)

    def test_jwt_decode_extracts_ids(self):
        # Sample payload only (signature not verified)
        import base64

        payload = {
            "UserID": "46136",
            "CompanyID": "11067",
            "UserName": "test-user",
        }
        encoded = (
            base64.urlsafe_b64encode(json.dumps(payload).encode())
            .decode()
            .rstrip("=")
        )
        token = f"header.{encoded}.signature"
        decoded = decode_jwt_payload(token)
        self.assertEqual(decoded["UserID"], "46136")
        self.assertEqual(decoded["CompanyID"], "11067")

    def test_catalog_to_selections_roundtrip(self):
        entry = EobCatalogEntry(
            company_id="11067",
            company_code="PV4",
            company_name="Physical Therapy of The City",
            from_date="06/01/2026",
            to_date="07/02/2026",
            clinic_code="PV4",
            eob_key="23941775",
            check_eft_num="26147B1001154298",
            payor="UNITEDHEALTHCARE OF NEW YORK INC",
            eob_date="06/01/2026",
            detail_url="https://billing.revflow.com/report/sub_report_data?rid=67",
        )
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "eob_catalog.json"
            write_eob_catalog(catalog_path, [entry], meta={"test": True})
            selections = load_selections(catalog_path)
            self.assertEqual(len(selections), 1)
            self.assertEqual(selections[0]["eob_key"], "23941775")
            filename = export_filename(selections[0])
            self.assertEqual(
                filename,
                "UNITEDHEALTHCARE OF NEW YORK INC - 26147B1001154298.csv",
            )
            key = selection_key(selections[0])
            self.assertIn("23941775", key)


class ExportFilenameTests(unittest.TestCase):
    def test_payor_check_number_format(self):
        selection = {
            "payor": "MOLINA HEALTHCARE OF NEW YORK, INC.",
            "check_eft_num": "1246736057",
        }
        self.assertEqual(
            export_filename(selection),
            "MOLINA HEALTHCARE OF NEW YORK, INC. - 1246736057.csv",
        )

    def test_strips_invalid_characters(self):
        selection = {
            "payor": 'BAD<>PAYER:"NAME',
            "check_eft_num": "check/1",
        }
        filename = export_filename(selection)
        self.assertIn(" - ", filename)
        self.assertNotIn("<", filename)
        self.assertNotIn("/", filename)
        self.assertTrue(filename.endswith(".csv"))

    def test_respects_download_extension(self):
        selection = {
            "payor": "AETNA",
            "check_eft_num": "826182000214418",
        }
        self.assertEqual(
            export_filename(selection, ".xlsx"),
            "AETNA - 826182000214418.xlsx",
        )

    def test_sanitize_truncates_long_payor(self):
        long_name = "A" * 200
        cleaned = _sanitize_filename_display(long_name, max_len=150)
        self.assertEqual(len(cleaned), 150)


if __name__ == "__main__":
    unittest.main()
