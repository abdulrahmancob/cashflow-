"""Pure finance payload assembly: aging, recovery, Pareto, pace, scorecard."""

from __future__ import annotations

from datetime import date

from cashflow_forecast.deposit_capacity import age_factor
from cashflow_forecast.finance_views import (
    build_burnup,
    build_cash_kpis,
    build_collection_rate,
    build_exec_scorecard,
    build_forward_weeks,
    build_overdue_analysis,
    leakage_waterfall,
    payer_grade,
)


def _grid_row(**kwargs):
    base = {
        "ins_name": "Aetna",
        "facility_name": "Bedstuy",
        "dos_month": "2026-06",
        "land_month": "2026-07",
        "age_bucket": "31_60",
        "amount": 100.0,
        "line_count": 2,
        "overdue_days_sum": 80.0,
        "sla_lag_sum": 20.0,
        "sla_lag_n": 2,
    }
    base.update(kwargs)
    return base


def test_recovery_uses_age_curve_upper_bound():
    assert age_factor(60) == 0.60
    payload = build_overdue_analysis([_grid_row(amount=100, age_bucket="31_60")], [], [])
    assert payload["kpi"]["face"] == 100.0
    assert payload["kpi"]["expected_recovery"] == 60.0
    assert payload["kpi"]["recovery_pct"] == 60.0
    assert payload["aging"][0]["b31_60"] == 100.0
    assert payload["aging"][0]["b90_plus"] == 0.0


def test_month_chart_folds_past_top_six_into_other():
    rows = [
        _grid_row(ins_name=f"P{i}", amount=float(10 - i), dos_month="2026-05", land_month="")
        for i in range(8)
    ]
    payload = build_overdue_analysis(rows, [], [])
    dos = [row for row in payload["by_month"] if row["basis"] == "dos"]
    names = {row["ins_name"] for row in dos}
    assert "Other" in names
    assert len(names) == 7
    assert sum(row["amount"] for row in dos) == sum(float(10 - i) for i in range(8))
    assert payload["by_month"] == dos or any(row["basis"] == "land" for row in payload["by_month"]) is False


def test_pareto_cumulative_and_top_shares():
    rows = [
        _grid_row(ins_name="A", amount=50),
        _grid_row(ins_name="B", amount=30),
        _grid_row(ins_name="C", amount=20),
    ]
    payload = build_overdue_analysis(rows, [], [])
    pareto = payload["pareto"]["rows"]
    assert pareto[-1]["cumulative_pct"] == 100.0
    assert payload["pareto"]["top3_share"] == 100.0
    assert payload["kpi"]["top3_share"] == 100.0
    assert abs(pareto[0]["share_pct"] - 50.0) < 0.01


def test_four_week_change_uses_snapshot_on_or_before_cutoff():
    trend = [
        {"as_of": "2026-08-01", "overdue": 100, "on_track": 10, "overdue_90_plus": 1},
        {"as_of": "2026-08-15", "overdue": 110, "on_track": 10, "overdue_90_plus": 1},
        {"as_of": "2026-09-01", "overdue": 150, "on_track": 10, "overdue_90_plus": 1},
        {"as_of": "2026-09-20", "overdue": 180, "on_track": 10, "overdue_90_plus": 1},
    ]
    payload = build_overdue_analysis([], trend, [])
    assert payload["kpi"]["overdue_change_4w"] == 70.0
    assert abs(payload["kpi"]["overdue_change_4w_pct"] - 63.6) < 0.05
    assert payload["kpi"]["overdue_change_4w"] is not None


def test_four_week_change_missing_when_history_is_short():
    payload = build_overdue_analysis(
        [],
        [{"as_of": "2026-09-20", "overdue": 10, "on_track": 1, "overdue_90_plus": 0}],
        [],
    )
    assert payload["kpi"]["overdue_change_4w"] is None


def test_chase_list_ranks_by_recovery_not_age():
    claims = [
        {"ins_name": "Old", "expected_amount": 10, "overdue_days": 200, "emr_patient_id": "1"},
        {"ins_name": "Fresh", "expected_amount": 100, "overdue_days": 10, "emr_patient_id": "2"},
    ]
    payload = build_overdue_analysis([], [], claims)
    assert payload["chase_list"][0]["ins_name"] == "Fresh"
    assert payload["chase_list"][0]["recovery_amount"] == 100.0
    assert payload["chase_list"][1]["recovery_amount"] == 0.5


def test_burnup_splits_settled_and_rest_of_month():
    daily = [
        {"period": "2026-10-01", "amount": 10},
        {"period": "2026-10-02", "amount": 30},
    ]
    actual = [{"period": "2026-10-01", "amount": 8}]
    rows = build_burnup(
        daily,
        actual,
        start=date(2026, 10, 1),
        end=date(2026, 10, 2),
        settled=date(2026, 10, 1),
    )
    assert rows[0]["actual_cum"] == 8.0
    assert rows[0]["forecast_to_date"] == 10.0
    assert rows[0]["forecast_rest"] == 10.0
    assert rows[1]["actual_cum"] is None
    assert rows[1]["forecast_rest"] == 40.0
    assert rows[1]["forecast_to_date"] is None


