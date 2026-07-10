"""Actual and projected cash buckets by day / week / month."""

from __future__ import annotations

from datetime import date

import pandas as pd


def actual_cash_buckets(payments: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Group paid_amount by eob_date (Check Date)."""
    paid = payments[(payments["paid_amount"] > 0) & payments["eob_date"].notna()].copy()
    if paid.empty:
        empty = pd.DataFrame(columns=["period", "amount", "line_count"])
        return {"daily": empty, "weekly": empty, "monthly": empty}

    paid["period"] = paid["eob_date"]
    daily = (
        paid.groupby("period", as_index=False)
        .agg(amount=("paid_amount", "sum"), line_count=("paid_amount", "count"))
        .sort_values("period")
    )
    daily["amount"] = daily["amount"].round(2)

    paid["week"] = paid["eob_date"].map(
        lambda d: f"{d.isocalendar().year}-W{d.isocalendar().week:02d}" if d else ""
    )
    weekly = (
        paid.groupby("week", as_index=False)
        .agg(amount=("paid_amount", "sum"), line_count=("paid_amount", "count"))
        .rename(columns={"week": "period"})
        .sort_values("period")
    )
    weekly["amount"] = weekly["amount"].round(2)

    paid["month"] = paid["eob_date"].map(lambda d: d.strftime("%Y-%m") if d else "")
    monthly = (
        paid.groupby("month", as_index=False)
        .agg(amount=("paid_amount", "sum"), line_count=("paid_amount", "count"))
        .rename(columns={"month": "period"})
        .sort_values("period")
    )
    monthly["amount"] = monthly["amount"].round(2)

    return {"daily": daily, "weekly": weekly, "monthly": monthly}


def projected_cash_buckets(outcomes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Project expected cash by forecast_date for non-paid outcomes."""
    proj = outcomes[
        outcomes["outcome_stage"].isin(["on_track", "overdue", "rejected", "denied"])
        & outcomes["forecast_date"].notna()
        & (outcomes["expected_amount"] > 0)
    ].copy()
    # Denied without resubmission shift still shows as denied exposure on forecast_date
    if proj.empty:
        empty = pd.DataFrame(columns=["period", "amount", "line_count", "outcome_stage"])
        return {"daily": empty, "weekly": empty, "monthly": empty}

    # For denied without shift, amount is denied exposure (may recover later)
    # Use expected_amount already set in outcome_stages

    proj["period"] = proj["forecast_date"]
    daily = (
        proj.groupby("period", as_index=False)
        .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
        .sort_values("period")
    )
    daily["amount"] = daily["amount"].round(2)

    proj["week"] = proj["forecast_date"].map(
        lambda d: f"{d.isocalendar().year}-W{d.isocalendar().week:02d}" if d else ""
    )
    weekly = (
        proj.groupby("week", as_index=False)
        .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
        .rename(columns={"week": "period"})
        .sort_values("period")
    )
    weekly["amount"] = weekly["amount"].round(2)

    proj["month"] = proj["forecast_date"].map(lambda d: d.strftime("%Y-%m") if d else "")
    monthly = (
        proj.groupby("month", as_index=False)
        .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
        .rename(columns={"month": "period"})
        .sort_values("period")
    )
    monthly["amount"] = monthly["amount"].round(2)

    return {"daily": daily, "weekly": weekly, "monthly": monthly}


def kpi_summary(
    outcomes: pd.DataFrame,
    payments: pd.DataFrame,
    risk_flags: pd.DataFrame,
    *,
    as_of: date,
) -> dict:
    actual = float(payments.loc[payments["paid_amount"] > 0, "paid_amount"].sum()) if not payments.empty else 0.0

    def _sum_stage(stage: str) -> tuple[float, int]:
        mask = outcomes["outcome_stage"] == stage
        return float(outcomes.loc[mask, "expected_amount"].sum()), int(mask.sum())

    on_track_amt, on_track_n = _sum_stage("on_track")
    overdue_amt, overdue_n = _sum_stage("overdue")
    rejected_amt, rejected_n = _sum_stage("rejected")
    denied_amt, denied_n = _sum_stage("denied")
    paid_n = int((outcomes["outcome_stage"] == "paid").sum())
    zero_n = int((outcomes["outcome_stage"] == "zero_pay").sum())

    projected = on_track_amt + overdue_amt
    # Resubmission-shifted rejected/denied contribute to future projected via forecast_date
    shifted = outcomes[
        (outcomes["forecast_shift_days"] > 0) & (outcomes["expected_amount"] > 0)
    ]
    projected_with_shift = projected + float(shifted["expected_amount"].sum())

    risk_exposure = float(risk_flags["exposure_amount"].sum()) if not risk_flags.empty else 0.0
    risk_n = int(risk_flags["webpt_patient_id"].nunique()) if not risk_flags.empty else 0

    return {
        "as_of": as_of.isoformat(),
        "actual_cash_received": round(actual, 2),
        "projected_cash_in": round(projected_with_shift, 2),
        "on_track_amount": round(on_track_amt, 2),
        "on_track_count": on_track_n,
        "overdue_amount": round(overdue_amt, 2),
        "overdue_count": overdue_n,
        "denied_amount": round(denied_amt + rejected_amt, 2),
        "denied_count": denied_n + rejected_n,
        "rejected_amount": round(rejected_amt, 2),
        "rejected_count": rejected_n,
        "paid_count": paid_n,
        "zero_pay_count": zero_n,
        "total_lines": int(len(outcomes)),
        "risk_exposure_amount": round(risk_exposure, 2),
        "risk_visit_count": risk_n,
    }
