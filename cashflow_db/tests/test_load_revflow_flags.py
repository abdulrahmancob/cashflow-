"""Unit tests for RevFlow loader daily-path flags (no DB)."""

from __future__ import annotations

import os

from cashflow_db.loaders.load_revflow import _bootstrap_claims_default


def test_bootstrap_claims_default_off():
    os.environ.pop("CASHFLOW_REVFLOW_BOOTSTRAP_CLAIMS", None)
    assert _bootstrap_claims_default(None) is False


def test_bootstrap_claims_explicit_true():
    assert _bootstrap_claims_default(True) is True


def test_bootstrap_claims_env_on(monkeypatch):
    monkeypatch.setenv("CASHFLOW_REVFLOW_BOOTSTRAP_CLAIMS", "1")
    assert _bootstrap_claims_default(None) is True


def test_bootstrap_claims_env_off_overrides_nothing(monkeypatch):
    monkeypatch.setenv("CASHFLOW_REVFLOW_BOOTSTRAP_CLAIMS", "0")
    assert _bootstrap_claims_default(None) is False
    assert _bootstrap_claims_default(True) is True
