"""Build payer SLA table: DOS → EOB lag per insurance (visit-level)."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd
import yaml

from cashflow_forecast.config import GLOBAL_MEDIAN_LAG_DAYS, MAX_LAG_DAYS, MIN_SLA_SAMPLES
from cashflow_forecast.utils import percentile


def _confidence(n: int) -> str:
    if n >= 30:
        return "high"
    if n >= 10:
        return "medium"
    if n >= MIN_SLA_SAMPLES:
        return "low"
    return "fallback"


def build_payer_sla(lines: pd.DataFrame) -> pd.DataFrame:
    """Compute visit-level lag stats grouped by WebPT insurance and RevFlow payor."""
    paid = lines[
        (lines["status"] == "paid")
        & (lines["paid_amount"] > 0)
        & lines["date_of_service"].notna()
        & lines["eob_date"].notna()
    ].copy()
    if paid.empty:
        return pd.DataFrame(
            columns=[
                "webpt_insurance",
                "revflow_payor",
                "sample_count",
                "median_lag_days",
                "avg_lag_days",
                "p75_lag_days",
                "p90_lag_days",
                "min_lag_days",
                "max_lag_days",
                "confidence",
            ]
        )

    paid["lag_days"] = paid.apply(
        lambda r: (r["eob_date"] - r["date_of_service"]).days
        if r["eob_date"] and r["date_of_service"]
        else None,
        axis=1,
    )
    paid = paid[paid["lag_days"].notna()]
    paid = paid[(paid["lag_days"] >= 0) & (paid["lag_days"] <= MAX_LAG_DAYS)]

    # Visit-level dedupe
    visit = paid.drop_duplicates(
        subset=["webpt_patient_id", "date_of_service", "ins_name"], keep="first"
    )

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for _, row in visit.iterrows():
        ins = (row.get("ins_name") or "UNKNOWN").strip() or "UNKNOWN"
        rev = (row.get("insurance_revflow") or ins).strip() or ins
        groups[(ins, rev)].append(int(row["lag_days"]))

    rows: list[dict[str, Any]] = []
    for (ins, rev), lags in sorted(groups.items(), key=lambda x: -len(x[1])):
        lags_sorted = sorted(lags)
        n = len(lags_sorted)
        rows.append(
            {
                "webpt_insurance": ins,
                "revflow_payor": rev,
                "sample_count": n,
                "median_lag_days": int(median(lags_sorted)) if n else GLOBAL_MEDIAN_LAG_DAYS,
                "avg_lag_days": round(mean(lags_sorted), 1) if n else float(GLOBAL_MEDIAN_LAG_DAYS),
                "p75_lag_days": int(percentile([float(x) for x in lags_sorted], 0.75)),
                "p90_lag_days": int(percentile([float(x) for x in lags_sorted], 0.90)),
                "min_lag_days": min(lags_sorted) if n else 0,
                "max_lag_days": max(lags_sorted) if n else 0,
                "confidence": _confidence(n),
            }
        )

    # Apply fallback median for low-sample rows
    for row in rows:
        if row["sample_count"] < MIN_SLA_SAMPLES:
            row["median_lag_days"] = GLOBAL_MEDIAN_LAG_DAYS
            row["confidence"] = "fallback"

    return pd.DataFrame(rows)


def sla_lookup(sla_df: pd.DataFrame) -> dict[str, int]:
    """Map webpt_insurance (lower) → median_lag_days."""
    lookup: dict[str, int] = {}
    if sla_df.empty:
        return lookup
    for _, row in sla_df.iterrows():
        key = str(row["webpt_insurance"]).strip().lower()
        if key and key not in lookup:
            lookup[key] = int(row["median_lag_days"])
        rev = str(row.get("revflow_payor") or "").strip().lower()
        if rev and rev not in lookup:
            lookup[rev] = int(row["median_lag_days"])
    return lookup


def get_lag_days(lookup: dict[str, int], insurance: str) -> int:
    key = (insurance or "").strip().lower()
    if key in lookup:
        return lookup[key]
    # Partial contains match
    for k, v in lookup.items():
        if k and (k in key or key in k):
            return v
    return GLOBAL_MEDIAN_LAG_DAYS


def write_payer_sla(sla_df: pd.DataFrame, csv_path: Path, yaml_path: Path | None = None) -> None:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    sla_df.to_csv(csv_path, index=False)
    if yaml_path is None:
        yaml_path = csv_path.with_suffix(".yaml")
    payload = {
        "global_fallback_days": GLOBAL_MEDIAN_LAG_DAYS,
        "payers": sla_df.to_dict(orient="records"),
    }
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
