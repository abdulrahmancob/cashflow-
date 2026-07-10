"""Insurance-level rollups for dashboard tables."""

from __future__ import annotations

import pandas as pd


def overdue_by_insurance(outcomes: pd.DataFrame) -> pd.DataFrame:
    overdue = outcomes[outcomes["outcome_stage"] == "overdue"].copy()
    if overdue.empty:
        return pd.DataFrame(
            columns=["ins_name", "expected_payment", "avg_overdue_days", "line_count"]
        )
    g = (
        overdue.groupby("ins_name", as_index=False)
        .agg(
            expected_payment=("expected_amount", "sum"),
            avg_overdue_days=("overdue_days", "mean"),
            line_count=("expected_amount", "count"),
        )
        .sort_values("expected_payment", ascending=False)
    )
    g["expected_payment"] = g["expected_payment"].round(2)
    g["avg_overdue_days"] = g["avg_overdue_days"].round(1)
    return g


def denied_by_insurance(outcomes: pd.DataFrame) -> pd.DataFrame:
    denied = outcomes[outcomes["outcome_stage"].isin(["denied", "rejected"])].copy()
    if denied.empty:
        return pd.DataFrame(columns=["ins_name", "denied_amount", "line_count", "outcome_stage"])
    g = (
        denied.groupby(["ins_name", "outcome_stage"], as_index=False)
        .agg(
            denied_amount=("expected_amount", "sum"),
            line_count=("expected_amount", "count"),
        )
        .sort_values("denied_amount", ascending=False)
    )
    g["denied_amount"] = g["denied_amount"].round(2)
    return g


def risk_by_insurance(risk_flags: pd.DataFrame) -> pd.DataFrame:
    if risk_flags is None or risk_flags.empty:
        return pd.DataFrame(columns=["ins_name", "risk_flag", "exposure_amount", "visit_count"])
    g = (
        risk_flags.groupby(["ins_name", "risk_flag"], as_index=False)
        .agg(
            exposure_amount=("exposure_amount", "sum"),
            visit_count=("webpt_patient_id", "nunique"),
        )
        .sort_values("exposure_amount", ascending=False)
    )
    g["exposure_amount"] = g["exposure_amount"].round(2)
    return g


def outcome_stage_counts(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame(columns=["outcome_stage", "line_count", "amount"])
    g = (
        outcomes.groupby("outcome_stage", as_index=False)
        .agg(line_count=("outcome_stage", "count"), amount=("expected_amount", "sum"))
        .sort_values("line_count", ascending=False)
    )
    g["amount"] = g["amount"].round(2)
    return g
