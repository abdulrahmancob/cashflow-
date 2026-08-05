"""Actual and projected cash buckets by day / week / month (+ insurance / facility)."""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from cashflow_forecast.utils import parse_date

# Deliverable window for Jan–Aug views
FORECAST_WINDOW_START = date(2026, 1, 1)
FORECAST_WINDOW_END = date(2026, 8, 31)


def _week_label(d: date | None) -> str:
    if not d:
        return ""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _month_label(d: date | None) -> str:
    return d.strftime("%Y-%m") if d else ""


def filter_period_to_window(
    df: pd.DataFrame,
    *,
    start: date = FORECAST_WINDOW_START,
    end: date = FORECAST_WINDOW_END,
    period_col: str = "period",
) -> pd.DataFrame:
    """Keep rows whose period falls in [start, end] (date, YYYY-MM, or ISO week)."""
    if df is None or df.empty or period_col not in df.columns:
        return df

    start_month = start.strftime("%Y-%m")
    end_month = end.strftime("%Y-%m")
    start_week = _week_label(start)
    end_week = _week_label(end)

    def _keep(p) -> bool:
        if p is None or (isinstance(p, float) and pd.isna(p)) or p == "":
            return False
        if isinstance(p, date):
            return start <= p <= end
        text = str(p)
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return start_month <= text <= end_month
        if re.fullmatch(r"\d{4}-W\d{2}", text):
            return start_week <= text <= end_week
        d = parse_date(text)
        return bool(d and start <= d <= end)

    return df[df[period_col].map(_keep)].reset_index(drop=True)


