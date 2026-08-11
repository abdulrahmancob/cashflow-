"""Unit tests for Acquire case-download skip helpers (no scrapers)."""

from __future__ import annotations

import json

from cashflow_ops.stages import acquire as acquire_mod


def test_skip_case_download_env(monkeypatch):
    monkeypatch.setenv("CASHFLOW_OPS_SKIP_CASE_DOWNLOAD", "1")
    skip, reason = acquire_mod._should_skip_case_download()
    assert skip is True
    assert "CASHFLOW_OPS_SKIP_CASE_DOWNLOAD" in reason


def test_skip_case_download_when_remaining_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("CASHFLOW_OPS_SKIP_CASE_DOWNLOAD", raising=False)
    health = tmp_path / "reports" / "health.json"
    health.parent.mkdir(parents=True)
    health.write_text(json.dumps({"cases_remaining": 0}), encoding="utf-8")
    monkeypatch.setattr(acquire_mod, "CASE_PIPELINE_DIR", tmp_path)
    skip, reason = acquire_mod._should_skip_case_download()
    assert skip is True
    assert reason == "cases_remaining=0"


def test_no_skip_when_remaining_positive(monkeypatch, tmp_path):
    monkeypatch.delenv("CASHFLOW_OPS_SKIP_CASE_DOWNLOAD", raising=False)
    health = tmp_path / "reports" / "health.json"
    health.parent.mkdir(parents=True)
    health.write_text(json.dumps({"cases_remaining": 12}), encoding="utf-8")
    monkeypatch.setattr(acquire_mod, "CASE_PIPELINE_DIR", tmp_path)
    skip, reason = acquire_mod._should_skip_case_download()
    assert skip is False
    assert reason == ""