def test_cash_kpis_pace_and_forward_fallback():
    kpi = build_cash_kpis(
        month_daily=[{"period": "2026-10-01", "amount": 100}],
        month_actual=[{"period": "2026-10-01", "amount": 80}],
        forward_daily=[],
        settled=date(2026, 10, 1),
        today=date(2026, 10, 5),
        accuracy_rows=[{"error_pct": 10}, {"error_pct": -20}],
        forward_weeks=[{"week": "2026-10-06", "on_track": 40, "overdue_recovery": 10}],
    )
    assert kpi["cash_mtd"] == 80.0
    assert kpi["forecast_mtd"] == 100.0
    assert kpi["pace_pct"] == 80.0
    assert kpi["next_30d"] == 50.0
    assert kpi["mae_30d"] == 15.0


def test_forward_weeks_weight_overdue_only():
    weeks = build_forward_weeks(
        [
            {"week": date(2026, 10, 5), "outcome_stage": "on_track", "age_bucket": "0_14", "amount": 20},
            {"week": "2026-10-05", "outcome_stage": "overdue", "age_bucket": "61_90", "amount": 100},
        ]
    )
    assert weeks[0]["on_track"] == 20.0
    assert weeks[0]["overdue_recovery"] == 35.0


def test_collection_rate_marks_recent_months_maturing():
    rows = build_collection_rate(
        [
            {"period": "2026-06", "outcome_stage": "paid", "amount": 80},
            {"period": "2026-06", "outcome_stage": "overdue", "amount": 20},
            {"period": "2026-10", "outcome_stage": "paid", "amount": 10},
            {"period": "2026-10", "outcome_stage": "on_track", "amount": 90},
        ],
        today=date(2026, 10, 5),
    )
    by_period = {row["period"]: row for row in rows}
    assert by_period["2026-06"]["rate_pct"] == 80.0
    assert by_period["2026-06"]["maturing"] is False
    assert by_period["2026-10"]["maturing"] is True


def test_waterfall_and_grade():
    steps = leakage_waterfall(
        [
            {"outcome_stage": "paid", "amount": 70},
            {"outcome_stage": "denied", "amount": 20},
            {"outcome_stage": "rejected", "amount": 5},
            {"outcome_stage": "zero_pay", "amount": 5},
        ],
        doc_risk=10,
    )
    by_label = {step["label"]: step["amount"] for step in steps}
    assert by_label["Expected"] == 100.0
    assert by_label["Collectible"] == 60.0
    assert payer_grade(92, 20) == "A"
    assert payer_grade(50, 20) == "D"
    assert payer_grade(None, None) == ""


def test_endpoints_degrade_without_a_database(monkeypatch):
    from cashflow_forecast import api as forecast_api

    monkeypatch.setattr(forecast_api, "_use_db", lambda: False)
    overdue = forecast_api._overdue_analysis_payload(fac=[], insurers=[], d0=None, d1=None)
    assert overdue["by_month"] == []
    assert overdue["kpi"]["face"] == 0.0
    cash = forecast_api.cash_overview()
    assert cash["daily"] == []
    assert "pace_pct" in cash["kpi"]
    score = forecast_api.exec_scorecard()
    assert len(score["tiles"]) == 6
    assert score["leakage"]


def test_exec_scorecard_tiles_and_narrative():
    payload = build_exec_scorecard(
        today=date(2026, 10, 5),
        stages=[{"outcome_stage": "paid", "amount": 50}, {"outcome_stage": "denied", "amount": 10}],
        doc_risk=5,
        collection_rows=[
            {"period": "2026-06", "outcome_stage": "paid", "amount": 80},
            {"period": "2026-06", "outcome_stage": "overdue", "amount": 20},
            {"period": "2026-07", "outcome_stage": "paid", "amount": 50},
            {"period": "2026-07", "outcome_stage": "overdue", "amount": 50},
            {"period": "2026-09", "outcome_stage": "denied", "amount": 30},
            {"period": "2026-08", "outcome_stage": "denied", "amount": 10},
        ],
        grid=[_grid_row(ins_name="Aetna", amount=100, age_bucket="91_180", overdue_days_sum=40, line_count=2)],
        trend=[
            {"as_of": "2026-08-01", "overdue": 80, "on_track": 20, "overdue_90_plus": 10},
            {"as_of": "2026-10-01", "overdue": 100, "on_track": 20, "overdue_90_plus": 40},
        ],
        stage_by_ins=[
            {"ins_name": "Aetna", "outcome_stage": "paid", "amount": 90},
            {"ins_name": "Aetna", "outcome_stage": "overdue", "amount": 10},
        ],
        tracker_by_ins=[{"ins_name": "Aetna", "amount": 900}],
        open_ar={"on_track_amount": 20, "overdue_amount": 80},
        cash_last_month=200,
        cash_prior_month=100,
        cash_90=900,
        cash_prior_90=900,
        accuracy_rows=[{"error_pct": 4}, {"error_pct": 6}],
    )
    tiles = {tile["key"]: tile for tile in payload["tiles"]}
    assert tiles["cash"]["delta_pct"] == 100.0
    assert tiles["collection"]["value"] == 50.0
    assert tiles["dso"]["value"] == 10.0
    assert tiles["overdue_pct"]["value"] == 80.0
    assert tiles["leakage"]["value"] == 30.0
    assert payload["narrative"]["worst_payer"] == "Aetna"
    assert payload["narrative"]["top3_share"] == 100.0
    assert payload["payer_scorecard"][0]["grade"] == "A"
    assert len(payload["tiles"]) == 6
