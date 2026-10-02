"""Billing cash-flow analysis helpers (no live DB)."""

from __future__ import annotations

from datetime import date

from cashflow_db.repository.billing_analysis import (
    AGING_SQL,
    CLINICS_SQL,
    CYCLE_SQL,
    MONTHLY_SQL,
    apply_visit_rates,
    attach_cycle,
    classify_billing_status,
    cycle_metrics,
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
    blob = MONTHLY_SQL + AGING_SQL + CYCLE_SQL + CLINICS_SQL
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
    cycle_positions = [CYCLE_SQL.index(token) for token in order]
    assert cycle_positions == sorted(cycle_positions)


def test_cycle_sql_keeps_later_year_collections():
    suffix = CYCLE_SQL.rsplit("FROM aged", 1)[1]
    assert "where" not in suffix.lower()
    assert "GROUPING SETS" in suffix
    assert "date_trunc('year'" not in CYCLE_SQL
    assert "EXTRACT(YEAR" not in CYCLE_SQL.upper()
    assert "aged.collect_date >= aged.date_of_service" in CYCLE_SQL


def test_cycle_year_rollup_uses_sums_and_supplied_median():
    year = cycle_metrics(
        collected_visits=3,
        open_visits=1,
        median_days=30,
        sum_days=80,
        sum_amount=500,
        sum_amount_days=16000,
    )
    assert year["avg_days"] == 26.7
    assert year["weighted_avg_days"] == 32.0
    assert year["median_days"] == 30.0
    blank = cycle_metrics(
        collected_visits=0,
        open_visits=4,
        median_days=12,
        sum_days=0,
        sum_amount=0,
        sum_amount_days=0,
    )
    assert blank["open_visits"] == 4
    assert blank["median_days"] is None
    assert blank["avg_days"] is None
    assert blank["weighted_avg_days"] is None


def test_attach_cycle_blank_months_and_year_median():
    rows, totals = fill_year_rows(2026, {}, {})
    attach_cycle(
        rows,
        totals,
        [
            {
                "visit_month": date(2026, 1, 1),
                "collected_visits": 2,
                "open_visits": 1,
                "median_days": 10,
                "sum_days": 40,
                "sum_amount": 200,
                "sum_amount_days": 4000,
            },
            {
                "visit_month": date(2026, 12, 1),
                "collected_visits": 1,
                "open_visits": 0,
                "median_days": 40,
                "sum_days": 40,
                "sum_amount": 300,
                "sum_amount_days": 12000,
            },
            {
                "visit_month": None,
                "collected_visits": 3,
                "open_visits": 1,
                "median_days": 30,
                "sum_days": 80,
                "sum_amount": 500,
                "sum_amount_days": 16000,
            },
        ],
    )
    assert rows[0]["median_days"] == 10.0
    assert rows[0]["avg_days"] == 20.0
    assert rows[0]["weighted_avg_days"] == 20.0
    assert rows[0]["open_visits"] == 1
    assert rows[1]["collected_visits"] == 0
    assert rows[1]["median_days"] is None
    assert rows[1]["avg_days"] is None
    assert rows[11]["median_days"] == 40.0
    assert rows[11]["weighted_avg_days"] == 40.0
    assert totals["collected_visits"] == 3
    assert totals["open_visits"] == 1
    assert totals["median_days"] == 30.0
    assert totals["avg_days"] == 26.7
    assert totals["weighted_avg_days"] == 32.0


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
