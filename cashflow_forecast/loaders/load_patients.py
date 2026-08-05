"""Load patients_export_*.csv (prefer longest window); fallback to patients_recent_273d."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key


def _prefer_patients_export(data_dir: Path) -> Path | None:
    """Prefer 273d, then 92d / 61d / 10d, else any patients_export_*.csv, else recent_273d."""
    preferred = [
        data_dir / "patients_export_273d.csv",
        data_dir / "patients_export_92d.csv",
        data_dir / "patients_export_61d.csv",
        data_dir / "patients_export_10d.csv",
    ]
    for path in preferred:
        if path.exists():
            return path
    candidates = sorted(data_dir.glob("patients_export_*.csv"))
    if candidates:
        return candidates[-1]
    recent = data_dir / "patients_recent_273d.csv"
    if recent.exists():
        return recent
    return None


def parse_auth_remaining(text: str) -> int | None:
    """Parse '3 of 25 Authorized ...' → remaining visits (22)."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)\s+of\s+(\d+)", text, re.IGNORECASE)
    if not m:
        return None
    used, authorized = int(m.group(1)), int(m.group(2))
    return max(authorized - used, 0)


def load_patients(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    if path.is_dir():
        chosen = _prefer_patients_export(path)
        if chosen is None:
            return pd.DataFrame()
        path = chosen
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["name_key"] = df.get("patient_name", pd.Series([""] * len(df))).map(normalize_name_key)
    df["facility_name"] = df.get("facility_name", "").astype(str).str.strip()
    df["ins_name"] = df.get("ins_name", "").astype(str).str.strip()
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    df["patient_name"] = df.get("patient_name", "").astype(str).str.strip()
    if "auth_ins_visits" in df.columns:
        df["auth_remaining"] = df["auth_ins_visits"].map(parse_auth_remaining)
    else:
        df["auth_remaining"] = None
    return df
