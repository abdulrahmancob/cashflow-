"""Package tests (no Postgres required for util)."""

from __future__ import annotations

from pathlib import Path

from cashflow_db.loaders.base import classify_auth_kind, load_business_rules
from cashflow_db.util import normalize_name_key, parse_date, parse_money, split_multi


def test_normalize_name_key_last_first():
    assert normalize_name_key("Doe, Jane Marie") == "DOEJANE"


def test_parse_money_parens():
    assert parse_money("($12.50)") == __import__("decimal").Decimal("-12.50")
    assert parse_money("$1,234.00") == __import__("decimal").Decimal("1234.00")


def test_parse_date():
    from datetime import date

    assert parse_date("2026-07-01") == date(2026, 7, 1)
    assert parse_date("07/01/2026") == date(2026, 7, 1)


def test_split_multi():
    assert split_multi("97110; 97112 ; ") == ["97110", "97112"]


def test_business_rules_yaml_loads():
    rules = load_business_rules()
    assert rules["auth"]["end_date_overrides_remaining"] is True
    assert "waystar" in rules["submission_routes"]
    assert rules["denial_ownership"]["front_desk"]


def test_dummy_auth_classification():
    assert classify_auth_kind("0") == "dummy"
    assert classify_auth_kind("12345") == "hard"


def test_sql_migrations_present():
    sql_dir = Path(__file__).resolve().parents[1] / "sql"
    names = sorted(p.name for p in sql_dir.glob("*.sql"))
    assert names[0] == "001_schemas.sql"
    assert "005_billing.sql" in names
    assert "011_seed_ref.sql" in names
    assert "012_case_centric.sql" in names


def test_erd_doc_exists():
    erd = Path(__file__).resolve().parents[2] / "docs" / "erd.md"
    assert erd.exists()
    text = erd.read_text(encoding="utf-8")
    assert "erDiagram" in text
    assert "CLAIM" in text
    assert "VISIT" in text
