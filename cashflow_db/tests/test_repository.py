"""Smoke tests for repository contracts (no live DB required)."""

from __future__ import annotations


def test_repository_exports():
    from cashflow_db import repository
    from cashflow_db.repository import (
        claims,
        connection,
        forecast,
        insurance,
        payments,
        reconciliation,
        visits,
    )

    assert repository.visits is visits
    assert callable(connection)
    assert callable(visits.get_clinical_visits)
    assert callable(payments.get_eob_payments_unified)
    assert callable(reconciliation.create_reconciliation_run)
    assert callable(forecast.create_forecast_run)
    assert callable(insurance.get_payor_behavior_summary)
    assert callable(claims.get_denial_records)


def test_migrations_include_spine():
    from cashflow_db.db import MIGRATIONS

    assert "013_operational_spine.sql" in MIGRATIONS


def test_validate_module_importable():
    from cashflow_db import validate_warehouse

    assert callable(validate_warehouse.run_assertions)
