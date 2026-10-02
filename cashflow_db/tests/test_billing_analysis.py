"""Billing cash-flow analysis helpers (no live DB)."""

from __future__ import annotations

from datetime import date

from cashflow_db.repository.billing_analysis import (
    AGING_SQL,
    CLINICS_SQL,
    MONTHLY_SQL,
    apply_visit_rates,
    classify_billing_status,
    fill_year_rows,
)


def test_classify_billing_status():
    assert classify_billing_status("Paid") == "paid"
    assert classify_billing_status("partial") == "paid"
    assert classify_billing_status("Denied") == "denied"
    assert classify_billing_status("Not Paid") == "denied"
    assert classify_billing_status("pending") == "pending"
    assert classify_billing_status(None) == "pending"
    assert classify_billing_status("  ") == "pending"


def test_sql_is_snowflake_not_ss():
    blob = MONTHLY_SQL + AGING_SQL + CLINICS_SQL
    assert "analytics.snowflake_visit_kpi" in blob
    assert "pr_queue_flag" not in blob
    assert "second_submission" not in blob
    assert "timely_filing" not in blob
    assert "reconciliation_visit_agg" not in blob
    assert "client_payment" in MONTHLY_SQL
    assert "primary_check_date" in AGING_SQL


def test_collect_date_source_order():
    order = [
        "sf_tracker.txn_date",
        "ws_tracker.txn_date",
        "el_tracker.txn_date",
        "waystar.trans_date",
        "elig.check_date",
        "v.primary_check_date",
    ]
    positions = [AGING_SQL.index(token) for token in order]
    assert positions == sorted(positions)
    assert "billing.transaction_tracker_row" in AGING_SQL
    assert "billing.waystar_claim" in AGING_SQL
    assert "billing.waystar_webpt_map" in AGING_SQL
    assert "ops.eligibility_work_item" in AGING_SQL
    assert "manual_overrides" in AGING_SQL
    assert "insurance_payment" in AGING_SQL
    assert "client_payment" in AGING_SQL


def test_fill_year_rows_rates_and_aging():
    monthly = {
        date(2026, 1, 1): {
            "insurance_payment": 100,
            "copay": 20,
            "visits": 10,
            "paid_visits": 8,
            "denied_visits": 1,
        }
    }
    aging = {(date(2026, 1, 1), 2): 50.0, (date(2026, 1, 1), 3): 30.0}
    rows, totals = fill_year_rows(2026, monthly, aging)
    assert len(rows) == 12
    jan = rows[0]
    assert jan["period"] == "Jan 2026"
    assert jan["total_payment"] == 120
    assert jan["pending_visits"] == 1
    assert jan["paid_visits"] + jan["pending_visits"] + jan["denied_visits"] == jan["visits"]
    assert jan["payment_pct"] == 80.0
    assert jan["ave_visit"] == 12.0
    assert jan["act_ave_visit"] == 15.0
    assert jan["collection"]["02"] == 50
    assert jan["collection"]["03"] == 30
    assert jan["collection"]["01"] == 0
    assert totals["period"] == "Total"
    assert totals["visits"] == 10
    assert totals["total_payment"] == 120
    assert totals["collection"]["02"] == 50
    assert rows[8]["visits"] == 0


def test_pending_is_visits_minus_paid_denied():
    row = apply_visit_rates(
        {
            "insurance_payment": 0,
            "copay": 0,
            "visits": 5,
            "paid_visits": 2,
            "denied_visits": 1,
        }
    )
    assert row["pending_visits"] == 2
    assert row["payment_pct"] == 40.0
