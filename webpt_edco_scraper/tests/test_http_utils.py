"""Unit tests for HTTP / browser error classification."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from http_utils import is_browser_connection_lost, is_transient_network_error


def test_browser_connection_lost_detects_playwright_driver_error() -> None:
    exc = Exception(
        "Page.goto: Connection closed while reading from the driver"
    )
    assert is_browser_connection_lost(exc)


def test_browser_connection_lost_rejects_generic_network() -> None:
    assert not is_browser_connection_lost(Exception("ECONNRESET"))


def test_transient_network_excludes_driver_death() -> None:
    exc = Exception(
        "APIRequestContext.get: Connection closed while reading from the driver"
    )
    assert is_browser_connection_lost(exc)
    assert not is_transient_network_error(exc)


def test_transient_network_still_matches_reset() -> None:
    assert is_transient_network_error(Exception("read ECONNRESET"))
