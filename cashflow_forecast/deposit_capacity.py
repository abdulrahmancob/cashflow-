"""Rolling per-payer_plan deposit capacity + priority-weighted FFD past-due pack."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Iterable

import pandas as pd

from cashflow_forecast.config import (
    BASELINE_DAYS_OF_DEPOSITS,
    CAPACITY_DRIFT_NORM_WARN,
    CAPACITY_SCALE_ALPHA,
    CAPACITY_SCALE_MAX,
    CAPACITY_SCALE_MIN,
    CAPACITY_SOFT_PENALTY_ALPHA,
    FALLBACK_TARGET_FLOOR,
    FALLBACK_TARGET_FRAC,
    MIN_CAP_SAMPLES,
    PACK_HORIZON_WEEKS,
    ROLLING_DEPOSIT_EVENTS,
    WEEKDAY_TARGET_EVENTS,
)
from cashflow_forecast.insurance_behavior_sla import (
    DepositSchedule,
    get_deposit_schedule,
    snap_to_bank_business_day,
    snap_to_deposit_weekdays,
)
from cashflow_forecast.payer_plan import PayerPlanKey, resolve_payer_plan

log = logging.getLogger(__name__)


# Age → collect probability (fallback curve when empirical buckets unavailable)
_DEFAULT_AGE_FACTORS: list[tuple[int, float]] = [
    (14, 1.00),
    (30, 0.85),
    (60, 0.60),
    (90, 0.35),
    (180, 0.15),
    (10_000, 0.05),
]


def week_of_month(d: date) -> int:
    return min((d.day - 1) // 7 + 1, 5)


def age_factor(overdue_days: int, curve: list[tuple[int, float]] | None = None) -> float:
    days = max(int(overdue_days or 0), 0)
    for upper, factor in curve or _DEFAULT_AGE_FACTORS:
        if days <= upper:
            return factor
    return 0.05


def status_factor(
    outcome_stage: str,
    reconcile_status: str = "",
    *,
    has_risk_docs: bool = False,
) -> float:
    stage = (outcome_stage or "").strip().lower()
    status = (reconcile_status or "").strip().lower()
    if stage in ("denied",) or status == "denied":
        return 0.35
    if stage == "rejected" or "reject" in status:
        return 0.40
    if has_risk_docs:
        return 0.45
    if stage in ("on_track", "overdue") or status in ("pending", "secondary_pending", ""):
        return 1.0
    return 0.7


def scale_for_outstanding(ratio: float) -> float:
    ratio = max(float(ratio), 0.0)
    scaled = ratio**CAPACITY_SCALE_ALPHA
    return max(CAPACITY_SCALE_MIN, min(CAPACITY_SCALE_MAX, scaled))


@dataclass
class DepositEvent:
    plan_key: str  # hierarchy key used for capacity (prefer plan:)
    amount: float
    deposit_date: date
    weekday: int
    week_of_month: int


def _hierarchy_keys_for_ins(ins_name: str, insurance_revflow: str = "") -> PayerPlanKey:
    return resolve_payer_plan(ins_name, insurance_revflow=insurance_revflow)


def build_deposit_events_from_actual(
    actual_daily_by_ins: pd.DataFrame,
    *,
    as_of: date,
) -> list[DepositEvent]:
    """Build deposit events from actual_cash_daily_by_insurance (period, ins_name, amount)."""
    if actual_daily_by_ins is None or actual_daily_by_ins.empty:
        return []
    events: list[DepositEvent] = []
    df = actual_daily_by_ins.copy()
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df = df[df["amount"] > 0]
    for row in df.itertuples(index=False):
        period = getattr(row, "period", None)
        if period is None:
            continue
        if hasattr(period, "date"):
            d = period.date() if not isinstance(period, date) else period
        else:
            try:
                d = date.fromisoformat(str(period)[:10])
            except ValueError:
                continue
        if d > as_of:
            continue
        ins = str(getattr(row, "ins_name", "") or "")
        key = _hierarchy_keys_for_ins(ins)
        grain = f"plan:{key.plan_key}" if key.plan_key else (f"org:{key.org_key}" if key.org_key else "")
        if not grain:
            continue
        events.append(
            DepositEvent(
                plan_key=grain,
                amount=float(getattr(row, "amount", 0) or 0),
                deposit_date=d,
                weekday=d.weekday(),
                week_of_month=week_of_month(d),
            )
        )
        # Also index class/org for fallback windows
        if key.class_key:
            events.append(
                DepositEvent(
                    plan_key=f"class:{key.class_key}",
                    amount=float(getattr(row, "amount", 0) or 0),
                    deposit_date=d,
                    weekday=d.weekday(),
                    week_of_month=week_of_month(d),
                )
            )
        if key.org_key:
            events.append(
                DepositEvent(
                    plan_key=f"org:{key.org_key}",
                    amount=float(getattr(row, "amount", 0) or 0),
                    deposit_date=d,
                    weekday=d.weekday(),
                    week_of_month=week_of_month(d),
                )
            )
    return events


def build_deposit_events_from_checks(
    checks_timeline: pd.DataFrame,
    *,
    as_of: date,
) -> list[DepositEvent]:
    """Optional richer source: insurance_behavior checks_timeline.csv."""
    if checks_timeline is None or checks_timeline.empty:
        return []
    events: list[DepositEvent] = []
    for row in checks_timeline.itertuples(index=False):
        dep = getattr(row, "deposit_date", None) or getattr(row, "eob_date", None)
        if dep is None or (isinstance(dep, float) and pd.isna(dep)):
            continue
        if hasattr(dep, "date"):
            d = dep.date() if not isinstance(dep, date) else dep
        else:
            try:
                d = date.fromisoformat(str(dep)[:10])
            except ValueError:
                continue
        if d > as_of:
            continue
        ins = str(getattr(row, "ins_name", "") or "")
        payor = str(getattr(row, "payor", "") or "")
        key = _hierarchy_keys_for_ins(ins, payor)
        amt = float(getattr(row, "paid_amount_sum", 0) or 0)
        if amt <= 0:
            continue
        for grain in key.hierarchy:
            events.append(
                DepositEvent(
                    plan_key=grain,
                    amount=amt,
                    deposit_date=d,
                    weekday=d.weekday(),
                    week_of_month=week_of_month(d),
                )
            )
    return events


def cap_base_for_slot(
    events: list[DepositEvent],
    grain_key: str,
    slot: date,
    *,
    fallback_keys: Iterable[str] = (),
    n_events: int = ROLLING_DEPOSIT_EVENTS,
) -> float:
    """Rolling median deposit for grain on slot weekday, preferring same week-of-month."""
    wd = slot.weekday()
    season = week_of_month(slot)

    def _window(key: str) -> list[DepositEvent]:
        matched = [e for e in events if e.plan_key == key and e.weekday == wd and e.deposit_date < slot]
        matched.sort(key=lambda e: e.deposit_date, reverse=True)
        return matched[:n_events]

    keys = (grain_key, *fallback_keys)
    for key in keys:
        window = _window(key)
        if len(window) < MIN_CAP_SAMPLES:
            continue
        seasonal = [e.amount for e in window if e.week_of_month == season]
        if len(seasonal) >= MIN_CAP_SAMPLES:
            return float(median(seasonal))
        return float(median(e.amount for e in window))
    return 0.0


def compute_outstanding_by_plan(outcomes: pd.DataFrame) -> dict[str, float]:
    if outcomes is None or outcomes.empty:
        return {}
    open_stages = {"on_track", "overdue"}
    totals: dict[str, float] = defaultdict(float)
    cache: dict[tuple[str, str], PayerPlanKey] = {}
    for row in outcomes.itertuples(index=False):
        stage = str(getattr(row, "outcome_stage", "") or "")
        if stage not in open_stages:
            continue
        amt = float(getattr(row, "expected_amount", 0) or 0)
        if amt <= 0:
            continue
        ins = str(getattr(row, "ins_name", "") or "")
        rev = str(getattr(row, "insurance_revflow", "") or "")
        ck = (ins.lower(), rev.lower())
        if ck not in cache:
            cache[ck] = _hierarchy_keys_for_ins(ins, rev)
        key = cache[ck]
        for h in key.hierarchy:
            totals[h] += amt
    return dict(totals)


def compute_baseline_by_plan(events: list[DepositEvent]) -> dict[str, float]:
    """Baseline open-AR proxy ≈ median daily deposit × BASELINE_DAYS_OF_DEPOSITS."""
    by_key: dict[str, list[float]] = defaultdict(list)
    for e in events:
        by_key[e.plan_key].append(e.amount)
    return {
        k: float(median(v)) * BASELINE_DAYS_OF_DEPOSITS for k, v in by_key.items() if v
    }


def build_weekday_deposit_probs(
    events: list[DepositEvent],
    *,
    as_of: date,
    n_events: int = ROLLING_DEPOSIT_EVENTS,
    min_samples: int = MIN_CAP_SAMPLES,
) -> tuple[dict[str, dict[int, float]], dict[int, float]]:
    """Per-grain and global Mon–Fri deposit share probs for weekend spill assign.

    Returns (grain_probs, global_probs) where each probs maps weekday 0..4 → share.
    """
    # Recent amounts by grain × weekday (plan: preferred; also class/org keys present)
    grain_wd: dict[str, dict[int, list[tuple[date, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    global_wd: dict[int, list[tuple[date, float]]] = defaultdict(list)

    for e in events:
        if e.deposit_date > as_of or e.weekday > 4:
            continue
        grain_wd[e.plan_key][e.weekday].append((e.deposit_date, e.amount))
        if e.plan_key.startswith("plan:"):
            global_wd[e.weekday].append((e.deposit_date, e.amount))

    def _probs_from_wd(
        wd_lists: dict[int, list[tuple[date, float]]],
    ) -> dict[int, float] | None:
        weights: dict[int, float] = {d: 0.0 for d in range(5)}
        n_total = 0
        for d in range(5):
            rows = sorted(wd_lists.get(d, []), key=lambda x: x[0], reverse=True)[
                :n_events
            ]
            weights[d] = float(sum(a for _, a in rows))
            n_total += len(rows)
        if n_total < min_samples:
            return None
        total = sum(weights.values())
        if total <= 1e-12:
            return None
        return {d: weights[d] / total for d in range(5)}

    grain_probs: dict[str, dict[int, float]] = {}
    for grain, wd_lists in grain_wd.items():
        p = _probs_from_wd(wd_lists)
        if p is not None:
            grain_probs[grain] = p

    global_probs = _probs_from_wd(global_wd) or {d: 0.2 for d in range(5)}
    return grain_probs, global_probs


def resolve_spill_probs_for_ins(
    ins_name: str,
    insurance_revflow: str,
    grain_probs: dict[str, dict[int, float]],
    global_probs: dict[int, float],
) -> tuple[dict[int, float], str]:
    """Pick historical → org/class fallback → global → uniform spill weights."""
    key = resolve_payer_plan(ins_name, insurance_revflow=insurance_revflow)
    for h in key.hierarchy:
        if h in grain_probs:
            return grain_probs[h], "historical"
    if global_probs and sum(global_probs.values()) > 1e-12:
        return global_probs, "global"
    return {d: 0.2 for d in range(5)}, "uniform"


def payer_plan_factor(grain_key: str, collect_rates: dict[str, float] | None = None) -> float:
    rates = collect_rates or {}
    if grain_key in rates:
        return max(0.05, min(1.0, rates[grain_key]))
    return 1.0


def compute_priority(
    *,
    expected_amount: float,
    overdue_days: int,
    outcome_stage: str,
    reconcile_status: str = "",
    has_risk_docs: bool = False,
    plan_grain_key: str = "",
    collect_rates: dict[str, float] | None = None,
) -> float:
    return (
        float(expected_amount)
        * age_factor(overdue_days)
        * status_factor(outcome_stage, reconcile_status, has_risk_docs=has_risk_docs)
        * payer_plan_factor(plan_grain_key, collect_rates)
    )


def _deposit_slots(
    start: date,
    schedule: DepositSchedule | None,
    *,
    weeks: int = PACK_HORIZON_WEEKS,
) -> list[date]:
    """Generate upcoming deposit dates for a plan from as_of (Mon–Fri only)."""
    end = start + timedelta(weeks=weeks)
    slots: list[date] = []
    if schedule is not None and schedule.snaps and schedule.allowed_weekdays:
        allowed = frozenset(d for d in schedule.allowed_weekdays if d < 5)
        if not allowed:
            log.warning(
                "Deposit schedule had only weekend days (cadence=%s); using Mon–Fri",
                schedule.cadence,
            )
        else:
            d = snap_to_deposit_weekdays(start, allowed)
            while d <= end:
                if d.weekday() in allowed and d >= start:
                    if d.weekday() >= 5:
                        log.warning("Weekend pack slot generated: %s (layer bug)", d)
                        d = snap_to_bank_business_day(d)
                    slots.append(d)
                d += timedelta(days=1)
                if len(slots) >= weeks * max(len(allowed), 1):
                    break
            return slots
    # near-daily / no snap: weekdays Mon–Fri
    d = start
    while d <= end:
        if d.weekday() < 5:
            slots.append(d)
        d += timedelta(days=1)
    return slots


def _guard_bank_slot(slot: date, *, context: str) -> date:
    """Assertion-style guard: weekend reaching the pack is a layer bug."""
    if slot.weekday() < 5:
        return slot
    log.warning(
        "Weekend pack %s on %s — fail-safe snap to bank business day",
        context,
        slot.isoformat(),
    )
    return snap_to_bank_business_day(slot)


def _lookup_schedule(
    deposit_schedules: dict[str, DepositSchedule] | None,
    ins_name: str,
    insurance_revflow: str,
) -> DepositSchedule | None:
    if insurance_revflow:
        s = get_deposit_schedule(deposit_schedules, insurance_revflow)
        if s is not None:
            return s
    return get_deposit_schedule(deposit_schedules, ins_name)


def weekday_deposit_targets(
    *,
    as_of: date,
    deposit_events: list[DepositEvent] | None = None,
    actual_cash_daily: pd.DataFrame | None = None,
    n_events: int = WEEKDAY_TARGET_EVENTS,
) -> dict[int, float]:
    """Median total daily deposit by weekday (last N days of that weekday)."""
    by_date: dict[date, float] = defaultdict(float)
    if actual_cash_daily is not None and not actual_cash_daily.empty:
        df = actual_cash_daily.copy()
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
        for row in df.itertuples(index=False):
            period = getattr(row, "period", None)
            if period is None:
                continue
            if hasattr(period, "date"):
                d = period.date() if not isinstance(period, date) else period
            else:
                try:
                    d = date.fromisoformat(str(period)[:10])
                except ValueError:
                    continue
            if d > as_of:
                continue
            by_date[d] += float(getattr(row, "amount", 0) or 0)
    elif deposit_events:
        # Sum plan: grains only — class/org rows are duplicates of the same cash.
        for e in deposit_events:
            if not e.plan_key.startswith("plan:"):
                continue
            if e.deposit_date > as_of:
                continue
            by_date[e.deposit_date] += e.amount

    by_wd: dict[int, list[float]] = defaultdict(list)
    for d, amt in sorted(by_date.items(), reverse=True):
        if amt <= 0:
            continue
        by_wd[d.weekday()].append(amt)

    targets: dict[int, float] = {}
    for wd, amts in by_wd.items():
        window = amts[:n_events]
        if window:
            targets[wd] = float(median(window))
    return targets


def _plan_weekday_median(
    events: list[DepositEvent],
    grain_key: str,
    weekday: int,
    *,
    before: date,
    n_events: int = ROLLING_DEPOSIT_EVENTS,
) -> float | None:
    matched = [
        e
        for e in events
        if e.plan_key == grain_key and e.weekday == weekday and e.deposit_date < before
    ]
    matched.sort(key=lambda e: e.deposit_date, reverse=True)
    window = matched[:n_events]
    if len(window) < MIN_CAP_SAMPLES:
        return None
    return float(median(e.amount for e in window))


def compute_parent_shares(
    children: list[str],
    *,
    parent_key: str,
    slot: date,
    deposit_events: list[DepositEvent],
    outstanding: dict[str, float],
) -> tuple[dict[str, float], str]:
    """Share parent Cap across sibling plans.

    Order: historical deposit share → outstanding share → equal.
    """
    kids = list(children)
    if not kids:
        return {}, "equal"
    if len(kids) == 1:
        return {kids[0]: 1.0}, "outstanding"

    medians: dict[str, float] = {}
    for c in kids:
        m = _plan_weekday_median(
            deposit_events, c, slot.weekday(), before=slot
        )
        if m is not None and m > 0:
            medians[c] = m
    if len(medians) == len(kids):
        total = sum(medians.values())
        if total > 1e-9:
            return {c: medians[c] / total for c in kids}, "historical"

    outs = {c: float(outstanding.get(c, 0.0) or 0.0) for c in kids}
    total_o = sum(outs.values())
    if total_o > 1e-9:
        return {c: outs[c] / total_o for c in kids}, "outstanding"

    n = float(len(kids))
    return {c: 1.0 / n for c in kids}, "equal"


def resolve_cap_hit(
    deposit_events: list[DepositEvent],
    grain: str,
    key: PayerPlanKey,
    slot: date,
) -> tuple[str, float, str]:
    """Return (hit_key, cap_base, source) with source in plan|class|org|fallback."""
    base = cap_base_for_slot(deposit_events, grain, slot, fallback_keys=())
    if base > 0:
        return grain, base, "plan"
    if key.class_key:
        ck = f"class:{key.class_key}"
        base = cap_base_for_slot(deposit_events, ck, slot, fallback_keys=())
        if base > 0:
            return ck, base, "class"
    if key.org_key:
        ok = f"org:{key.org_key}"
        base = cap_base_for_slot(deposit_events, ok, slot, fallback_keys=())
        if base > 0:
            return ok, base, "org"
    return "", 0.0, "fallback"


def thin_fallback_cap(target: float) -> float:
    return max(float(target) * FALLBACK_TARGET_FRAC, FALLBACK_TARGET_FLOOR)


def pack_pastdue_ffd(
    outcomes: pd.DataFrame,
    *,
    as_of: date,
    deposit_events: list[DepositEvent],
    deposit_schedules: dict[str, DepositSchedule] | None = None,
    risk_patient_dos: set[tuple[str, date]] | None = None,
    collect_rates: dict[str, float] | None = None,
    actual_cash_daily: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Reschedule past-due forecast_dates via shared Cap + day-normalize + FFD.

    Pipeline: Cap_base (share parent once) → outstanding scale → raw →
    normalize to weekday Target → reserve future → FFD.

    Returns (updated_outcomes, reschedule_audit, slot_capacity_audit, deposit_capacity).
    """
    empty = pd.DataFrame()
    if outcomes is None or outcomes.empty:
        return outcomes, empty, empty, empty

    out = outcomes.copy()
    if "forecast_date" not in out.columns:
        return out, empty, empty, empty

    out["_fd"] = pd.to_datetime(out["forecast_date"], errors="coerce")
    outstanding = compute_outstanding_by_plan(out)
    baselines = compute_baseline_by_plan(deposit_events)
    risk_keys = risk_patient_dos or set()
    wd_targets = weekday_deposit_targets(
        as_of=as_of,
        deposit_events=deposit_events,
        actual_cash_daily=actual_cash_daily,
    )

    audit_rows: list[dict] = []
    plan_groups: dict[str, list] = defaultdict(list)
    meta: dict = {}
    grain_key: dict[str, PayerPlanKey] = {}
    grain_schedule: dict[str, DepositSchedule | None] = {}

    for idx, row in out.iterrows():
        stage = str(row.get("outcome_stage") or "")
        if stage not in ("on_track", "overdue", "rejected", "denied"):
            continue
        amt = float(row.get("expected_amount") or 0)
        if amt <= 0:
            continue
        fd = row.get("_fd")
        if pd.isna(fd):
            continue
        fd_d = fd.date() if hasattr(fd, "date") else fd
        ins = str(row.get("ins_name") or "")
        rev = str(row.get("insurance_revflow") or "")
        key = _hierarchy_keys_for_ins(ins, rev)
        grain = (
            f"plan:{key.plan_key}"
            if key.plan_key
            else (f"class:{key.class_key}" if key.class_key else f"org:{key.org_key}")
        )
        if not grain or grain.endswith(":"):
            continue
        pid = str(row.get("webpt_patient_id") or "")
        dos = row.get("date_of_service")
        if hasattr(dos, "date"):
            dos_d = dos.date() if not isinstance(dos, date) else dos
        else:
            dos_d = dos if isinstance(dos, date) else None
        has_risk = bool(dos_d and (pid, dos_d) in risk_keys)
        overdue_days = int(row.get("overdue_days") or 0)
        if stage != "overdue" and isinstance(fd_d, date) and fd_d < as_of:
            overdue_days = max(overdue_days, (as_of - fd_d).days)
        pri = compute_priority(
            expected_amount=amt,
            overdue_days=overdue_days,
            outcome_stage=stage,
            reconcile_status=str(row.get("reconcile_status") or ""),
            has_risk_docs=has_risk,
            plan_grain_key=grain,
            collect_rates=collect_rates,
        )
        meta[idx] = {
            "grain": grain,
            "key": key,
            "amount": amt,
            "priority": pri,
            "fd": fd_d,
            "ins": ins,
            "rev": rev,
            "past_due": isinstance(fd_d, date) and fd_d < as_of,
        }
        plan_groups[grain].append(idx)
        grain_key[grain] = key
        if grain not in grain_schedule:
            grain_schedule[grain] = _lookup_schedule(deposit_schedules, ins, rev)

    grain_slots: dict[str, list[date]] = {}
    for grain in plan_groups:
        grain_slots[grain] = _deposit_slots(as_of, grain_schedule.get(grain))

    # ---- Build raw caps per (grain, slot) with parent sharing ----
    # Only grains that actually need the slot (past-due to pack, or future reserved
    # already sitting on it) — empty schedule members must not consume Target.
    slot_grains: dict[date, list[str]] = defaultdict(list)
    grain_needs_slot: dict[str, set[date]] = defaultdict(set)
    for grain, indices in plan_groups.items():
        slots_set = set(grain_slots.get(grain) or [])
        if not slots_set:
            continue
        has_past = any(meta[i]["past_due"] for i in indices)
        for i in indices:
            fd_d = meta[i]["fd"]
            if meta[i]["past_due"]:
                continue
            if isinstance(fd_d, date) and fd_d in slots_set:
                grain_needs_slot[grain].add(fd_d)
        if has_past:
            for slot in slots_set:
                grain_needs_slot[grain].add(slot)
    for grain, needed in grain_needs_slot.items():
        for slot in needed:
            slot_grains[slot].append(grain)

    # Precompute scale per grain
    grain_scale: dict[str, float] = {}
    grain_outstand: dict[str, float] = {}
    grain_baseline: dict[str, float] = {}
    for grain, key in grain_key.items():
        outstand = outstanding.get(grain, 0.0)
        for h in key.hierarchy:
            if outstand <= 0:
                outstand = outstanding.get(h, 0.0)
        baseline = baselines.get(grain, 0.0)
        for h in key.hierarchy:
            if baseline <= 0:
                baseline = baselines.get(h, 0.0)
        ratio = outstand / baseline if baseline > 0 else 1.0
        grain_scale[grain] = scale_for_outstanding(ratio)
        grain_outstand[grain] = outstand
        grain_baseline[grain] = baseline

    # calibrated[grain][slot] after normalize
    calibrated: dict[str, dict[date, float]] = defaultdict(dict)
    raw_caps: dict[str, dict[date, float]] = defaultdict(dict)
    cap_detail: dict[tuple[str, date], dict] = {}

    for slot, grains in slot_grains.items():
        target = float(wd_targets.get(slot.weekday(), 0.0) or 0.0)
        # Resolve hit per grain
        hits: dict[str, tuple[str, float, str]] = {}
        for g in grains:
            hits[g] = resolve_cap_hit(deposit_events, g, grain_key[g], slot)

        # Group by parent hit when source is class/org
        parent_groups: dict[str, list[str]] = defaultdict(list)
        plan_owned: list[str] = []
        fallback_owned: list[str] = []
        parent_base: dict[str, float] = {}
        for g, (hit_key, base, source) in hits.items():
            if source in ("class", "org") and hit_key:
                parent_groups[hit_key].append(g)
                parent_base[hit_key] = base
            elif source == "plan":
                plan_owned.append(g)
            else:
                fallback_owned.append(g)

        shared_base: dict[str, float] = {}
        share_meta: dict[str, tuple[str, str, float]] = {}  # grain -> (parent, method, share)

        for g in plan_owned:
            shared_base[g] = hits[g][1]
            share_meta[g] = (hits[g][0], "plan", 1.0)

        for parent, kids in parent_groups.items():
            shares, method = compute_parent_shares(
                kids,
                parent_key=parent,
                slot=slot,
                deposit_events=deposit_events,
                outstanding=outstanding,
            )
            pbase = parent_base[parent]
            for g in kids:
                sh = float(shares.get(g, 0.0))
                shared_base[g] = pbase * sh
                share_meta[g] = (parent, method, sh)

        fb = thin_fallback_cap(target) if target > 0 else FALLBACK_TARGET_FLOOR
        for g in fallback_owned:
            shared_base[g] = fb
            share_meta[g] = ("", "fallback", 1.0)

        # Outstanding scale → raw
        raw_for_slot: dict[str, float] = {}
        for g in grains:
            raw_for_slot[g] = shared_base.get(g, 0.0) * grain_scale.get(g, 1.0)
            raw_caps[g][slot] = raw_for_slot[g]

        raw_sum = sum(raw_for_slot.values())
        norm = 1.0
        if target > 0 and raw_sum > target + 1e-9:
            # Soft penalty toward Target — not a hard land clamp on the forecast.
            ratio = target / raw_sum
            norm = ratio**CAPACITY_SOFT_PENALTY_ALPHA
            if norm < CAPACITY_DRIFT_NORM_WARN:
                log.warning(
                    "Capacity Model Drift: slot=%s raw=%.0f target=%.0f day_norm_factor=%.3f",
                    slot.isoformat(),
                    raw_sum,
                    target,
                    norm,
                )
        for g in grains:
            cal = raw_for_slot[g] * norm
            calibrated[g][slot] = cal
            parent, method, sh = share_meta[g]
            cap_detail[(g, slot)] = {
                "grain_key": g,
                "slot": slot.isoformat(),
                "weekday": slot.strftime("%a"),
                "week_of_month": week_of_month(slot),
                "parent_key": parent,
                "parent_share_method": method,
                "parent_share": round(sh, 6),
                "cap_base_shared": round(shared_base.get(g, 0.0), 2),
                "scale": round(grain_scale.get(g, 1.0), 4),
                "cap_raw": round(raw_for_slot[g], 2),
                "cap_calibrated": round(cal, 2),
                "day_norm_factor": round(norm, 6),
                "weekday_target": round(target, 2),
                "outstanding": round(grain_outstand.get(g, 0.0), 2),
                "baseline": round(grain_baseline.get(g, 0.0), 2),
            }

    # ---- Reserve + FFD ----
    remaining: dict[str, dict[date, float]] = {
        g: dict(calibrated.get(g, {})) for g in plan_groups
    }
    packed_by_slot: dict[date, float] = defaultdict(float)
    reserved_by_slot: dict[date, float] = defaultdict(float)
    fallback_raw_by_slot: dict[date, float] = defaultdict(float)

    for grain, indices in plan_groups.items():
        slots = grain_slots.get(grain) or []
        if not slots:
            continue
        rem = remaining.setdefault(grain, {})
        for slot in slots:
            rem.setdefault(slot, calibrated.get(grain, {}).get(slot, 0.0))

        scale = grain_scale.get(grain, 1.0)
        outstand = grain_outstand.get(grain, 0.0)
        baseline = grain_baseline.get(grain, 0.0)

        future_idxs = [i for i in indices if not meta[i]["past_due"]]
        past_idxs = [i for i in indices if meta[i]["past_due"]]
        for i in future_idxs:
            fd_d = meta[i]["fd"]
            if fd_d in rem:
                amt = meta[i]["amount"]
                rem[fd_d] = max(0.0, rem[fd_d] - amt)
                reserved_by_slot[fd_d] += amt

        past_idxs.sort(key=lambda i: (-meta[i]["priority"], -meta[i]["amount"], i))
        for i in past_idxs:
            amt = meta[i]["amount"]
            placed_slot: date | None = None
            overflow = False
            for slot in slots:
                if amt <= rem.get(slot, 0.0) + 1e-9:
                    placed_slot = slot
                    break
            if placed_slot is None:
                placed_slot = slots[-1]
                overflow = True
            placed_slot = _guard_bank_slot(placed_slot, context="placement")
            rem[placed_slot] = max(0.0, rem.get(placed_slot, 0.0) - amt)
            old_fd = meta[i]["fd"]
            out.at[i, "forecast_date"] = placed_slot
            packed_by_slot[placed_slot] += amt
            detail = cap_detail.get((grain, placed_slot), {})
            audit_rows.append(
                {
                    "grain_key": grain,
                    "ins_name": meta[i]["ins"],
                    "old_forecast_date": old_fd.isoformat()
                    if isinstance(old_fd, date)
                    else old_fd,
                    "new_forecast_date": placed_slot.isoformat(),
                    "expected_amount": amt,
                    "priority": round(meta[i]["priority"], 4),
                    "capacity_overflow": overflow,
                    "cap_scale": round(scale, 4),
                    "outstanding": round(outstand, 2),
                    "baseline": round(baseline, 2),
                    "parent_key": detail.get("parent_key", ""),
                    "parent_share_method": detail.get("parent_share_method", ""),
                    "cap_calibrated": detail.get("cap_calibrated", 0.0),
                }
            )

    for (g, slot), detail in cap_detail.items():
        if detail.get("parent_share_method") == "fallback":
            fallback_raw_by_slot[slot] += float(detail.get("cap_raw", 0.0) or 0.0)

    # Slot-level audit (pack grains)
    slot_audit_rows: list[dict] = []
    all_slots = sorted(set(slot_grains) | set(packed_by_slot) | set(reserved_by_slot))
    for slot in all_slots:
        grains = slot_grains.get(slot, [])
        raw_sum = sum(raw_caps.get(g, {}).get(slot, 0.0) for g in grains)
        cal_sum = sum(calibrated.get(g, {}).get(slot, 0.0) for g in grains)
        reserved = reserved_by_slot.get(slot, 0.0)
        remaining_cap = max(0.0, cal_sum - reserved)
        packed = packed_by_slot.get(slot, 0.0)
        target = float(wd_targets.get(slot.weekday(), 0.0) or 0.0)
        # Capacity diagnostic (may include denied/rejected packed onto the day)
        final_exp = reserved + packed
        methods = {
            cap_detail[(g, slot)]["parent_share_method"]
            for g in grains
            if (g, slot) in cap_detail
        }
        # Soft-penalty norm for the slot (same for all grains that day)
        norms = [
            float(cap_detail[(g, slot)].get("day_norm_factor") or 1.0)
            for g in grains
            if (g, slot) in cap_detail
        ]
        day_norm = float(min(norms)) if norms else 1.0
        slot_audit_rows.append(
            {
                "slot": slot.isoformat(),
                "weekday": slot.strftime("%a"),
                "weekday_target": round(target, 2),
                "raw_cap_sum": round(raw_sum, 2),
                "calibrated_cap_sum": round(cal_sum, 2),
                "fallback_cap_sum": round(fallback_raw_by_slot.get(slot, 0.0), 2),
                "reserved_future": round(reserved, 2),
                "remaining_capacity": round(remaining_cap, 2),
                "packed_overdue": round(packed, 2),
                "final_expected": round(final_exp, 2),
                "cash_expected_land": 0.0,  # filled below from on_track+overdue
                "day_norm_factor": round(day_norm, 6),
                "raw_over_target": round(raw_sum / target, 4) if target > 0 else None,
                "final_over_target": round(final_exp / target, 4) if target > 0 else None,
                "reserved_pct_of_calibrated": round(reserved / cal_sum, 4)
                if cal_sum > 1e-9
                else None,
                "parent_share_methods": "|".join(sorted(methods)),
                "n_grains": len(grains),
            }
        )

    capacity_df = pd.DataFrame(list(cap_detail.values()))
    if not capacity_df.empty:
        capacity_df = capacity_df.sort_values(["slot", "grain_key"]).reset_index(drop=True)

    out = out.drop(columns=["_fd"], errors="ignore")

    # Cash Expected Land = on_track + overdue only (bank-comparable)
    cash_by_slot: dict[date, float] = defaultdict(float)
    if out is not None and not out.empty and "forecast_date" in out.columns:
        for row in out.itertuples(index=False):
            stage = str(getattr(row, "outcome_stage", "") or "")
            if stage not in ("on_track", "overdue"):
                continue
            fd = getattr(row, "forecast_date", None)
            if hasattr(fd, "date") and not isinstance(fd, date):
                fd = fd.date()
            if not isinstance(fd, date):
                continue
            cash_by_slot[fd] += float(getattr(row, "expected_amount", 0) or 0)

    audit = pd.DataFrame(audit_rows)
    slot_audit = pd.DataFrame(slot_audit_rows)
    if not slot_audit.empty and "slot" in slot_audit.columns:
        slot_audit["cash_expected_land"] = slot_audit["slot"].map(
            lambda s: round(cash_by_slot.get(date.fromisoformat(str(s)[:10]), 0.0), 2)
        )
        if "weekday_target" in slot_audit.columns:
            slot_audit["cash_over_target"] = slot_audit.apply(
                lambda r: round(r["cash_expected_land"] / r["weekday_target"], 4)
                if float(r.get("weekday_target") or 0) > 0
                else None,
                axis=1,
            )
    return out, audit, slot_audit, capacity_df


def capacity_summary_frame(
    outcomes: pd.DataFrame,
    deposit_events: list[DepositEvent],
    *,
    as_of: date,
    deposit_schedules: dict[str, DepositSchedule] | None = None,
    actual_cash_daily: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return per-plan slot caps (same rules as pack). Prefer pack_pastdue_ffd's 4th return."""
    if outcomes is None or outcomes.empty:
        return pd.DataFrame()
    # Use a copy so caller's forecast_dates are not mutated by the pack pass.
    _, _, _, capacity_df = pack_pastdue_ffd(
        outcomes.copy(),
        as_of=as_of,
        deposit_events=deposit_events,
        deposit_schedules=deposit_schedules,
        actual_cash_daily=actual_cash_daily,
    )
    return capacity_df
