"""Load patients_export_*.csv only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key


def load_patients(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    if path.is_dir():
        candidates = sorted(path.glob("patients_export_*.csv"))
        if not candidates:
            return pd.DataFrame()
        path = candidates[-1]
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["name_key"] = df.get("patient_name", pd.Series([""] * len(df))).map(normalize_name_key)
    df["facility_name"] = df.get("facility_name", "").astype(str).str.strip()
    df["ins_name"] = df.get("ins_name", "").astype(str).str.strip()
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    return df
