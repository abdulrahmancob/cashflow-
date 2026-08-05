"""Load cash-velocity lags and deposit weekday schedules from insurance_behavior."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

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


@dataclass(frozen=True)
class DepositSchedule:
    allowed_weekdays: frozenset[int]
    cadence: str

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
        if (
            top_day
            and top_pct is not None
            and top_pct >= _TOP_DEPOSIT_PCT_FALLBACK
            and cadence not in NO_SNAP_CADENCES
        ):
            idx = _weekday_token_to_idx(top_day)
            if idx is not None:
                allowed = _bank_weekdays_only(frozenset({idx}))

    if not allowed:
        return None
    return DepositSchedule(allowed_weekdays=allowed, cadence=cadence)


def snap_to_bank_business_day(raw: date) -> date:
    """Move weekend dates forward to the next bank weekday (Mon–Fri).

    Named for a future Federal-holiday calendar without renaming call sites.
    Today: Sat → Mon, Sun → Mon; Mon–Fri unchanged.
    """
    wd = raw.weekday()
    if wd < 5:
        return raw
    # Sat=5 → +2, Sun=6 → +1
    return raw + timedelta(days=(7 - wd))


def snap_to_deposit_weekdays(raw: date, allowed: frozenset[int]) -> date:
    """Move ``raw`` forward to the next allowed weekday (inclusive).

    ``allowed`` is filtered to Mon–Fri so cadence never lands on a weekend.
    """
    bank_allowed = _bank_weekdays_only(allowed)
    if not bank_allowed:
        return snap_to_bank_business_day(raw)
    if raw.weekday() in bank_allowed:
        return raw
    for offset in range(1, 8):
        candidate = raw + timedelta(days=offset)
        if candidate.weekday() in bank_allowed:
            return candidate
    return snap_to_bank_business_day(raw)


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
    """Next ``n_days`` Mon–Fri dates starting at ``floor`` (inclusive if bank day)."""
    start = snap_to_bank_business_day(floor)
    out: list[date] = []
    d = start
    while len(out) < n_days:
        if d.weekday() < 5:
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

    min_floor = snap_to_bank_business_day(raw)
    if floor is not None:
        min_floor = max(min_floor, snap_to_bank_business_day(floor))
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
