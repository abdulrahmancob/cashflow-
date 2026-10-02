"""Billing cash-flow analysis helpers (no live DB)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cashflow_db.repository.billing_analysis import (
    AGING_SQL,
    CASH_SHARE_SQL,
    CLINICS_SQL,
    CYCLE_SQL,
    MONTHLY_SQL,
    apply_visit_rates,
    attach_cash_share,
    attach_cycle,
    cash_share_days,
    classify_billing_status,
    cycle_metrics,
    fill_year_rows,
)

_COLLECT_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "072_billing_collect_visit.sql"
).read_text(encoding="utf-8")


def test_classify_billing_status():
    assert classify_billing_status("Paid") == "paid"
    assert classify_billing_status("partial") == "paid"
    assert classify_billing_status("Denied") == "denied"
    assert classify_billing_status("Not Paid") == "denied"
    assert classify_billing_status("pending") == "pending"
    assert classify_billing_status(None) == "pending"
    assert classify_billing_status("  ") == "pending"


def test_sql_is_snowflake_not_ss():
    blob = MONTHLY_SQL + _COLLECT_SQL + AGING_SQL + CYCLE_SQL + CASH_SHARE_SQL + CLINICS_SQL
    assert "analytics.snowflake_visit_kpi" in blob
    assert "pr_queue_flag" not in blob
    assert "second_submission" not in blob
    assert "timely_filing" not in blob
    assert "reconciliation_visit_agg" not in blob
    assert "client_payment" in MONTHLY_SQL
    assert "primary_check_date" in _COLLECT_SQL


def test_collect_date_source_order():
    order = [
        "sf_tracker.txn_date",
        "ws_tracker.txn_date",
        "el_tracker.txn_date",
        "waystar.trans_date",
        "elig.check_date",
        "v.primary_check_date",
    ]
    positions = [_COLLECT_SQL.index(token) for token in order]
    assert positions == sorted(positions)
    assert "billing.transaction_tracker_row" in _COLLECT_SQL
    assert "billing.waystar_claim" in _COLLECT_SQL
    assert "billing.waystar_webpt_map" in _COLLECT_SQL
    assert "ops.eligibility_work_item" in _COLLECT_SQL
    assert "manual_overrides" in _COLLECT_SQL
    assert "insurance_payment" in _COLLECT_SQL
    assert "client_payment" in _COLLECT_SQL
    assert "btrim(m.webpt_patient_id)" not in _COLLECT_SQL
    assert "btrim(wi.emr_patient_id)" not in _COLLECT_SQL
    assert "m.dos" not in _COLLECT_SQL
    assert "MATERIALIZED VIEW" in _COLLECT_SQL
    assert "analytics.billing_collect_visit" in _COLLECT_SQL
    assert _COLLECT_SQL.count("regexp_replace") == 8
    waystar = _COLLECT_SQL.split("waystar_base AS", 1)[1].split("elig_base AS", 1)[0]
    elig = _COLLECT_SQL.split("elig_base AS", 1)[1].split("sf_tracker AS", 1)[0]
    assert "FROM billing.waystar_claim" in waystar
    assert "FROM visits" not in waystar
    assert "FROM ops.eligibility_work_item" in elig
    assert "FROM visits" not in elig


def test_page_reads_stored_view():
    assert "analytics.billing_collect_visit" in AGING_SQL
    assert "analytics.billing_collect_visit" in CYCLE_SQL
    assert "analytics.billing_collect_visit" in CASH_SHARE_SQL
    assert "CREATE TEMP TABLE" not in AGING_SQL
    assert "CREATE TEMP TABLE" not in CYCLE_SQL
    assert "WITH visits" not in AGING_SQL
    assert "WITH visits" not in CYCLE_SQL


def test_cycle_sql_keeps_later_year_collections():
    after_from = CYCLE_SQL.lower().split("from analytics.billing_collect_visit", 1)[1]
    window = after_from.split("where", 1)[1].split("group", 1)[0]
    assert "collect_date" not in window
    assert "date_of_service" in window
    assert "GROUPING SETS" in CYCLE_SQL
    assert "date_trunc('year'" not in CYCLE_SQL
    assert "EXTRACT(YEAR" not in CYCLE_SQL.upper()
    assert "aged.collect_date >= aged.date_of_service" in CYCLE_SQL


def test_cash_share_sql_keeps_later_year_collections():
    assert "date_trunc('year'" not in CASH_SHARE_SQL
    assert "EXTRACT(YEAR" not in CASH_SHARE_SQL.upper()
    assert "aged.collect_date >= aged.date_of_service" in CASH_SHARE_SQL
    assert "aged.amount > 0" in CASH_SHARE_SQL
    after_from = CASH_SHARE_SQL.lower().split("from analytics.billing_collect_visit", 1)[1]
    window = after_from.split("where", 1)[1]
    assert "extract(year" not in window


def test_cash_share_days_walk():
    got = cash_share_days([(5, 200), (70, 100), (20, 300), (50, 100), (35, 300)])
    assert got["cash_50_days"] == 20
    assert got["cash_80_days"] == 35
    assert got["cash_90_days"] == 50
    assert got["cash_95_days"] == 70
    blank = cash_share_days([])
    assert blank["cash_50_days"] is None
    assert blank["cash_95_days"] is None


def test_attach_cash_share_pools_year_dollars():
    rows, totals = fill_year_rows(2026, {}, {})
    attach_cash_share(
        rows,
        totals,
        [
            {"visit_month": date(2026, 1, 1), "lag_days": 10, "amount": 1000},
            {"visit_month": date(2026, 12, 1), "lag_days": 100, "amount": 1000},
        ],
    )
    assert rows[0]["cash_50_days"] == 10
    assert rows[0]["cash_95_days"] == 10
    assert rows[1]["cash_50_days"] is None
    assert rows[11]["cash_50_days"] == 100
    assert rows[11]["cash_95_days"] == 100
    assert totals["cash_50_days"] == 10
    assert totals["cash_80_days"] == 100
    assert totals["cash_90_days"] == 100
    assert totals["cash_95_days"] == 100


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
