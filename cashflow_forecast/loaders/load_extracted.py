"""Load extracted CPT / daily notes / plans of care for forecast AR + forward volume."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date


def load_daily_notes(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["daily_note_id"] = df.get("daily_note_id", "").astype(str).str.strip()
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    df["facility_name"] = df.get("facility_name", "").astype(str).str.strip()
    df["insurance_name"] = df.get("insurance_name", "").astype(str).str.strip()
    df["patient_name"] = df.get("patient_name", "").astype(str).str.strip()
    df["date_of_daily_note"] = df.get("date_of_daily_note", pd.Series([""] * len(df))).map(parse_date)
    df["date_of_birth"] = df.get("date_of_birth", "").astype(str).str.strip()
    return df


def load_cpt_codes(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    df["daily_note_id"] = df.get("daily_note_id", "").astype(str).str.strip()
    df["patient_name"] = df.get("patient_name", "").astype(str).str.strip()
    df["insurance_name"] = df.get("insurance_name", "").astype(str).str.strip()
    df["cpt_code"] = df.get("cpt_code", "").astype(str).str.strip()
    df["modifier"] = df.get("modifier", "").astype(str).str.strip()
    df["date_of_daily_note"] = df.get("date_of_daily_note", pd.Series([""] * len(df))).map(parse_date)
    units = pd.to_numeric(df.get("units", 1), errors="coerce").fillna(1.0)
    df["units"] = units.clip(lower=1.0)
    return df


def load_plans_of_care(path: Path | str) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["patient_id"] = df.get("patient_id", "").astype(str).str.strip()
    df["poc_id"] = df.get("poc_id", "").astype(str).str.strip()
    df["frequency"] = df.get("frequency", "").astype(str).str.strip()
    df["duration"] = df.get("duration", "").astype(str).str.strip()
    df["date_of_plan_of_care"] = df.get("date_of_plan_of_care", pd.Series([""] * len(df))).map(
        parse_date
    )
    return df


def load_extracted_ar_lines(
    extracted_dir: Path | str,
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 5, 31),
) -> pd.DataFrame:
    """Extracted CPT in [start, end] → recon-like pending rows (default Jan–May).

    Ends before the Jun–Jul reconciliation DOS window to avoid double-counting.
    """
    extracted_dir = Path(extracted_dir)
    cpt_path = extracted_dir / "cpt_codes.csv"
    notes_path = extracted_dir / "daily_notes.csv"
    if not cpt_path.exists() or not notes_path.exists():
        return pd.DataFrame()

    cpt = load_cpt_codes(cpt_path)
    notes = load_daily_notes(notes_path)

    mask = cpt["date_of_daily_note"].notna()
    mask &= cpt["date_of_daily_note"].map(lambda d: start <= d <= end if d else False)
    cpt = cpt.loc[mask].copy()
    if cpt.empty:
        return pd.DataFrame()

    facility_map = (
        notes.drop_duplicates(subset=["daily_note_id"], keep="last")
        .set_index("daily_note_id")["facility_name"]
        .to_dict()
    )
    dob_map = (
        notes.drop_duplicates(subset=["daily_note_id"], keep="last")
        .set_index("daily_note_id")["date_of_birth"]
        .to_dict()
    )

    cpt["facility_name"] = cpt["daily_note_id"].map(facility_map).fillna("")
    # Fallback: latest note facility per patient
    missing = cpt["facility_name"].eq("")
    if missing.any():
        latest_fac = (
            notes[notes["facility_name"].ne("")]
            .sort_values("date_of_daily_note")
            .drop_duplicates(subset=["patient_id"], keep="last")
            .set_index("patient_id")["facility_name"]
            .to_dict()
        )
        cpt.loc[missing, "facility_name"] = cpt.loc[missing, "patient_id"].map(latest_fac).fillna("")

    out = pd.DataFrame(
        {
            "webpt_patient_id": cpt["patient_id"],
            "patient_name": cpt["patient_name"],
            "dob": cpt["daily_note_id"].map(dob_map).fillna(""),
            "facility_name": cpt["facility_name"],
            "ins_name": cpt["insurance_name"],
            "insurance_revflow": "",
            "date_of_service": cpt["date_of_daily_note"],
            "cpt_code": cpt["cpt_code"],
            "modifier": cpt["modifier"],
            "status": "pending",
            "paid_amount": 0.0,
            "allowed_amount": 0.0,
            "adjustment_amount": 0.0,
            "deductible_amount": 0.0,
            "eob_date": None,
            "units": cpt["units"],
            "daily_note_id": cpt["daily_note_id"],
            "source": "extracted_ar",
        }
    )
    out["name_key"] = out["patient_name"].map(normalize_name_key)
    return out.reset_index(drop=True)


def load_may_ar_lines(
    extracted_dir: Path | str,
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 5, 31),
) -> pd.DataFrame:
    """Alias for load_extracted_ar_lines (kept for callers/tests)."""
    return load_extracted_ar_lines(extracted_dir, start=start, end=end)
