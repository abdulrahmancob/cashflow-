"""Session expiry detection and export recovery tests."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from auth import (
    SessionExpiredError,
    _is_login_url,
    is_session_expired_page,
)
from config import RevFlowConfig
from scraper import _export_with_recovery


class SessionDetectionTests(unittest.TestCase):
    def test_login_url_detected(self):
        self.assertTrue(_is_login_url("https://billing.revflow.com/login"))
        self.assertFalse(_is_login_url("https://billing.revflow.com/dashboard"))

    def test_session_expired_error_is_exception(self):
        self.assertTrue(issubclass(SessionExpiredError, Exception))


class SessionDetectionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_expired_on_login_url(self):
        page = MagicMock()
        page.url = "https://billing.revflow.com/login"
        page.locator.return_value.count = AsyncMock(return_value=0)
        self.assertTrue(await is_session_expired_page(page))

    async def test_session_expired_on_server_error_text(self):
        page = MagicMock()
        page.url = "https://billing.revflow.com/report/sub_report_data"
        body = MagicMock()
        body.inner_text = AsyncMock(return_value="Server error. Please sign in again.")
        page.locator.return_value = body
        sign_in = MagicMock()
        sign_in.count = AsyncMock(return_value=0)
        page.locator.side_effect = lambda sel: body if sel == "body" else sign_in
        self.assertTrue(await is_session_expired_page(page))


class ExportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_after_session_expired(self):
        config = RevFlowConfig(
            username="user",
            password="pass",
            export_retry_max=2,
            reauth_cooldown_sec=0,
        )
        state = MagicMock()
        state.bearer_token = "token"
        state.user_id = "1"
        state.company_id = "2"

        selection = {
            "company_id": "11067",
            "from_date": "06/01/2026",
            "to_date": "06/30/2026",
            "eob_key": "1",
            "check_eft_num": "CHK1",
            "payor": "TEST",
            "eob_date": "06/01/2026",
        }

        export_mock = AsyncMock(
            side_effect=[
                SessionExpiredError("logged out"),
                {"key": "k", "status": "ok", "path": "/tmp/x.csv", "selection": selection},
            ]
        )
        reauth_mock = AsyncMock(return_value=state)

        with patch("scraper.export_eob_spreadsheet", export_mock), patch(
            "scraper.reauthenticate", reauth_mock
        ), patch("scraper.save_storage_state", AsyncMock()):
            result, _new_state = await _export_with_recovery(
                MagicMock(),
                MagicMock(),
                config,
                state,
                selection,
                MagicMock(),
                skip_existing=True,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(export_mock.await_count, 2)
        reauth_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
