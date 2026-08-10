"""Synthesize August visit volume from Plans of Care."""

from __future__ import annotations

import re
from datetime import date, timedelta
from statistics import median

import pandas as pd

from cashflow_forecast.utils import normalize_name_key


AUG_START = date(2026, 8, 1)
AUG_END = date(2026, 8, 31)


def parse_frequency_per_week(text: str) -> float:
    """Parse '2-3 times a week' → 2.5; '3 times a week' → 3.0."""
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    if "visit only" in text or "one time" in text or re.search(r"\b1\s*time\s*visit", text):
        return 0.25  # ~1 visit in the month window if still active
    # Range first: 2-3, 1-2, 2 - 3
    m = re.search(r"(\d+)\s*[-–to]+\s*(\d+)", text)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return 0.0


def parse_duration_weeks(text: str) -> float:
    """Parse '12 weeks' / '12 MONTH' → weeks."""
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return 0.0
    n = float(m.group(1))
    if "month" in text:
        return n * 4.0
    return n


def _weekdays_in_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon–Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def _spread_visit_dates(n_visits: int, start: date, end: date) -> list[date]:
    if n_visits <= 0:
        return []
    weekdays = _weekdays_in_range(start, end)
    if not weekdays:
        return []
    if n_visits >= len(weekdays):
        return weekdays[:n_visits] if n_visits <= len(weekdays) else (
            weekdays * (n_visits // len(weekdays) + 1)
        )[:n_visits]
    # Evenly spaced indices
    if n_visits == 1:
        return [weekdays[len(weekdays) // 2]]
    step = (len(weekdays) - 1) / (n_visits - 1)
    return [weekdays[int(round(i * step))] for i in range(n_visits)]


def _build_cpt_mix(
    cpt: pd.DataFrame,
) -> tuple[dict[str, list[tuple[str, str, float]]], list[tuple[str, str, float]]]:
    """patient_id → list of (cpt, modifier, units) from most recent visit; plus global median mix."""
    by_patient: dict[str, list[tuple[str, str, float]]] = {}
    default = [("97110", "GP", 1.0), ("97140", "GP", 1.0), ("97112", "GP", 1.0)]
    if cpt is None or cpt.empty:
        return by_patient, default

    cpt = cpt.copy()
    cpt["patient_id"] = cpt["patient_id"].astype(str)
    cpt["cpt_code"] = cpt["cpt_code"].astype(str).str.strip()
    cpt = cpt[cpt["cpt_code"].ne("") & cpt["date_of_daily_note"].notna()]
    if cpt.empty:
        return by_patient, default

    latest_dos = cpt.groupby("patient_id")["date_of_daily_note"].transform("max")
    latest = cpt[cpt["date_of_daily_note"] == latest_dos]
    for pid, grp in latest.groupby("patient_id", sort=False):
        mix = [
            (
                str(r["cpt_code"]),
                str(r.get("modifier") or "").strip(),
                float(r.get("units") or 1) or 1.0,
            )
            for _, r in grp.iterrows()
        ]
        if mix:
            by_patient[str(pid)] = mix

    # Global top CPTs by frequency
    counts = cpt["cpt_code"].value_counts()
    top = list(counts.head(4).index)
    default = []
    for code in top:
        sub = cpt[cpt["cpt_code"] == code]
        units_med = float(median(sub["units"].astype(float).tolist())) if len(sub) else 1.0
        mod = str(sub.iloc[0].get("modifier") or "GP").strip() or "GP"
        default.append((code, mod, units_med))
    if not default:
        default = [("97110", "GP", 1.0)]
    return by_patient, default


def build_august_forward_lines(
    plans: pd.DataFrame,
    patients: pd.DataFrame,
    daily_notes: pd.DataFrame,
    cpt_codes: pd.DataFrame,
    *,
    fee_estimator=None,
    window_start: date = AUG_START,
    window_end: date = AUG_END,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return (synthetic pending lines, forward_visits summary).

    Active PoCs overlapping August → one pending line per projected weekday visit
    with precomputed_expected = sum of CPT-mix fee estimates.
    """
    if plans is None or plans.empty:
        empty = pd.DataFrame()
        return empty, empty

    plans = plans[plans["date_of_plan_of_care"].notna()].copy()
    plans["visits_per_week"] = plans["frequency"].map(parse_frequency_per_week)
    plans["duration_weeks"] = plans["duration"].map(parse_duration_weeks)
    plans = plans[(plans["visits_per_week"] > 0) & (plans["duration_weeks"] > 0)]
    plans["poc_end"] = plans.apply(
        lambda r: r["date_of_plan_of_care"] + timedelta(days=int(round(r["duration_weeks"] * 7))),
        axis=1,
    )
    # Overlap with August window
    plans = plans[
        (plans["date_of_plan_of_care"] <= window_end) & (plans["poc_end"] >= window_start)
    ]
    if plans.empty:
        empty = pd.DataFrame()
        return empty, empty

    # Latest PoC per patient
    plans = plans.sort_values("date_of_plan_of_care").drop_duplicates(
        subset=["patient_id"], keep="last"
    )

    # Patient enrichment
    pat = patients.copy() if patients is not None and not patients.empty else pd.DataFrame()
    if not pat.empty:
        if "patient_id" not in pat.columns and "webpt_patient_id" in pat.columns:
            pat["patient_id"] = pat["webpt_patient_id"]
        if "patient_id" in pat.columns:
            pat = pat.drop_duplicates(subset=["patient_id"], keep="last")
            pat_idx = pat.set_index("patient_id")
        else:
            pat_idx = pd.DataFrame()
    else:
        pat_idx = pd.DataFrame()

    # Fallback facility/ins from latest daily note
    note_fac: dict[str, str] = {}
    note_ins: dict[str, str] = {}
    note_name: dict[str, str] = {}
    if daily_notes is not None and not daily_notes.empty:
        dn = daily_notes.sort_values("date_of_daily_note")
        for _, r in dn.iterrows():
            pid = str(r["patient_id"])
            if r.get("facility_name"):
                note_fac[pid] = str(r["facility_name"])
            if r.get("insurance_name"):
                note_ins[pid] = str(r["insurance_name"])
            if r.get("patient_name"):
                note_name[pid] = str(r["patient_name"])

    by_patient_mix, default_mix = _build_cpt_mix(cpt_codes)

    def _visit_expected(mix: list[tuple[str, str, float]], ins: str) -> float:
        if fee_estimator is None:
            return 50.0 * sum(u for _, _, u in mix)
        total = 0.0
        for cpt_code, _, units in mix:
            total += fee_estimator.estimate(cpt_code, ins) * (float(units) or 1.0)
        return round(total, 2)

    line_rows: list[dict] = []
    visit_rows: list[dict] = []

    for _, poc in plans.iterrows():
        pid = str(poc["patient_id"])
        overlap_start = max(poc["date_of_plan_of_care"], window_start)
        overlap_end = min(poc["poc_end"], window_end)
        if overlap_start > overlap_end:
            continue

        weeks = (overlap_end - overlap_start).days / 7.0
        n_visits = int(round(float(poc["visits_per_week"]) * weeks))
        if n_visits <= 0:
            continue

        facility = ""
        ins = ""
        pname = note_name.get(pid, "")
        auth_cap = None
        if not pat_idx.empty and pid in pat_idx.index:
            prow = pat_idx.loc[pid]
            if isinstance(prow, pd.DataFrame):
                prow = prow.iloc[0]
            facility = str(prow.get("facility_name") or "")
            ins = str(prow.get("ins_name") or "")
            pname = str(prow.get("patient_name") or pname)
            ar = prow.get("auth_remaining")
            if ar is not None and not (isinstance(ar, float) and pd.isna(ar)):
                try:
                    auth_cap = int(ar)
                except (TypeError, ValueError):
                    auth_cap = None

        if not facility:
            facility = note_fac.get(pid, "")
        if not ins:
            ins = note_ins.get(pid, "")

        if auth_cap is not None:
            n_visits = min(n_visits, auth_cap)
        if n_visits <= 0:
            continue

        visit_dates = _spread_visit_dates(n_visits, overlap_start, overlap_end)
        mix = by_patient_mix.get(pid) or default_mix
        per_visit = _visit_expected(mix, ins)
        primary_cpt = mix[0][0] if mix else "97110"

        for vdos in visit_dates:
            line_rows.append(
                {
                    "webpt_patient_id": pid,
                    "patient_name": pname,
                    "dob": "",
                    "facility_name": facility,
                    "ins_name": ins,
                    "insurance_revflow": "",
                    "date_of_service": vdos,
                    "cpt_code": primary_cpt,
                    "modifier": mix[0][1] if mix else "GP",
                    "status": "pending",
                    "paid_amount": 0.0,
                    "allowed_amount": 0.0,
                    "adjustment_amount": 0.0,
                    "deductible_amount": 0.0,
                    "eob_date": None,
                    "units": 1.0,
                    "precomputed_expected": per_visit,
                    "daily_note_id": "",
                    "source": "forward_poc",
                    "name_key": normalize_name_key(pname),
                }
            )

        visit_rows.append(
            {
                "webpt_patient_id": pid,
                "patient_name": pname,
                "facility_name": facility,
                "ins_name": ins,
                "poc_id": poc.get("poc_id", ""),
                "poc_start": poc["date_of_plan_of_care"],
                "poc_end": poc["poc_end"],
                "visits_per_week": float(poc["visits_per_week"]),
                "projected_visit_count": len(visit_dates),
                "projected_line_count": len(visit_dates),
                "expected_amount": round(per_visit * len(visit_dates), 2),
                "auth_remaining": auth_cap if auth_cap is not None else "",
                "cpt_mix": ";".join(c for c, _, _ in mix),
            }
        )

    lines = pd.DataFrame(line_rows)
    summary = pd.DataFrame(visit_rows)
    return lines, summary


def attach_forward_expected_amounts(
    summary: pd.DataFrame, outcomes: pd.DataFrame
) -> pd.DataFrame:
    """Fill expected_$ on forward visit summary from classified outcomes (if missing)."""
    if summary is None or summary.empty:
        return summary
    out = summary.copy()
    if "expected_amount" in out.columns and float(out["expected_amount"].fillna(0).sum()) > 0:
        out["expected_amount"] = out["expected_amount"].fillna(0.0).round(2)
        return out
    if outcomes is None or outcomes.empty:
        out["expected_amount"] = 0.0
        return out
    if "source" in outcomes.columns:
        fwd = outcomes[outcomes["source"] == "forward_poc"]
    else:
        fwd = outcomes[
            outcomes["date_of_service"].map(
                lambda d: isinstance(d, date) and AUG_START <= d <= AUG_END
            )
        ]
    if fwd.empty:
        out["expected_amount"] = 0.0
        return out
    totals = (
        fwd.groupby("webpt_patient_id", as_index=False)
        .agg(expected_amount=("expected_amount", "sum"))
    )
    out = out.drop(columns=["expected_amount"], errors="ignore").merge(
        totals, on="webpt_patient_id", how="left"
    )
    out["expected_amount"] = out["expected_amount"].fillna(0.0).round(2)
    return out
