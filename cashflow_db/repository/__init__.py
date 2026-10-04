"""Stable data-access contracts for cashflow consumers.

SQL for product paths lives here (and in loaders/migrations) — not in
cashflow_forecast / cashflow_reconcile call sites.
"""

from __future__ import annotations

from cashflow_db.repository import (
    admin_db,
    auth_users,
    checks_deposits,
    claims,
    collection,
    completeness,
    cpt_audit,
    cpt_guide,
    eligibility,
    features,
    forecast,
    identity_backfill,
    insurance,
    payments,
    portal_activity,
    pr_tfl,
    reconciliation,
    tracker,
    visits,
    work_analytics,
    billing_analysis,
    user_away,
)
from cashflow_db.repository.client import connection, transaction

__all__ = [
    "admin_db",
    "auth_users",
    "billing_analysis",
    "checks_deposits",
    "claims",
    "collection",
    "completeness",
    "connection",
    "cpt_audit",
    "cpt_guide",
    "eligibility",
    "features",
    "forecast",
    "identity_backfill",
    "insurance",
    "payments",
    "portal_activity",
    "pr_tfl",
    "reconciliation",
    "tracker",
    "transaction",
    "user_away",
    "visits",
    "work_analytics",
]
