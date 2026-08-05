"""Unit tests for JWT TTL helper and auth_expired download errors."""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auth import AUTH_EXPIRED, vega_jwt_seconds_remaining
from edoc_download import is_auth_expired_error


def _jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return f"{header}.{payload}.sig"


def test_vega_jwt_seconds_remaining_ok() -> None:
    exp = int(time.time()) + 600
    cookies = [{"name": "vega_emr_auth", "value": _jwt_with_exp(exp)}]
    remaining = vega_jwt_seconds_remaining(cookies)
    assert remaining is not None
    assert 590 <= remaining <= 600


def test_vega_jwt_seconds_remaining_missing() -> None:
    assert vega_jwt_seconds_remaining([]) is None
    assert vega_jwt_seconds_remaining([{"name": "IDEM", "value": "x"}]) is None


def test_is_auth_expired_error() -> None:
    assert is_auth_expired_error(AUTH_EXPIRED)
    assert is_auth_expired_error(f"{AUTH_EXPIRED}: redirect")
    assert not is_auth_expired_error("HTTP 500")
    assert not is_auth_expired_error(None)
