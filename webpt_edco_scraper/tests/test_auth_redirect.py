"""Unit tests for auth redirect URL detection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auth import (
    _is_auth_redirect_url,
    _is_login_url,
    _is_on_app_domain,
    _is_post_login_interstitial_url,
)


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


def test_post_login_interstitial_detects_redirect() -> None:
    assert _is_post_login_interstitial_url(
        "https://app.webpt.com/redirect/?cb=1783079356"
    )


def test_post_login_interstitial_detects_delegator() -> None:
    url = "https://delegator.webpt.com/authorization/"
    assert _is_post_login_interstitial_url(url)


def test_delegator_is_not_app_domain() -> None:
    """Delegator must not be treated as a completed Auth0 login."""
    url = "https://delegator.webpt.com/authorization/"
    assert _is_post_login_interstitial_url(url)
    assert not _is_on_app_domain(url)
    assert not _is_auth_redirect_url(url)


def test_app_dashboard_is_on_app_domain() -> None:
    url = "https://app.webpt.com/dashboard.php"
    assert _is_on_app_domain(url)
    assert not _is_post_login_interstitial_url(url)
