"""Stable data-access contracts for cashflow consumers.

SQL for product paths lives here (and in loaders/migrations) — not in
cashflow_forecast / cashflow_reconcile call sites.
"""

from __future__ import annotations

from cashflow_db.repository import (
    auth_users,
    claims,
    eligibility,
    features,
    forecast,
    insurance,
    payments,
    reconciliation,
    tracker,
    visits,
)
from cashflow_db.repository.client import connection, transaction

__all__ = [
    "auth_users",
    "claims",
    "connection",
    "eligibility",
    "features",
    "forecast",
    "insurance",
    "payments",
    "reconciliation",
    "tracker",
    "transaction",
    "visits",
]
