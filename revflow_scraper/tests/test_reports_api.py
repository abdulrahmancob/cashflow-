"""Tests for RevFlow report link parsing and catalog building."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from config import COMPANY_EOB_LOG_REPORT_ID, OPEN_835_REPORT_ID, RevFlowConfig
from reports_api import (
    ReportParams,
    ReportsClient,
    _params_from_link,
    _report_rows,
    build_eob_catalog,
    build_silversurfer,
    discover_eobs,
    parse_report_link,
)

SAMPLE_METADATA = {
    "reportParameters": [
        {
            "sequence": 1,
            "label_name": "From Date",
            "name": "Fdate",
            "default_value": "01/01/2026",
            "param_name": "@Fdate",
            "report_param_id": 197,
        },
        {
            "sequence": 2,
            "label_name": "Through Date",
            "name": "Tdate",
            "default_value": "01/31/2026",
            "param_name": "@Tdate",
            "report_param_id": 198,
        },
        {
            "sequence": 3,
            "label_name": "User",
            "name": "UserId",
            "default_value": "0",
            "param_name": "@UserId",
            "report_param_id": 554,
        },
    ]
}


class SilversurferTests(unittest.TestCase):
    def test_build_silversurfer_applies_overrides(self):
        header = build_silversurfer(
            SAMPLE_METADATA,
            {"Fdate": "06/01/2026", "Tdate": "07/30/2026", "UserId": "46136"},
        )
        params = json.loads(header)
        self.assertEqual(params[0]["default_value"], "06/01/2026")
        self.assertEqual(params[1]["default_value"], "07/30/2026")
        self.assertEqual(params[2]["default_value"], "46136")

    def test_build_silversurfer_fallback_without_metadata(self):
        header = build_silversurfer({}, {"Fdate": "06/01/2026", "Tdate": "07/02/2026"})
        params = json.loads(header)
        self.assertEqual(len(params), 2)
        self.assertEqual(params[0]["default_value"], "06/01/2026")


class ReportParamsUrlTests(unittest.TestCase):
    def test_report_data_ui_url_is_bare_page(self):
        params = ReportParams(
            rid=str(OPEN_835_REPORT_ID),
            from_date="06/01/2026",
            to_date="07/02/2026",
            clinic_code="PV4",
        )
        url = params.report_data_ui_url()
        self.assertEqual(url, "https://billing.revflow.com/report/report_data")
        self.assertNotIn("rid=", url)
        self.assertNotIn("FDate", url)

    def test_ui_url_uses_encoded_query_blob(self):
        params = ReportParams(
            rid=str(COMPANY_EOB_LOG_REPORT_ID),
            from_date="06/01/2026",
            to_date="07/30/2026",
            clinic_code="PV4",
            company_id="11067",
        )
        url = params.ui_url()
        self.assertIn("billing.revflow.com/report/sub_report_data?", url)
        self.assertIn("rid%3D66", url)
        self.assertIn("company_id%3D11067", url)
        self.assertIn("FDate%3D06%2F01%2F2026", url)
        self.assertNotIn("rid=66", url)


class GetReportDataApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_report_data_uses_silversurfer_without_query_params(self):
        config = RevFlowConfig(
            username="u",
            password="p",
            clinic_code="PV4",
            user_id="46136",
        )
        request = MagicMock()
        metadata_resp = MagicMock()
        metadata_resp.ok = True
        metadata_resp.json = AsyncMock(return_value={"data": SAMPLE_METADATA})
        report_resp = MagicMock()
        report_resp.ok = True
        report_resp.json = AsyncMock(return_value={"data": {"ReportRows": []}})
        request.get = AsyncMock(side_effect=[metadata_resp, report_resp])

        client = ReportsClient(request, "token", config)
        result = await client.get_report_data(
            OPEN_835_REPORT_ID,
            from_date="06/01/2026",
            to_date="07/02/2026",
        )

        self.assertEqual(result, {"ReportRows": []})
        self.assertEqual(request.get.await_count, 2)

        report_call = request.get.await_args_list[1]
        report_url = report_call.args[0]
        report_headers = report_call.kwargs.get("headers") or report_call.args[1]
        self.assertEqual(report_url, f"https://r6prodgoldna.revflow.com/v1/reports/report_data/{OPEN_835_REPORT_ID}")
        self.assertNotIn("FDate=", report_url)
        self.assertIn("Silversurfer", report_headers)
        silversurfer = json.loads(report_headers["Silversurfer"])
        self.assertEqual(silversurfer[0]["default_value"], "06/01/2026")
        self.assertEqual(silversurfer[2]["default_value"], "46136")


class DiscoverEobsFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_uses_bare_report_data_page(self):
        page = MagicMock()
        context = MagicMock()
        config = RevFlowConfig(username="u", password="p", clinic_code="PV4", company_id="11067")
        client = ReportsClient(MagicMock(), "token", config)

        open_report = {"ReportRows": []}
        client.get_report_metadata = AsyncMock(return_value={})
        client.get_report_data = AsyncMock(
            side_effect=RuntimeError("report_data/68 failed: 500 error")
        )
        client.fetch_via_page = AsyncMock(return_value=open_report)

        await discover_eobs(
            page,
            context,
            client,
            config,
            from_date="06/01/2026",
            to_date="07/02/2026",
        )

        client.fetch_via_page.assert_awaited_once()
        call_args = client.fetch_via_page.await_args
        ui_url = call_args.args[1]
        expect_fragment = call_args.kwargs.get("expect_url_fragment")
        self.assertEqual(ui_url, "https://billing.revflow.com/report/report_data")
        self.assertEqual(expect_fragment, f"report_data/{OPEN_835_REPORT_ID}")


class ParamsFromLinkTests(unittest.TestCase):
    def test_prefers_link_dates_over_cli_fallback(self):
        link_params = {
            "rid": "66",
            "company_id": "11067",
            "FDate": "07/01/2026",
            "Tdate": "07/02/2026",
            "cliniccode": "PV4",
        }
        fallback = ReportParams(
            rid=str(COMPANY_EOB_LOG_REPORT_ID),
            from_date="06/01/2026",
            to_date="07/02/2026",
            clinic_code="PV4",
            company_id="11067",
        )
        params = _params_from_link(
            link_params,
            rid=str(COMPANY_EOB_LOG_REPORT_ID),
            fallback=fallback,
        )
        self.assertEqual(params.from_date, "07/01/2026")
        self.assertEqual(params.to_date, "07/02/2026")
        self.assertEqual(params.company_id, "11067")
        url = params.ui_url()
        self.assertIn("FDate%3D07%2F01%2F2026", url)
        self.assertIn("Tdate%3D07%2F02%2F2026", url)
        self.assertIn("company_id%3D11067", url)


class ReportRowsTests(unittest.TestCase):
    def test_none_returns_empty_list(self):
        self.assertEqual(_report_rows(None), [])

    def test_non_dict_returns_empty_list(self):
        self.assertEqual(_report_rows([]), [])


class DiscoverEobsCompanyUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_company_fetch_uses_link_dates_in_sub_report_url(self):
        page = MagicMock()
        context = MagicMock()
        config = RevFlowConfig(username="u", password="p", clinic_code="PV4", company_id="11067")
        client = ReportsClient(MagicMock(), "token", config)

        company_row_html = (
            "<a href=ReportSelection.aspx?rid=66&company_id=11067"
            "&FDate=07/01/2026&Tdate=07/02/2026&cliniccode=PV4><b>PV4</b></a>"
        )
        open_report = {
            "ReportRows": [
                {
                    "stringCol0": company_row_html,
                    "stringCol1": "Physical Therapy of The City",
                }
            ]
        }
        company_report = {"ReportRows": []}

        client.get_report_metadata = AsyncMock(return_value={})
        client.get_report_data = AsyncMock(return_value=open_report)
        client.get_sub_report_data = AsyncMock(return_value=company_report)
        client.fetch_via_page = AsyncMock()

        await discover_eobs(
            page,
            context,
            client,
            config,
            from_date="06/01/2026",
            to_date="07/02/2026",
        )

        client.get_sub_report_data.assert_awaited_once()
        params = client.get_sub_report_data.await_args.args[0]
        self.assertEqual(params.from_date, "07/01/2026")
        self.assertEqual(params.to_date, "07/02/2026")
        self.assertIn("FDate%3D07%2F01%2F2026", params.ui_url())

    async def test_null_sub_report_payload_triggers_browser_fallback(self):
        page = MagicMock()
        context = MagicMock()
        config = RevFlowConfig(username="u", password="p", clinic_code="PV4", company_id="11067")
        client = ReportsClient(MagicMock(), "token", config)

        open_report = {
            "ReportRows": [
                {
                    "stringCol0": (
                        "<a href=ReportSelection.aspx?rid=66&company_id=11067"
                        "&FDate=07/01/2026&Tdate=07/02/2026&cliniccode=PV4><b>PV4</b></a>"
                    ),
                    "stringCol1": "Physical Therapy of The City",
                }
            ]
        }
        company_report = {"ReportRows": []}

        client.get_report_metadata = AsyncMock(return_value={})
        client.get_report_data = AsyncMock(return_value=open_report)
        client.get_sub_report_data = AsyncMock(
            side_effect=RuntimeError("sub_report_data returned no report payload")
        )
        client.fetch_via_page = AsyncMock(return_value=company_report)

        await discover_eobs(
            page,
            context,
            client,
            config,
            from_date="06/01/2026",
            to_date="07/02/2026",
        )

        client.fetch_via_page.assert_awaited_once()
        ui_url = client.fetch_via_page.await_args.args[1]
        self.assertIn("rid%3D66", ui_url)
        self.assertIn("FDate%3D07%2F01%2F2026", ui_url)


class ParseReportLinkTests(unittest.TestCase):
    def test_parses_company_link(self):
        html = (
            '<a href=ReportSelection.aspx?rid=66&company_id=11067'
            "&FDate=06/01/2026&Tdate=07/02/2026&cliniccode=PV4><b>PV4</b></a>"
        )
        params = parse_report_link(html)
        self.assertEqual(params["rid"], "66")
        self.assertEqual(params["company_id"], "11067")
        self.assertEqual(params["FDate"], "06/01/2026")
        self.assertEqual(params["cliniccode"], "PV4")

    def test_parses_eob_detail_link(self):
        html = (
            '<a href="ReportSelection.aspx?rid=67&eob_key=23941775'
            "&FDate=06/01/2026&Tdate=07/02/2026&cliniccode=PV4"
            "&check_eft_num=26147B1001154298"
            "&Payor=UNITEDHEALTHCARE OF NEW YORK INC"
            '&eob_date=06/01/2026"><b>26147B1001154298</b></a>'
        )
        params = parse_report_link(html)
        self.assertEqual(params["rid"], "67")
        self.assertEqual(params["eob_key"], "23941775")
        self.assertEqual(params["check_eft_num"], "26147B1001154298")
        self.assertEqual(params["Payor"], "UNITEDHEALTHCARE OF NEW YORK INC")


class BuildCatalogTests(unittest.TestCase):
    def test_builds_unique_entries(self):
        company_row = {
            "stringCol0": (
                "<a href=ReportSelection.aspx?rid=66&company_id=11067"
                "&FDate=06/01/2026&Tdate=07/02/2026&cliniccode=PV4><b>PV4</b></a>"
            ),
            "stringCol1": "Physical Therapy of The City",
        }
        eob_row = {
            "stringCol0": (
                '<a href="ReportSelection.aspx?rid=67&eob_key=23941775'
                "&FDate=06/01/2026&Tdate=07/02/2026&cliniccode=PV4"
                "&check_eft_num=26147B1001154298"
                "&Payor=UNITEDHEALTHCARE OF NEW YORK INC"
                '&eob_date=06/01/2026"><b>26147B1001154298</b></a>'
            ),
            "stringCol1": "UNITEDHEALTHCARE OF NEW YORK INC",
            "stringCol2": "06/01/2026",
        }
        entries = build_eob_catalog(
            from_date="06/01/2026",
            to_date="07/02/2026",
            clinic_code="PV4",
            company_id="11067",
            open_835_rows=[company_row],
            company_rows=[eob_row, eob_row],
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.eob_key, "23941775")
        self.assertEqual(entry.check_eft_num, "26147B1001154298")
        self.assertIn("billing.revflow.com/report/sub_report_data", entry.detail_url)
        self.assertIn("rid%3D67", entry.detail_url)


if __name__ == "__main__":
    unittest.main()
