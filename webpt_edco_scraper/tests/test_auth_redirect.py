"""Unit tests for auth redirect URL detection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auth import _is_auth_redirect_url, _is_login_url


def test_auth_redirect_detects_login_webpt() -> None:
    assert _is_auth_redirect_url("https://login.webpt.com/u/login?state=abc")


def test_auth_redirect_detects_auth_webpt() -> None:
    assert _is_auth_redirect_url("https://auth.webpt.com/authorize?client_id=x")


def test_auth_redirect_detects_auth0_host() -> None:
    assert _is_auth_redirect_url("https://webpt.auth0.com/login?state=xyz")


def test_auth_redirect_rejects_app_page() -> None:
    assert not _is_auth_redirect_url("https://app.webpt.com/scheduler/index.php")


def test_login_url_alias() -> None:
    assert _is_login_url("https://login.webpt.com/") == _is_auth_redirect_url(
        "https://login.webpt.com/"
    )
