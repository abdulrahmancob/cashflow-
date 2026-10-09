"""Scheduled deposits from posted EOBs + mail checks in hand (week-1 known cash)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

import pandas as pd

from cashflow_forecast.forecast_components import COMPONENT_KNOWN
from cashflow_forecast.insurance_behavior_sla import (
    apply_holiday_shift,
    is_bank_open,
    snap_to_bank_business_day,
)
from cashflow_forecast.payer_plan import fill_blank_ins, resolve_payer_plan

STALE_BANK_DAYS = 3
MAX_SCHEDULE_SPILLS = 5


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def bank_days_past(land: date, as_of: date) -> int:
    """Count open bank days in (land, as_of]."""
    n = 0
    cursor = land + timedelta(days=1)
    while cursor <= as_of:
        if is_bank_open(cursor):
            n += 1
        cursor += timedelta(days=1)
    return n


def next_bank_day(d: date) -> date:
    cursor = d + timedelta(days=1)
    return snap_to_bank_business_day(cursor)


def lag_lookup_from_leadtime_rows(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    """Map raw payor + plan_key → median lead days (floored at 0)."""
    out: dict[str, int] = {}
    for row in rows or []:
        payor = str(row.get("payor") or "").strip().lower()
        raw = row.get("median_lead_days")
        if not payor or raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            lag = max(0, int(round(float(raw))))
        except (TypeError, ValueError):
            continue
        out[payor] = lag
        key = resolve_payer_plan(payor)
        if key.plan_key:
            out[key.plan_key] = lag
        if key.org_key:
            out[key.org_key] = lag
            out[f"org:{key.org_key}"] = lag
    return out


def scheduled_from_eobs(
    rows: list[dict[str, Any]],
    *,
    as_of: date,
    lag_lookup: dict[str, int] | None = None,
    default_lag: int = 2,
    holiday_shifts: dict[str, int] | None = None,
) -> pd.DataFrame:
    lags = {str(k).strip().lower(): int(v) for k, v in (lag_lookup or {}).items()}
    shifts = {str(k).strip().lower(): int(v) for k, v in (holiday_shifts or {}).items()}
    recs: list[dict[str, Any]] = []
    for row in rows or []:
        amt = float(row.get("paid_amount_sum") or 0)
        if amt <= 0:
            continue
        origin = _as_date(row.get("check_date")) or _as_date(row.get("eob_date"))
        if origin is None:
            continue
        payor = fill_blank_ins(str(row.get("payor_raw") or ""), "")
        key = resolve_payer_plan(payor)
        lag = lags.get(
            key.plan_key,
            lags.get(key.org_key, lags.get(payor.strip().lower(), default_lag)),
        )
        shift = shifts.get(
            key.plan_key,
            shifts.get(key.org_key, shifts.get(payor.strip().lower(), 1)),
        )
        raw_land = origin + timedelta(days=max(0, lag))
        if is_bank_open(raw_land):
            land = raw_land
        elif raw_land.weekday() >= 5:
            land = snap_to_bank_business_day(raw_land)
        else:
            land = apply_holiday_shift(raw_land, shift, as_of)
        # Expected date is source + payer lead. Do not slide a date that
        # has already arrived onto the next bank day (that double-counted
        # cash the tracker had not typed yet).
        if land <= as_of:
            continue
        recs.append(
            {
                "source": "eob_inflight",
                "component": COMPONENT_KNOWN,
                "ins_name": payor,
                "expected_amount": round(amt, 2),
                "forecast_date": land,
                "original_forecast_date": land,
                "outcome_stage": "on_track",
                "line_key": f"eob:{row.get('check_eft_num') or ''}:{origin.isoformat()}:{round(amt, 2)}",
                "grain": (
                    f"org:{key.org_key}"
                    if key.org_key
                    else (f"plan:{key.plan_key}" if key.plan_key else "")
                ),
            }
        )
    return pd.DataFrame(recs)


def scheduled_from_mail(
    rows: list[dict[str, Any]],
    *,
    as_of: date,
    default_lag: int = 1,
    holiday_shifts: dict[str, int] | None = None,
) -> pd.DataFrame:
    shifts = {str(k).strip().lower(): int(v) for k, v in (holiday_shifts or {}).items()}
    recs: list[dict[str, Any]] = []
    for row in rows or []:
        amt = float(row.get("amount") or 0)
        if amt <= 0:
            continue
        origin = _as_date(row.get("check_date_recognized")) or _as_date(row.get("bank_posting_date"))
        if origin is None:
            continue
        desc = str(row.get("description") or "mail")
        shift = shifts.get(desc.strip().lower(), 1)
        raw_land = origin + timedelta(days=default_lag)
        if is_bank_open(raw_land):
            land = raw_land
        elif raw_land.weekday() >= 5:
            land = snap_to_bank_business_day(raw_land)
        else:
            land = apply_holiday_shift(raw_land, shift, as_of)
        if land <= as_of:
            continue
        recs.append(
            {
                "source": "mail_inflight",
                "component": COMPONENT_KNOWN,
                "ins_name": desc,
                "expected_amount": round(amt, 2),
                "forecast_date": land,
                "original_forecast_date": land,
                "outcome_stage": "on_track",
                "grain": "",
            }
        )
    return pd.DataFrame(recs)


def scheduled_amounts_by_slot(scheduled: pd.DataFrame) -> dict[date, float]:
    """Sum expected_amount by forecast_date for pack displacement."""
    out: dict[date, float] = {}
    if scheduled is None or scheduled.empty or "forecast_date" not in scheduled.columns:
        return out
    for row in scheduled.itertuples(index=False):
        slot = _as_date(getattr(row, "forecast_date", None))
        amt = float(getattr(row, "expected_amount", 0) or 0)
        if slot is None or amt <= 0:
            continue
        out[slot] = out.get(slot, 0.0) + amt
    return out


def cap_scheduled_by_slot(
    scheduled: pd.DataFrame,
    weekday_targets: dict[int, float] | None,
    *,
    day_total_mult: float = 1.0,
    as_of: date | None = None,
    max_spills: int = MAX_SCHEDULE_SPILLS,
) -> pd.DataFrame:
    """Cap each day's scheduled sum at k×weekday target; spill overflow forward.

    Remaining after ``max_spills`` parks on the last spill day (not dropped).
    """
    if scheduled is None or scheduled.empty:
        return scheduled if scheduled is not None else pd.DataFrame()
    targets = weekday_targets or {}
    fallback = float(median(targets.values())) if targets else 0.0
    mult = float(day_total_mult if day_total_mult is not None else 1.0)
    pending: dict[date, list[dict[str, Any]]] = {}
    for row in scheduled.to_dict(orient="records"):
        slot = _as_date(row.get("forecast_date"))
        amt = float(row.get("expected_amount") or 0)
        if slot is None or amt <= 0:
            continue
        pending.setdefault(slot, []).append(dict(row))
    if not pending:
        return scheduled.iloc[0:0].copy()

    kept: list[dict[str, Any]] = []
    spills_from: dict[date, int] = {}
    horizon = (as_of or min(pending)) + timedelta(days=42)
    guard = 0
    while pending and guard < 80:
        guard += 1
        slot = min(pending)
        rows = pending.pop(slot)
        wd = slot.weekday()
        target = float(targets.get(wd, fallback) or 0)
        cap = mult * target if target > 0 else float("inf")
        used = 0.0
        overflow: list[dict[str, Any]] = []
        for rec in rows:
            amt = float(rec.get("expected_amount") or 0)
            if amt <= 0:
                continue
            if used >= cap:
                overflow.append(rec)
                continue
            room = cap - used
            if amt <= room + 1e-9:
                kept.append(rec)
                used += amt
            else:
                head = dict(rec)
                head["expected_amount"] = round(room, 2)
                kept.append(head)
                used += room
                tail = dict(rec)
                tail["expected_amount"] = round(amt - room, 2)
                overflow.append(tail)
        if overflow:
            n_spills = spills_from.get(slot, 0)
            nxt = next_bank_day(slot)
            if n_spills >= max_spills or nxt > horizon:
                for rec in overflow:
                    rec["forecast_date"] = slot
                    rec["original_forecast_date"] = rec.get("original_forecast_date") or slot
                    kept.append(rec)
            else:
                for rec in overflow:
                    rec["forecast_date"] = nxt
                    rec["original_forecast_date"] = rec.get("original_forecast_date") or nxt
                    pending.setdefault(nxt, []).append(rec)
                spills_from[nxt] = n_spills + 1
    if not kept:
        return scheduled.iloc[0:0].copy()
    return pd.DataFrame(kept)


def week1_bank_days(as_of: date, n: int = 5) -> list[date]:
    days: list[date] = []
    cursor = as_of + timedelta(days=1)
    while len(days) < n:
        if is_bank_open(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def overlay_scheduled(
    outcomes: pd.DataFrame,
    scheduled: pd.DataFrame,
    *,
    as_of: date | None = None,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """Append inflight rows and report scheduled share of packed dollars.

    Overdue claims that will be paid by these EOBs may still sit in AR; we do
    not drop them here (recon status is the gate). Scheduled share is the
    diagnostic for week-1 known-money coverage.
    """
    empty_meta = {
        "scheduled_sum": 0.0,
        "packed_sum": 0.0,
        "scheduled_share": 0.0,
        "week1_scheduled_sum": 0.0,
        "week1_pred_sum": 0.0,
        "week1_share": 0.0,
    }
    if scheduled is None or scheduled.empty:
        return outcomes, empty_meta
    extra = scheduled.copy()
    base = outcomes.copy()
    for col in extra.columns:
        if col not in base.columns:
            base[col] = None
    for col in base.columns:
        if col not in extra.columns:
            extra[col] = None
    extra = extra[list(base.columns)]
    out = pd.concat([base, extra], ignore_index=True)
    packed = 0.0
    if "expected_amount" in outcomes.columns and "outcome_stage" in outcomes.columns:
        packed = float(
            pd.to_numeric(
                outcomes.loc[outcomes["outcome_stage"].isin(["on_track", "overdue"]), "expected_amount"],
                errors="coerce",
            )
            .fillna(0)
            .sum()
        )
    sched = float(pd.to_numeric(scheduled["expected_amount"], errors="coerce").fillna(0).sum())
    share = sched / (sched + packed) if (sched + packed) > 1e-9 else 0.0
    week1_sched = 0.0
    week1_pred = 0.0
    if as_of is not None:
        w1 = {d.isoformat() for d in week1_bank_days(as_of)}
        if "forecast_date" in scheduled.columns:
            for row in scheduled.itertuples(index=False):
                slot = _as_date(getattr(row, "forecast_date", None))
                if slot is not None and slot.isoformat() in w1:
                    week1_sched += float(getattr(row, "expected_amount", 0) or 0)
        if "forecast_date" in out.columns and "expected_amount" in out.columns:
            for row in out.itertuples(index=False):
                slot = _as_date(getattr(row, "forecast_date", None))
                if slot is not None and slot.isoformat() in w1:
                    week1_pred += float(getattr(row, "expected_amount", 0) or 0)
    week1_share = week1_sched / week1_pred if week1_pred > 1e-9 else 0.0
    return out, {
        "scheduled_sum": round(sched, 2),
        "packed_sum": round(packed, 2),
        "scheduled_share": round(share, 6),
        "week1_scheduled_sum": round(week1_sched, 2),
        "week1_pred_sum": round(week1_pred, 2),
        "week1_share": round(week1_share, 6),
    }