def _agg_cash(
    df: pd.DataFrame,
    date_col: str,
    amount_col: str,
    group_cols: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Build daily/weekly/monthly aggregates; optional extra group columns."""
    base_cols = list(group_cols or [])
    empty_cols = ["period", *base_cols, "amount", "line_count"]
    empty = pd.DataFrame(columns=empty_cols)
    if df.empty:
        return {"daily": empty, "weekly": empty, "monthly": empty}

    work = df.copy()
    work["_day"] = work[date_col]
    work["_week"] = work[date_col].map(_week_label)
    work["_month"] = work[date_col].map(_month_label)

    def _group(period_key: str, period_name: str = "period") -> pd.DataFrame:
        keys = [period_key, *base_cols]
        g = (
            work.groupby(keys, as_index=False)
            .agg(amount=(amount_col, "sum"), line_count=(amount_col, "count"))
            .rename(columns={period_key: period_name})
            .sort_values([period_name, *base_cols] if base_cols else [period_name])
        )
        g["amount"] = g["amount"].round(2)
        return g

    return {
        "daily": _group("_day"),
        "weekly": _group("_week"),
        "monthly": _group("_month"),
    }


def actual_cash_buckets(payments: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Group paid_amount by eob_date (Check Date). Legacy RevFlow path."""
    paid = payments[(payments["paid_amount"] > 0) & payments["eob_date"].notna()].copy()
    return _agg_cash(paid, "eob_date", "paid_amount")


def actual_cash_buckets_from_deposits(deposits: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Group bank Amount by Transaction Tracker deposit_date."""
    if deposits is None or deposits.empty:
        return _agg_cash(pd.DataFrame(), "deposit_date", "amount")
    work = deposits.copy()
    if "deposit_date" not in work.columns or "amount" not in work.columns:
        return _agg_cash(pd.DataFrame(), "deposit_date", "amount")
    work["amount"] = pd.to_numeric(work["amount"], errors="coerce").fillna(0.0)
    work = work[work["deposit_date"].notna()]
    return _agg_cash(work, "deposit_date", "amount")


def actual_cash_buckets_by_insurance(payments: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Actual cash by eob_date × payor/ins_name."""
    paid = payments[(payments["paid_amount"] > 0) & payments["eob_date"].notna()].copy()
    if paid.empty:
        return _agg_cash(paid, "eob_date", "paid_amount", ["ins_name"])
    if "ins_name" not in paid.columns:
        paid["ins_name"] = paid.get("payor", pd.Series([""] * len(paid))).astype(str)
    else:
        # Prefer ins_name; fill from payor
        paid["ins_name"] = paid["ins_name"].astype(str)
        if "payor" in paid.columns:
            blank = paid["ins_name"].eq("") | paid["ins_name"].eq("nan")
            paid.loc[blank, "ins_name"] = paid.loc[blank, "payor"].astype(str)
    return _agg_cash(paid, "eob_date", "paid_amount", ["ins_name"])


def actual_cash_buckets_by_facility(
    payments: pd.DataFrame, facility_lookup: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """Actual cash by eob_date × facility (via payment/webpt/name_key lookup when needed)."""
    paid = payments[(payments["paid_amount"] > 0) & payments["eob_date"].notna()].copy()
    if paid.empty:
        return _agg_cash(paid, "eob_date", "paid_amount", ["facility_name"])

    if "facility_name" not in paid.columns:
        paid["facility_name"] = ""
    else:
        paid["facility_name"] = (
            paid["facility_name"].astype(str).fillna("").replace({"nan": "", "None": ""})
        )

    blank = paid["facility_name"].str.strip().eq("")
    if blank.any() and facility_lookup is not None and not facility_lookup.empty:
        lookup_df = facility_lookup.copy()
        if "facility_name" in lookup_df.columns:
            if "webpt_patient_id" in lookup_df.columns and "webpt_patient_id" in paid.columns:
                by_pid = (
                    lookup_df[["webpt_patient_id", "facility_name"]]
                    .assign(
                        webpt_patient_id=lambda d: d["webpt_patient_id"].astype(str),
                        facility_name=lambda d: d["facility_name"]
                        .astype(str)
                        .str.strip(),
                    )
                    .loc[lambda d: d["facility_name"].ne("")]
                    .drop_duplicates("webpt_patient_id", keep="last")
                    .set_index("webpt_patient_id")["facility_name"]
                )
                filled = paid.loc[blank, "webpt_patient_id"].astype(str).map(by_pid)
                paid.loc[blank, "facility_name"] = filled.fillna("")
                blank = paid["facility_name"].str.strip().eq("")

            if blank.any() and "name_key" in lookup_df.columns and "name_key" in paid.columns:
                by_name = (
                    lookup_df[["name_key", "facility_name"]]
                    .assign(
                        name_key=lambda d: d["name_key"].astype(str),
                        facility_name=lambda d: d["facility_name"]
                        .astype(str)
                        .str.strip(),
                    )
                    .loc[lambda d: d["facility_name"].ne("") & d["name_key"].ne("")]
                    .drop_duplicates("name_key", keep="last")
                    .set_index("name_key")["facility_name"]
                )
                filled = paid.loc[blank, "name_key"].astype(str).map(by_name)
                paid.loc[blank, "facility_name"] = filled.fillna("")

    return _agg_cash(paid, "eob_date", "paid_amount", ["facility_name"])


def _land_date_col(outcomes: pd.DataFrame) -> str:
    """Bank-comparable land day: pre-pack original when present."""
    if "original_forecast_date" in outcomes.columns:
        return "original_forecast_date"
    return "forecast_date"


def _projected_base(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Cash Expected Land stages only (bank-comparable; excludes denied/rejected)."""
    col = _land_date_col(outcomes)
    proj = outcomes[
        outcomes["outcome_stage"].isin(["on_track", "overdue"])
        & outcomes[col].notna()
        & (outcomes["expected_amount"] > 0)
    ].copy()
    # Normalize aggregation column name for _agg_cash callers.
    if col != "forecast_date":
        # Prefer original; fill gaps from packed forecast_date.
        if "forecast_date" in proj.columns:
            miss = proj[col].isna()
            if miss.any():
                proj.loc[miss, col] = proj.loc[miss, "forecast_date"]
        proj["forecast_date"] = proj[col]
    return proj


def projected_cash_buckets(outcomes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Project expected cash by scheduled land date for non-paid outcomes."""
    proj = _projected_base(outcomes)
    return _agg_cash(proj, "forecast_date", "expected_amount")


def projected_cash_buckets_by_insurance(outcomes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    proj = _projected_base(outcomes)
    if "ins_name" not in proj.columns:
        proj["ins_name"] = ""
    return _agg_cash(proj, "forecast_date", "expected_amount", ["ins_name"])


def projected_cash_buckets_by_facility(outcomes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    proj = _projected_base(outcomes)
    if "facility_name" not in proj.columns:
        proj["facility_name"] = ""
    return _agg_cash(proj, "forecast_date", "expected_amount", ["facility_name"])


def projected_cash_monthly_by_facility_insurance(outcomes: pd.DataFrame) -> pd.DataFrame:
    proj = _projected_base(outcomes)
    if proj.empty:
        return pd.DataFrame(
            columns=["period", "facility_name", "ins_name", "amount", "line_count"]
        )
    proj["period"] = proj["forecast_date"].map(_month_label)
    g = (
        proj.groupby(["period", "facility_name", "ins_name"], as_index=False)
        .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
        .sort_values(["period", "amount"], ascending=[True, False])
    )
    g["amount"] = g["amount"].round(2)
    return g


def kpi_summary(
    outcomes: pd.DataFrame,
    payments: pd.DataFrame,
    risk_flags: pd.DataFrame,
    *,
    as_of: date,
    actual_cash_received: float | None = None,
) -> dict:
    if actual_cash_received is not None:
        actual = float(actual_cash_received)
    else:
        actual = (
            float(payments.loc[payments["paid_amount"] > 0, "paid_amount"].sum())
            if not payments.empty
            else 0.0
        )
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

    may_aug = filter_period_to_window(
        projected_cash_buckets(outcomes)["monthly"]
    )
    may_aug_projected = float(may_aug["amount"].sum()) if not may_aug.empty else 0.0

    return {
        "as_of": as_of.isoformat(),
        "actual_cash_received": round(actual, 2),
        "projected_cash_in": round(projected_with_shift, 2),
        "projected_cash_may_aug": round(may_aug_projected, 2),
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
