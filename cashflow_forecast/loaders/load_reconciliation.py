"""Load reconciliation_lines.csv and payments_unified.csv only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date, parse_money


def load_reconciliation_lines(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["date_of_service"] = df["date_of_service"].map(parse_date)
    df["eob_date"] = df.get("eob_date", pd.Series([""] * len(df))).map(parse_date)
    for col in ("paid_amount", "allowed_amount", "adjustment_amount", "deductible_amount"):
        if col in df.columns:
            df[col] = df[col].map(parse_money)
        else:
            df[col] = 0.0
    df["name_key"] = df["patient_name"].map(normalize_name_key)
    df["status"] = df.get("status", "").astype(str).str.strip().str.lower()
    return df


def load_payments_unified(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["date_of_service"] = df["date_of_service"].map(parse_date)
    df["eob_date"] = df.get("eob_date", pd.Series([""] * len(df))).map(parse_date)
    for col in ("paid_amount", "allowed_amount", "billed_amount", "adjustment_amount", "deductible_amount"):
        if col in df.columns:
            df[col] = df[col].map(parse_money)
        else:
            df[col] = 0.0
    if "name_key" not in df.columns:
        df["name_key"] = (
            df.get("last_name", "").astype(str) + " " + df.get("first_name", "").astype(str)
        ).map(normalize_name_key)
    return df
