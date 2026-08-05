"""Tests for land accuracy metrics."""

from __future__ import annotations

from datetime import date

import pandas as pd

from cashflow_forecast.land_accuracy import (
    build_land_accuracy_frame,
    summarize_land_accuracy,
)


def test_land_accuracy_mape_bias_rmse():
    outcomes = pd.DataFrame(
        [
            {
                "forecast_date": date(2026, 7, 24),
                "outcome_stage": "on_track",
                "expected_amount": 100.0,
            },
            {
                "forecast_date": date(2026, 7, 24),
                "outcome_stage": "overdue",
                "expected_amount": 50.0,
            },
            {
                "forecast_date": date(2026, 7, 24),
                "outcome_stage": "denied",
                "expected_amount": 999.0,
            },
            {
                "forecast_date": date(2026, 7, 27),
                "outcome_stage": "on_track",
                "expected_amount": 80.0,
            },
        ]
    )
    actual = pd.DataFrame(
        [
            {"period": date(2026, 7, 24), "amount": 150.0},
            {"period": date(2026, 7, 27), "amount": 100.0},
        ]
    )
    frame = build_land_accuracy_frame(
        outcomes, actual, dates=["2026-07-24", "2026-07-27"]
    )
    assert len(frame) == 2
    row24 = frame[frame["date"] == "2026-07-24"].iloc[0]
    assert abs(row24["pred"] - 150.0) < 1e-6  # denied excluded
    assert abs(row24["actual"] - 150.0) < 1e-6
    assert abs(row24["accuracy"] - 1.0) < 1e-6
    row27 = frame[frame["date"] == "2026-07-27"].iloc[0]
    assert abs(row27["pred"] - 80.0) < 1e-6
    assert abs(row27["error"] - (-20.0)) < 1e-6
    summary = summarize_land_accuracy(frame)
    assert summary["n_days"] == 2
    assert summary["bias"] == -10.0  # mean(0 + -20)
    assert abs(summary["mape"] - 0.1) < 1e-6  # (0 + 0.2) / 2
    assert summary["accuracy"] is not None and summary["accuracy"] > 0.8


def test_land_accuracy_prefers_original_forecast_date():
    """Packed forecast_date moves forward; pred land stays on original day."""
    outcomes = pd.DataFrame(
        [
            {
                "original_forecast_date": date(2026, 7, 28),
                "forecast_date": date(2026, 7, 30),
                "outcome_stage": "overdue",
                "expected_amount": 200.0,
            },
            {
                "original_forecast_date": date(2026, 7, 29),
                "forecast_date": date(2026, 7, 30),
                "outcome_stage": "on_track",
                "expected_amount": 75.0,
            },
        ]
    )
    actual = pd.DataFrame(
        [
            {"period": date(2026, 7, 28), "amount": 0.0},
            {"period": date(2026, 7, 29), "amount": 0.0},
        ]
    )
    frame = build_land_accuracy_frame(
        outcomes, actual, dates=["2026-07-28", "2026-07-29", "2026-07-30"]
    )
    by_date = {r["date"]: r for r in frame.to_dict("records")}
    assert abs(by_date["2026-07-28"]["pred"] - 200.0) < 1e-6
    assert abs(by_date["2026-07-29"]["pred"] - 75.0) < 1e-6
    # Packed day should not receive pred when original is set
    assert "2026-07-30" not in by_date or abs(by_date.get("2026-07-30", {}).get("pred", 0)) < 1e-9
