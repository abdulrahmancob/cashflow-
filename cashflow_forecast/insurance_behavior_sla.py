"""Load cash-velocity lags and deposit weekday schedules from insurance_behavior."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Iterable

from cashflow_forecast.config import MIN_SLA_SAMPLES
from cashflow_reconcile.payer_registry import resolve

WEEKDAY_NAME_TO_IDX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

NO_SNAP_CADENCES = frozenset(
    {
        "",
        "near_daily",
        "irregular",
        "insufficient_history",
        "semi_monthly",
        "biweekly",
        "monthly",
    }
)

_TOP_DEPOSIT_PCT_FALLBACK = 45.0
_IRREGULAR_TOP_PCT_FALLBACK = 60.0
PHASE_GAP_MIN = 10
PHASE_REGULARITY_MIN = 0.70
PHASE_ON_DOM_MIN = 4
DOMINANT_HIT_MIN = 0.60
MIN_PIT_DEPOSITS = 6
_IDX_TO_TOKEN = ("mon", "tue", "wed", "thu", "fri")
_HOLIDAY_YEAR_START = 2019
_HOLIDAY_YEAR_END = 2031


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observe_federal(nominal: date) -> date:
    wd = nominal.weekday()
    if wd == 5:
        return nominal - timedelta(days=1)
    if wd == 6:
        return nominal + timedelta(days=1)
    return nominal


def observed_federal_bank_holidays(year: int) -> tuple[tuple[str, date], ...]:
    """Nominal-year Federal Reserve holidays, stored on their observed dates."""
    items: list[tuple[str, date]] = [
        ("new_years", _observe_federal(date(year, 1, 1))),
        ("mlk", _nth_weekday(year, 1, 0, 3)),
        ("presidents", _nth_weekday(year, 2, 0, 3)),
        ("memorial", _last_weekday(year, 5, 0)),
        ("independence", _observe_federal(date(year, 7, 4))),
        ("labor", _nth_weekday(year, 9, 0, 1)),
        ("columbus", _nth_weekday(year, 10, 0, 2)),
        ("veterans", _observe_federal(date(year, 11, 11))),
        ("thanksgiving", _nth_weekday(year, 11, 3, 4)),
        ("christmas", _observe_federal(date(year, 12, 25))),
    ]
    if year >= 2021:
        items.insert(4, ("juneteenth", _observe_federal(date(year, 6, 19))))
    return tuple(items)


def _build_bank_holiday_set() -> frozenset[date]:
    out: set[date] = set()
    for year in range(_HOLIDAY_YEAR_START, _HOLIDAY_YEAR_END + 1):
        for _name, observed in observed_federal_bank_holidays(year):
            out.add(observed)
    return frozenset(out)


_BANK_HOLIDAYS: frozenset[date] | None = None


def bank_holiday_dates() -> frozenset[date]:
    global _BANK_HOLIDAYS
    if _BANK_HOLIDAYS is None:
        _BANK_HOLIDAYS = _build_bank_holiday_set()
    return _BANK_HOLIDAYS


def is_bank_holiday(day: date) -> bool:
    try:
        parsed = date(int(day.year), int(day.month), int(day.day))
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return parsed in bank_holiday_dates()


def is_bank_open(day: date) -> bool:
    try:
        parsed = date(int(day.year), int(day.month), int(day.day))
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return parsed.weekday() < 5 and parsed not in bank_holiday_dates()


@dataclass(frozen=True)
class DepositSchedule:
    allowed_weekdays: frozenset[int]
    cadence: str
    anchor_date: date | None = None
    period_days: int | None = None
    holiday_shift: int = 1

    @property
    def snaps(self) -> bool:
        return bool(self.allowed_weekdays)


def _parse_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def period_days_from_cadence(cadence: str) -> int | None:
    text = (cadence or "").strip().lower()
    if text.startswith("biweekly"):
        return 14
    if text.startswith("monthly"):
        return 28
    return None


def on_cycle(day: date, anchor: date, period_days: int) -> bool:
    """True when ``day`` sits on the same every-N-days phase as ``anchor`` (±2d)."""
    if period_days <= 0:
        return True
    rem = (day - anchor).days % period_days
    return rem <= 2 or rem >= period_days - 2


def gap_regularity(dates: list[date], period: int, *, slack: int = 3) -> float:
    """Share of consecutive gaps that sit within ``period ± slack`` days."""
    if period <= 0 or len(dates) < 2:
        return 0.0
    ordered = sorted(dates)
    gaps = [(ordered[i] - ordered[i - 1]).days for i in range(1, len(ordered))]
    if not gaps:
        return 0.0
    ok = sum(1 for g in gaps if abs(g - period) <= slack)
    return ok / len(gaps)


def phase_confident(
    on_dom: list[tuple[date, float]],
    period: int,
    *,
    min_n: int = PHASE_ON_DOM_MIN,
    min_regularity: float = PHASE_REGULARITY_MIN,
) -> bool:
    """True when the dollar-majority phase is a stable every-N-days cycle.

    Regularity is measured on the winning phase only, so a stray off-cycle
    check on the same weekday cannot veto a real biweekly payer.
    """
    if period < PHASE_GAP_MIN or len(on_dom) < min_n:
        return False
    weights: dict[int, float] = {}
    by_phase: dict[int, list[date]] = {}
    for d, a in on_dom:
        r = d.toordinal() % period
        weights[r] = weights.get(r, 0.0) + float(a or 0)
        by_phase.setdefault(r, []).append(d)
    best = max(weights, key=lambda r: weights[r])
    cycle: list[date] = []
    for r, days in by_phase.items():
        dist = min((r - best) % period, (best - r) % period)
        if dist <= 2:
            cycle.extend(days)
    if len(cycle) < min_n:
        return False
    return gap_regularity(cycle, period) >= min_regularity


def dollar_weighted_anchor(
    on_dom: list[tuple[date, float]],
    period: int,
) -> date | None:
    """Last deposit on the dollar-majority phase (ordinal % period, ±2).

    A stray off-cycle check cannot flip the phase against larger on-cycle batches.
    """
    if not on_dom or period <= 0:
        return None
    weights: dict[int, float] = {}
    by_phase: dict[int, list[tuple[date, float]]] = {}
    for d, a in on_dom:
        r = d.toordinal() % period
        weights[r] = weights.get(r, 0.0) + float(a or 0)
        by_phase.setdefault(r, []).append((d, a))
    best = max(weights, key=lambda r: weights[r])
    cycle: list[tuple[date, float]] = []
    for r, pairs in by_phase.items():
        dist = min((r - best) % period, (best - r) % period)
        if dist <= 2:
            cycle.extend(pairs)
    if not cycle:
        return max(d for d, _a in on_dom)
    return max(d for d, _a in cycle)


def _weekday_token_to_idx(token: str) -> int | None:
    key = (token or "").strip().lower()[:3]
    return WEEKDAY_NAME_TO_IDX.get(key)


def _bank_weekdays_only(days: frozenset[int]) -> frozenset[int]:
    """Drop Sat/Sun — bank deposits do not land on weekends."""
    return frozenset(d for d in days if 0 <= d <= 4)


def parse_cadence_weekdays(cadence: str) -> frozenset[int]:
    """Parse deposit weekdays from a cadence label.

    Examples:
      weekly_fri → {4}
      biweekly_tue → {1}
      multi_weekday_fri_tue → {4, 1}
      near_daily / irregular → empty (no snap)

    Sat/Sun tokens are ignored (never allowed deposit days).
    """
    text = (cadence or "").strip().lower()
    if not text or text in NO_SNAP_CADENCES:
        return frozenset()

    multi = re.fullmatch(r"multi_weekday_([a-z]+)_([a-z]+)", text)
    if multi:
        days = {
            idx
            for token in multi.groups()
            if (idx := _weekday_token_to_idx(token)) is not None
        }
        return _bank_weekdays_only(frozenset(days))

    single = re.fullmatch(r"(?:weekly|biweekly|monthly)_([a-z]+)", text)
    if single:
        idx = _weekday_token_to_idx(single.group(1))
        if idx is None:
            return frozenset()
        return _bank_weekdays_only(frozenset({idx}))

    return frozenset()


def schedule_from_behavior_row(row: dict[str, str]) -> DepositSchedule | None:
    """Build a DepositSchedule from one payor_behavior_summary row, or None if no snap."""
    cadence = str(row.get("cadence") or "").strip()
    allowed = parse_cadence_weekdays(cadence)

    if not allowed:
        top_day = str(row.get("top_deposit_weekday") or "").strip()
        top_pct = _parse_float(row.get("top_deposit_weekday_pct"))
        irregular_ok = (
            cadence == "irregular"
            and top_pct is not None
            and top_pct >= _IRREGULAR_TOP_PCT_FALLBACK
        )
        other_ok = cadence not in NO_SNAP_CADENCES
        if (
            top_day
            and top_pct is not None
            and top_pct >= _TOP_DEPOSIT_PCT_FALLBACK
            and (irregular_ok or other_ok)
        ):
            idx = _weekday_token_to_idx(top_day)
            if idx is not None:
                allowed = _bank_weekdays_only(frozenset({idx}))

    if not allowed:
        return None
    anchor = _parse_date(row.get("last_deposit_date"))
    period = period_days_from_cadence(cadence)
    return DepositSchedule(
        allowed_weekdays=allowed,
        cadence=cadence,
        anchor_date=anchor,
        period_days=period,
    )


def fill_schedule_anchors(
    schedules: dict[str, DepositSchedule] | None,
    events: Iterable,
    as_of: date,
) -> dict[str, DepositSchedule]:
    """Attach / correct biweekly anchors via dollar-weighted phase vote.

    Last-deposit-on-weekday is not used: a stray off-cycle check would flip
    the phase. When regularity is too low, period is stripped (weekday-only).
    """
    if not schedules:
        return schedules or {}
    by: dict[tuple[str, int], list[tuple[date, float]]] = {}
    by_name: dict[str, list[tuple[date, float]]] = {}
    for ev in events or []:
        d = getattr(ev, "deposit_date", None)
        if d is None or d > as_of:
            continue
        grain = str(getattr(ev, "plan_key", "") or "")
        name = grain.split(":", 1)[-1].strip().lower()
        if not name:
            continue
        wd = d.weekday()
        amt = float(getattr(ev, "amount", 0) or 0)
        by.setdefault((name, wd), []).append((d, amt))
        by_name.setdefault(name, []).append((d, amt))
    out: dict[str, DepositSchedule] = {}
    for key, sch in schedules.items():
        name = key.strip().lower()
        shift = infer_holiday_shift(by_name.get(name, []), sch.allowed_weekdays, as_of)
        if not sch.period_days or sch.period_days < PHASE_GAP_MIN:
            out[key] = replace(sch, holiday_shift=shift)
            continue
        pairs: list[tuple[date, float]] = []
        for wd in sch.allowed_weekdays:
            pairs.extend(by.get((name, wd), []))
        if not pairs:
            out[key] = replace(sch, holiday_shift=shift)
            continue
        if not phase_confident(pairs, sch.period_days):
            out[key] = DepositSchedule(
                allowed_weekdays=sch.allowed_weekdays,
                cadence=sch.cadence,
                anchor_date=None,
                period_days=None,
                holiday_shift=shift,
            )
            continue
        anchor = dollar_weighted_anchor(pairs, sch.period_days)
        out[key] = DepositSchedule(
            allowed_weekdays=sch.allowed_weekdays,
            cadence=sch.cadence,
            anchor_date=anchor,
            period_days=sch.period_days,
            holiday_shift=shift,
        )
    return out


def pit_deposit_schedules(events: Iterable, as_of: date) -> dict[str, DepositSchedule]:
    """Build deposit schedules from as_of-gated deposit events (no snapshot leakage).

    Dominant weekday ≥60% and ≥6 deposits → snap. Period/anchor only when
    gap-regularity passes (biweekly 11–17d with ≥70% gaps in 14±3).
    """
    by: dict[str, list[tuple[date, float]]] = {}
    for ev in events or []:
        d = getattr(ev, "deposit_date", None)
        if d is None or d > as_of:
            continue
        grain = str(getattr(ev, "plan_key", "") or "")
        if not grain.startswith("plan:"):
            continue
        name = grain.split(":", 1)[-1].strip().lower()
        if not name:
            continue
        by.setdefault(name, []).append((d, float(getattr(ev, "amount", 0) or 0)))
    out: dict[str, DepositSchedule] = {}
    for name, pairs in by.items():
        if len(pairs) < MIN_PIT_DEPOSITS:
            continue
        pairs = sorted(pairs, key=lambda p: p[0])
        wd_counts: dict[int, int] = {}
        for d, _a in pairs:
            if 0 <= d.weekday() <= 4:
                wd_counts[d.weekday()] = wd_counts.get(d.weekday(), 0) + 1
        if not wd_counts:
            continue
        dominant = max(wd_counts, key=lambda w: wd_counts[w])
        hit = wd_counts[dominant] / max(len(pairs), 1)
        if hit < DOMINANT_HIT_MIN:
            continue
        on_dom = [(d, a) for d, a in pairs if d.weekday() == dominant]
        if len(on_dom) >= 2:
            gaps = [(on_dom[i][0] - on_dom[i - 1][0]).days for i in range(1, len(on_dom))]
            gap = float(median(gaps)) if gaps else 7.0
        else:
            gap = 7.0
        token = _IDX_TO_TOKEN[dominant] if 0 <= dominant < 5 else "fri"
        period: int | None = None
        cadence = f"weekly_{token}"
        if 11.0 <= gap <= 17.0:
            cadence = f"biweekly_{token}"
            if phase_confident(on_dom, 14):
                period = 14
        elif 5.0 <= gap <= 9.0:
            cadence = f"weekly_{token}"
        else:
            cadence = f"weekly_{token}"
        anchor = dollar_weighted_anchor(on_dom, period) if period else None
        allowed = frozenset({dominant})
        out[name] = DepositSchedule(
            allowed_weekdays=allowed,
            cadence=cadence,
            anchor_date=anchor,
            period_days=period,
            holiday_shift=infer_holiday_shift(pairs, allowed, as_of),
        )
    return out


def as_calendar_date(value: date | None) -> date | None:
    """Return a real ``datetime.date``, or None for NaT / NaN / missing."""
    if value is None:
        return None
    try:
        return date(int(value.year), int(value.month), int(value.day))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def snap_to_bank_business_day(raw: date) -> date:
    """Move weekend and federal bank holidays forward to the next open bank day.

    Sat/Sun → next Monday unless that Monday is a holiday (then the next open
    day). Weekday holidays such as Labor Day 2026-09-07 move to the following
    open day.
    """
    parsed = as_calendar_date(raw)
    if parsed is None:
        raise ValueError("snap_to_bank_business_day requires a real calendar date")
    cursor = parsed
    for _ in range(14):
        if is_bank_open(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def previous_bank_business_day(raw: date) -> date:
    """Last open bank day strictly before ``raw``."""
    parsed = as_calendar_date(raw)
    if parsed is None:
        raise ValueError("previous_bank_business_day requires a real calendar date")
    cursor = parsed - timedelta(days=1)
    for _ in range(14):
        if is_bank_open(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def apply_holiday_shift(
    closed: date,
    holiday_shift: int = 1,
    as_of: date | None = None,
) -> date:
    """Land a closed bank day on the neighboring open day.

    ``holiday_shift < 0`` uses the previous open day unless that day is
    already ``<= as_of``, in which case the next open day is used.
    """
    parsed = as_calendar_date(closed)
    if parsed is None:
        raise ValueError("apply_holiday_shift requires a real calendar date")
    if is_bank_open(parsed):
        return parsed
    if holiday_shift < 0:
        prev = previous_bank_business_day(parsed)
        if as_of is None or prev > as_of:
            return prev
    return snap_to_bank_business_day(parsed)


def align_to_weekday(raw: date, weekday: int) -> date:
    """Next calendar date on or after ``raw`` whose weekday matches ``weekday``."""
    parsed = as_calendar_date(raw) or raw
    cursor = parsed
    for _ in range(7):
        if cursor.weekday() == weekday:
            return cursor
        cursor += timedelta(days=1)
    return parsed


def infer_holiday_shift(
    deposits: Iterable[tuple[date, float]],
    allowed_weekdays: frozenset[int],
    as_of: date,
) -> int:
    """Dollar vote: previous vs next open day around past cadence-day holidays.

    Returns -1 when more dollars landed before the holiday, otherwise +1.
    """
    allowed = _bank_weekdays_only(allowed_weekdays)
    if not allowed:
        return 1
    deposit_map: dict[date, float] = {}
    for item in deposits or []:
        d, amt = item[0], float(item[1] or 0)
        parsed = as_calendar_date(d)
        if parsed is None or parsed > as_of:
            continue
        deposit_map[parsed] = deposit_map.get(parsed, 0.0) + amt
    before = 0.0
    after = 0.0
    for year in range(_HOLIDAY_YEAR_START, as_of.year + 1):
        for _name, observed in observed_federal_bank_holidays(year):
            if observed > as_of or observed.weekday() not in allowed:
                continue
            before += deposit_map.get(previous_bank_business_day(observed), 0.0)
            after += deposit_map.get(snap_to_bank_business_day(observed), 0.0)
    if before > after:
        return -1
    return 1


def next_cadence_day(
    raw: date,
    allowed: frozenset[int],
    *,
    cadence: str = "",
    anchor_date: date | None = None,
    period_days: int | None = None,
) -> date:
    """Next allowed weekday (inclusive). May land on a bank holiday."""
    parsed = as_calendar_date(raw)
    if parsed is None:
        raise ValueError("next_cadence_day requires a real calendar date")
    bank_allowed = _bank_weekdays_only(allowed)
    if not bank_allowed:
        return parsed
    candidate = parsed if parsed.weekday() in bank_allowed else None
    if candidate is None:
        for offset in range(1, 8):
            nxt = parsed + timedelta(days=offset)
            if nxt.weekday() in bank_allowed:
                candidate = nxt
                break
    if candidate is None:
        return parsed
    period = period_days if period_days is not None else period_days_from_cadence(cadence)
    if (
        period is not None
        and period >= PHASE_GAP_MIN
        and anchor_date is not None
        and not on_cycle(candidate, anchor_date, period)
    ):
        for extra in range(1, period + 8):
            nxt = candidate + timedelta(days=extra)
            if nxt.weekday() in bank_allowed and on_cycle(nxt, anchor_date, period):
                return nxt
    return candidate


def snap_to_deposit_weekdays(
    raw: date,
    allowed: frozenset[int],
    *,
    cadence: str = "",
    anchor_date: date | None = None,
    period_days: int | None = None,
    holiday_shift: int = 1,
    as_of: date | None = None,
) -> date:
    """Move ``raw`` forward to the next allowed weekday (inclusive).

    ``allowed`` is filtered to Mon–Fri so cadence never lands on a weekend.
    For biweekly/monthly cadences with an anchor, skip off-cycle weeks so
    every-other-Tuesday does not collapse to every Tuesday.
    When the cadence weekday is a bank holiday, apply ``holiday_shift``
    instead of jumping a full week.
    """
    bank_allowed = _bank_weekdays_only(allowed)
    if not bank_allowed:
        parsed = as_calendar_date(raw)
        if parsed is None:
            return snap_to_bank_business_day(raw)
        if parsed.weekday() >= 5:
            return snap_to_bank_business_day(parsed)
        if not is_bank_open(parsed):
            return apply_holiday_shift(parsed, holiday_shift, as_of)
        return parsed
    candidate = next_cadence_day(
        raw,
        bank_allowed,
        cadence=cadence,
        anchor_date=anchor_date,
        period_days=period_days,
    )
    if is_bank_open(candidate):
        return candidate
    if candidate.weekday() in bank_allowed:
        return apply_holiday_shift(candidate, holiday_shift, as_of)
    return snap_to_bank_business_day(candidate)


@dataclass(frozen=True)
class WeekendSpillResult:
    forecast_date: date
    spill_method: str  # historical | global | uniform
    weekday_probs: tuple[float, float, float, float, float]  # Mon..Fri


def _normalize_weekday_weights(weights: dict[int, float]) -> dict[int, float]:
    bank = {d: max(float(weights.get(d, 0.0) or 0.0), 0.0) for d in range(5)}
    total = sum(bank.values())
    if total <= 1e-12:
        return {d: 0.2 for d in range(5)}
    return {d: bank[d] / total for d in range(5)}


def forward_bank_candidates(floor: date, *, n_days: int = 5) -> list[date]:
    """Next ``n_days`` open bank dates starting at ``floor`` (inclusive if bank day)."""
    start = snap_to_bank_business_day(floor)
    out: list[date] = []
    d = start
    while len(out) < n_days:
        if is_bank_open(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def snap_weekend_by_historical_weekday(
    raw: date,
    weekday_probs: dict[int, float] | None,
    *,
    spill_method: str = "uniform",
    floor: date | None = None,
) -> WeekendSpillResult:
    """Assign a near_daily/no-cadence forecast date via Hist→Normalize→Forward→Assign.

    Does not split amounts: one line → one bank day. Forward-only from bank floor.
    """
    probs = _normalize_weekday_weights(weekday_probs or {})
    method = spill_method if weekday_probs else "uniform"
    if weekday_probs is None or sum(weekday_probs.values()) <= 1e-12:
        method = "uniform"
        probs = {d: 0.2 for d in range(5)}

    parsed = as_calendar_date(raw) or as_calendar_date(floor)
    if parsed is None:
        return WeekendSpillResult(
            forecast_date=date.today(),
            spill_method=method,
            weekday_probs=tuple(probs[d] for d in range(5)),
        )
    min_floor = snap_to_bank_business_day(parsed)
    if floor is not None:
        floor_parsed = as_calendar_date(floor)
        if floor_parsed is not None:
            min_floor = max(min_floor, snap_to_bank_business_day(floor_parsed))
    candidates = forward_bank_candidates(min_floor, n_days=5)

    def _key(d: date) -> tuple:
        # argmax p[weekday]; tie → closer (smaller offset), then lower weekday idx
        return (-probs.get(d.weekday(), 0.0), (d - min_floor).days, d.weekday())

    chosen = min(candidates, key=_key)
    return WeekendSpillResult(
        forecast_date=chosen,
        spill_method=method,
        weekday_probs=tuple(probs[d] for d in range(5)),
    )


def _row_lookup_keys(row: dict[str, str]) -> list[str]:
    keys = [
        str(row.get("dominant_ins_name") or "").strip().lower(),
        str(row.get("payor") or "").strip().lower(),
        str(row.get("payer_org_code") or "").strip().lower(),
        str(row.get("payer_org") or "").strip().lower(),
    ]
    if not keys[2] and not keys[3]:
        hit = (
            resolve(str(row.get("payor") or ""), "revflow")
            or resolve(str(row.get("dominant_ins_name") or ""), "webpt")
            or resolve(str(row.get("payor") or ""), "any")
        )
        if hit is not None:
            keys[2] = hit.code.lower()
            keys[3] = hit.name.lower()
    return [k for k in keys if k]


def cash_velocity_lookup_from_rows(rows: list[dict]) -> dict[str, int]:
    """Map payor / dominant_ins_name / payer_org (lower) → cash_velocity_median days."""
    lookup: dict[str, int] = {}
    for row in rows:
        n = _parse_int(row.get("eob_to_deposit_n"))
        velocity = _parse_int(
            row.get("cash_velocity_median") or row.get("median_cash_velocity_days")
        )
        if n is None or n < MIN_SLA_SAMPLES or velocity is None or velocity < 0:
            continue
        for key in _row_lookup_keys({k: str(v) if v is not None else "" for k, v in row.items()}):
            if key not in lookup:
                lookup[key] = velocity
    return lookup


def load_cash_velocity_lookup(summary_path: Path) -> dict[str, int]:
    """Map payor / dominant_ins_name / payer_org (lower) → cash_velocity_median days.

    Only includes rows with a valid cash_velocity_median and
    eob_to_deposit_n >= MIN_SLA_SAMPLES.
    """
    path = Path(summary_path)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return cash_velocity_lookup_from_rows(list(csv.DictReader(fh)))


def eob_to_deposit_lookup_from_rows(rows: list[dict]) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for row in rows:
        n = _parse_int(row.get("eob_to_deposit_n"))
        lag = _parse_int(
            row.get("eob_to_deposit_median") or row.get("median_eob_to_deposit_days")
        )
        if n is None or n < MIN_SLA_SAMPLES or lag is None or lag < 0:
            continue
        for key in _row_lookup_keys({k: str(v) if v is not None else "" for k, v in row.items()}):
            if key not in lookup:
                lookup[key] = lag
    return lookup


def load_eob_to_deposit_lookup(summary_path: Path) -> dict[str, int]:
    """Map payor / dominant_ins_name / payer_org (lower) → eob_to_deposit_median days.

    Used when a line already has eob_date: land = eob + lag, then cadence snap.
    """
    path = Path(summary_path)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return eob_to_deposit_lookup_from_rows(list(csv.DictReader(fh)))


def get_eob_to_deposit_days(
    lookup: dict[str, int] | None,
    insurance: str,
    insurance_revflow: str = "",
) -> int | None:
    """Resolve EOB→deposit lag for WebPT / RevFlow labels."""
    if not lookup:
        return None
    for raw in (insurance_revflow, insurance):
        key = (raw or "").strip().lower()
        if not key:
            continue
        if key in lookup:
            return lookup[key]
        hit = resolve(raw, "revflow") or resolve(raw, "webpt") or resolve(raw, "any")
        if hit is not None:
            for candidate in (hit.code.lower(), hit.name.lower()):
                if candidate in lookup:
                    return lookup[candidate]
    return None


def _schedule_row_keys(row: dict[str, str]) -> list[str]:
    """Exact keys only — do not index by payer_org (products differ by cadence)."""
    keys = [
        str(row.get("payor") or "").strip().lower(),
        str(row.get("dominant_ins_name") or "").strip().lower(),
    ]
    return [k for k in keys if k]


def deposit_schedule_lookup_from_rows(rows: list[dict]) -> dict[str, DepositSchedule]:
    lookup: dict[str, DepositSchedule] = {}
    for row in rows:
        str_row = {k: str(v) if v is not None else "" for k, v in row.items()}
        schedule = schedule_from_behavior_row(str_row)
        if schedule is None:
            continue
        for key in _schedule_row_keys(str_row):
            if key not in lookup:
                lookup[key] = schedule
    return lookup


def load_deposit_schedule_lookup(summary_path: Path) -> dict[str, DepositSchedule]:
    """Map payor / dominant_ins_name (lower) → deposit weekday schedule.

    Intentionally omits payer_org keys: one org (e.g. UHC) can have near_daily
    and weekly products; org-level indexing would mis-snap cash dates.
    """
    path = Path(summary_path)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return deposit_schedule_lookup_from_rows(list(csv.DictReader(fh)))


def get_deposit_schedule(
    lookup: dict[str, DepositSchedule] | None,
    insurance: str,
) -> DepositSchedule | None:
    """Resolve a deposit schedule for an insurance / payor string (exact keys only)."""
    if not lookup:
        return None
    key = (insurance or "").strip().lower()
    if not key:
        return None
    if key in lookup:
        return lookup[key]
    return None


def merge_velocity_into_lookup(
    base: dict[str, int],
    velocity: dict[str, int],
) -> dict[str, int]:
    """Overlay cash-velocity lags on top of DOS→EOB sla_lookup."""
    merged = dict(base)
    merged.update(velocity)
    return merged
