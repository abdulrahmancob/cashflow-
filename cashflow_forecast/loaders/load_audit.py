"""Load WebPT audit CPT/ICD violation CSVs only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date


def load_audit(path: Path | str) -> pd.DataFrame:
    """Load cpt_violations.csv + icd_violations.csv from an audit directory."""
    path = Path(path)
    frames: list[pd.DataFrame] = []

    cpt_path = path / "cpt_violations.csv" if path.is_dir() else None
    icd_path = path / "icd_violations.csv" if path.is_dir() else None

    if path.is_file():
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df["violation_type"] = "cpt" if "cpt" in path.name.lower() else "icd"
        frames.append(df)
    else:
        if cpt_path and cpt_path.exists():
            cpt = pd.read_csv(cpt_path, dtype=str, keep_default_na=False)
            cpt["violation_type"] = "cpt"
            frames.append(cpt)
        if icd_path and icd_path.exists():
            icd = pd.read_csv(icd_path, dtype=str, keep_default_na=False)
            icd["violation_type"] = "icd"
            frames.append(icd)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    dos_col = "date_of_daily_note" if "date_of_daily_note" in df.columns else "service_date"
    df["date_of_service"] = df[dos_col].map(parse_date)
    df["name_key"] = df.get("patient_name", pd.Series([""] * len(df))).map(normalize_name_key)
    df["insurance_name"] = df.get("insurance_name", "").astype(str).str.strip()
    df["rule_id"] = df.get("rule_id", "").astype(str).str.strip()
    df["severity"] = df.get("severity", "").astype(str).str.strip().str.lower()
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    return df
