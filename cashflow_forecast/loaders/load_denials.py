"""Load Waystar denials batch_*.csv or denials_merged.csv only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date, parse_money


def load_denials(path: Path | str) -> pd.DataFrame:
    """Load denials from a directory of batch_*.csv or a single merged CSV."""
    path = Path(path)
    frames: list[pd.DataFrame] = []
    if path.is_dir():
        files = sorted(path.glob("batch_*.csv"))
        if not files:
            merged = path / "denials_merged.csv"
            if merged.exists():
                files = [merged]
        for fp in files:
            frames.append(pd.read_csv(fp, dtype=str, keep_default_na=False))
    elif path.is_file():
        frames.append(pd.read_csv(path, dtype=str, keep_default_na=False))
    else:
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if "denial_id" in df.columns:
        df = df.drop_duplicates(subset=["denial_id"], keep="first")

    df["service_date"] = df.get("service_date", pd.Series([""] * len(df))).map(parse_date)
    df["denial_date"] = df.get("denial_date", pd.Series([""] * len(df))).map(parse_date)
    for col in ("denied_amount", "billed_amount", "allowed_amount"):
        if col in df.columns:
            df[col] = df[col].map(parse_money)
        else:
            df[col] = 0.0
    df["name_key"] = df["patient_name"].map(normalize_name_key)
    df["payer"] = df.get("payer", "").astype(str).str.strip()
    df["denial_category"] = df.get("denial_category", "").astype(str).str.strip()
    df["stage"] = df.get("stage", "").astype(str).str.strip()
    df["source"] = "denial"
    return df
