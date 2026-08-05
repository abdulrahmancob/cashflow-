"""Land accuracy: Cash Expected Land vs bank actual deposits."""

from __future__ import annotations

from datetime import date
from math import sqrt
from typing import Iterable

import pandas as pd


def _as_date(value: object) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date) and not isinstance(value, pd.Timestamp):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])


def land_date_series(outcomes: pd.DataFrame) -> pd.Series:
    """Scheduled land day: prefer original_forecast_date (pre-pack), else forecast_date."""
    if outcomes is None or outcomes.empty:
        return pd.Series(dtype="datetime64[ns]")
    if "original_forecast_date" in outcomes.columns:
        s = pd.to_datetime(outcomes["original_forecast_date"], errors="coerce")
        if "forecast_date" in outcomes.columns:
            s = s.fillna(pd.to_datetime(outcomes["forecast_date"], errors="coerce"))
        return s
    if "forecast_date" in outcomes.columns:
        return pd.to_datetime(outcomes["forecast_date"], errors="coerce")
    return pd.Series(pd.NaT, index=outcomes.index)


def cash_expected_land_by_day(outcomes: pd.DataFrame) -> pd.Series:
    """Sum on_track + overdue expected_amount by scheduled land date (pre-pack)."""
    if outcomes is None or outcomes.empty:
        return pd.Series(dtype=float)
    df = outcomes.copy()
    df = df[df["outcome_stage"].isin(["on_track", "overdue"])]
    df["expected_amount"] = pd.to_numeric(df.get("expected_amount"), errors="coerce").fillna(0)
    df["_fd"] = land_date_series(df)
    df = df[df["_fd"].notna() & (df["expected_amount"] > 0)]
    g = df.groupby(df["_fd"].dt.strftime("%Y-%m-%d"), as_index=True)["expected_amount"].sum()
    g.name = "pred"
    return g


def actual_by_day(actual_daily: pd.DataFrame) -> pd.Series:
    if actual_daily is None or actual_daily.empty:
        return pd.Series(dtype=float)
    df = actual_daily.copy()
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df["_d"] = pd.to_datetime(df["period"], errors="coerce")
    df = df[df["_d"].notna()]
    g = df.groupby(df["_d"].dt.strftime("%Y-%m-%d"), as_index=True)["amount"].sum()
    g.name = "actual"
    return g


def build_land_accuracy_frame(
    outcomes: pd.DataFrame,
    actual_daily: pd.DataFrame,
    *,
    dates: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Per-day pred vs actual with error columns; plus optional date filter."""
    pred = cash_expected_land_by_day(outcomes)
    act = actual_by_day(actual_daily)
    idx = sorted(set(pred.index) | set(act.index))
    if dates is not None:
        want = {str(d)[:10] for d in dates}
        idx = [d for d in idx if d in want]
    rows = []
    for d in idx:
        p = float(pred.get(d, 0.0) or 0.0)
        a = float(act.get(d, 0.0) or 0.0)
        err = p - a
        abs_pct = abs(err) / a if a > 1e-9 else (None if a == 0 and p == 0 else 1.0)
        acc = (1.0 - abs_pct) if abs_pct is not None else None
        rows.append(
            {
                "date": d,
                "actual": round(a, 2),
                "pred": round(p, 2),
                "error": round(err, 2),
                "abs_pct_error": None if abs_pct is None else round(abs_pct, 6),
                "accuracy": None if acc is None else round(acc, 6),
            }
        )
    return pd.DataFrame(rows)


def summarize_land_accuracy(day_frame: pd.DataFrame) -> dict[str, float | None]:
    """MAPE, Bias, RMSE, mean Accuracy over days with actual > 0."""
    if day_frame is None or day_frame.empty:
        return {"mape": None, "bias": None, "rmse": None, "accuracy": None, "n_days": 0}
    df = day_frame[pd.to_numeric(day_frame["actual"], errors="coerce").fillna(0) > 0].copy()
    if df.empty:
        return {"mape": None, "bias": None, "rmse": None, "accuracy": None, "n_days": 0}
    err = pd.to_numeric(df["error"], errors="coerce").fillna(0)
    ape = pd.to_numeric(df["abs_pct_error"], errors="coerce")
    acc = pd.to_numeric(df["accuracy"], errors="coerce")
    return {
        "mape": round(float(ape.mean()), 6),
        "bias": round(float(err.mean()), 2),
        "rmse": round(float(sqrt(float((err**2).mean()))), 2),
        "accuracy": round(float(acc.mean()), 6),
        "n_days": int(len(df)),
    }
