"""Recurring dollars that are not residual AR: NYNM, patient cash, paper checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pandas as pd

from cashflow_forecast.batch_cadence import nynm_tuesday_on_slots
from cashflow_forecast.deposit_capacity import DepositEvent
from cashflow_forecast.forecast_components import COMPONENT_KNOWN, COMPONENT_STREAM, is_stream_payer
from cashflow_forecast.insurance_behavior_sla import snap_to_bank_business_day


def _weekday_bank_open(day: date) -> bool:
    """Monday through Friday. Production snap has no holiday calendar yet."""
    try:
        parsed = date(int(day.year), int(day.month), int(day.day))
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return parsed.weekday() < 5

PATIENT_TOKENS = ("visa", "cash", "copay", "patient pay", "credit card", "patient payment")


def is_patient_text(description: str, transaction_type: str = "") -> bool:
    text = f"{description or ''} {transaction_type or ''}".lower()
    return any(tok in text for tok in PATIENT_TOKENS)


def nynm_stream_frame(
    events: Iterable[DepositEvent],
    as_of: date,
    *,
    horizon_days: int = 21,
) -> pd.DataFrame:
    slots, sizes = nynm_tuesday_on_slots(list(events or []), as_of, horizon_days=horizon_days)
    recs: list[dict[str, Any]] = []
    for grain, days in slots.items():
        amount = round(float(sizes.get(grain) or 0), 2)
        if amount <= 0:
            continue
        for day in days:
            if day <= as_of:
                continue
            recs.append(
                {
                    "source": "nynm_stream",
                    "component": COMPONENT_STREAM,
                    "ins_name": "NYNM",
                    "expected_amount": amount,
                    "forecast_date": day,
                    "original_forecast_date": day,
                    "outcome_stage": "on_track",
                    "line_key": f"stream:nynm:{day.isoformat()}",
                    "grain": grain,
                }
            )
    return pd.DataFrame(recs)


def offset_stream_by_known(stream: pd.DataFrame, known: pd.DataFrame) -> pd.DataFrame:
    """A NYNM EOB already in known cash is not also a stream dollar."""
    if stream is None or stream.empty:
        return stream
    known_left = 0.0
    if known is not None and not known.empty and "ins_name" in known.columns:
        mask = known["ins_name"].fillna("").astype(str).map(is_stream_payer)
        known_left = float(
            pd.to_numeric(known.loc[mask, "expected_amount"], errors="coerce").fillna(0).sum()
        )
    if known_left <= 0:
        return stream
    out = stream.sort_values("forecast_date").copy()
    amounts = pd.to_numeric(out["expected_amount"], errors="coerce").fillna(0.0)
    for idx in out.index:
        if known_left <= 1e-9:
            break
        cur = float(amounts.at[idx])
        take = min(cur, known_left)
        amounts.at[idx] = round(cur - take, 2)
        known_left -= take
    out["expected_amount"] = amounts
    return out.loc[out["expected_amount"] > 1e-9].reset_index(drop=True)


def patient_weekday_frame(
    history: Iterable[tuple[date, float]],
    as_of: date,
    *,
    weeks: int = 8,
    horizon_days: int = 14,
) -> pd.DataFrame:
    """Weekday mean of the last 8 weeks of patient cash/card, forward only."""
    cutoff = as_of - timedelta(days=7 * weeks)
    by_wd: dict[int, list[float]] = defaultdict(list)
    for day, amount in history or []:
        if day is None or day > as_of or day < cutoff or amount <= 0:
            continue
        if not _weekday_bank_open(day):
            continue
        by_wd[day.weekday()].append(float(amount))
    means = {wd: sum(vals) / len(vals) for wd, vals in by_wd.items() if vals}
    recs: list[dict[str, Any]] = []
    cursor = as_of + timedelta(days=1)
    end = as_of + timedelta(days=horizon_days)
    while cursor <= end:
        if _weekday_bank_open(cursor) and cursor.weekday() in means:
            amt = round(means[cursor.weekday()], 2)
            recs.append(
                {
                    "source": "patient_stream",
                    "component": COMPONENT_STREAM,
                    "ins_name": "patient cash/card",
                    "expected_amount": amt,
                    "forecast_date": cursor,
                    "original_forecast_date": cursor,
                    "outcome_stage": "on_track",
                    "line_key": f"stream:patient:{cursor.isoformat()}",
                    "grain": "class:patient_cash",
                }
            )
        cursor += timedelta(days=1)
    return pd.DataFrame(recs)


def paper_check_frame(rows: Iterable[dict[str, Any]], as_of: date) -> pd.DataFrame:
    """Sheet checks land on Deposit Date when that day is still ahead of as_of."""
    recs: list[dict[str, Any]] = []
    for row in rows or []:
        amt = float(row.get("amount") or 0)
        if amt <= 0:
            continue
        raw = row.get("deposit_date")
        if isinstance(raw, datetime):
            day = raw.date()
        elif isinstance(raw, date):
            day = raw
        else:
            parsed = pd.to_datetime(raw, errors="coerce")
            if pd.isna(parsed):
                continue
            day = parsed.date()
        if day <= as_of:
            continue
        payer = str(row.get("payer") or row.get("description") or "")
        visa = "visa" in payer.lower()
        land = day if _weekday_bank_open(day) else snap_to_bank_business_day(day)
        if land <= as_of:
            continue
        recs.append(
            {
                "source": "paper_visa" if visa else "paper_check",
                "component": COMPONENT_KNOWN,
                "ins_name": "patient card" if visa else (payer or "paper check"),
                "expected_amount": round(amt, 2),
                "forecast_date": land,
                "original_forecast_date": land,
                "outcome_stage": "on_track",
                "line_key": f"paper:{row.get('check_number') or ''}:{land.isoformat()}:{round(amt, 2)}",
                "grain": "class:patient_cash" if visa else "class:paper_check",
            }
        )
    return pd.DataFrame(recs)


def tag_residual(frame: pd.DataFrame) -> pd.DataFrame:
    from cashflow_forecast.forecast_components import COMPONENT_RESIDUAL

    out = frame.copy() if frame is not None else pd.DataFrame()
    if out.empty:
        return out
    if "component" not in out.columns:
        out["component"] = None
    amount = pd.to_numeric(out.get("expected_amount"), errors="coerce").fillna(0.0)
    if "outcome_stage" in out.columns:
        stage = out["outcome_stage"]
    else:
        stage = pd.Series("", index=out.index)
    mask = out["component"].isna() & stage.isin(["on_track", "overdue"]) & (amount > 0)
    out.loc[mask, "component"] = COMPONENT_RESIDUAL
    return out
