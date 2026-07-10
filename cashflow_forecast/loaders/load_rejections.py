"""Load Waystar claims_rejected_all_merged.csv only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date, parse_money


def load_rejections(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["service_date"] = df.get("service_date", pd.Series([""] * len(df))).map(parse_date)
    df["transaction_date"] = df.get("transaction_date", pd.Series([""] * len(df))).map(parse_date)
    df["charges"] = df.get("charges", pd.Series(["0"] * len(df))).map(parse_money)
    df["name_key"] = df["patient_name"].map(normalize_name_key)
    df["payer"] = df.get("payer", "").astype(str).str.strip()
    df["status"] = df.get("status", "").astype(str).str.strip()
    df["source"] = "rejection"
    return df
