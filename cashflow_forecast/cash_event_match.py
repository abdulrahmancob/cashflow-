"""EOB / paper-check to bank matching with an auditable tier.

Definitive matches (EXACT, REF, single PAYER_WINDOW, single BATCH, MANUAL)
mean the cash is already in the tracker. HEURISTIC is report-only.
AMBIGUOUS never drops inflight. DEAD_NOT_BANKED is a forecast cutoff
(5 bank days), not a claim the money never existed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from cashflow_forecast.insurance_behavior_sla import is_bank_open, snap_to_bank_business_day
from cashflow_forecast.tracker_posting import last_settled_bank_date

FORECAST_DEAD_BANK_DAYS = 5
AMOUNT_EPS = 1.0

TIER_EXACT = "MATCHED_EXACT"
TIER_REF = "MATCHED_REF"
TIER_PAYER = "MATCHED_PAYER_WINDOW"
TIER_BATCH = "MATCHED_BATCH"
TIER_HEURISTIC = "MATCHED_HEURISTIC"
TIER_MANUAL = "MATCHED_MANUAL"
TIER_AMBIGUOUS = "AMBIGUOUS"
TIER_UNMATCHED = "UNMATCHED"

REASON_NOT_YET = "NOT_YET_DUE"
REASON_DEAD = "DEAD_NOT_BANKED"
REASON_ZERO = "ZERO_PAID"
REASON_LATE = "LATE_BANKED"

DEFINITIVE = frozenset({TIER_EXACT, TIER_REF, TIER_PAYER, TIER_BATCH, TIER_MANUAL})

_REF_RE = re.compile(r"[^A-Z0-9]")


def norm_ref(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text or text in {"#N/A", "NAN", "NONE"}:
        return ""
    return _REF_RE.sub("", text)


def bank_days_between(start: date, end: date) -> int:
    """Open bank days strictly after start through end."""
    if end <= start:
        return 0
    n = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if is_bank_open(cursor):
            n += 1
        cursor += timedelta(days=1)
    return n


def expected_bank_date(source: date, lead_days: int) -> date:
    raw = source + timedelta(days=max(0, int(lead_days)))
    return snap_to_bank_business_day(raw)


@dataclass
class CashEvent:
    event_id: str
    payer: str
    ref: str
    amount: float
    source_date: date
    lead_days: int = 2
    observed_at: date | None = None
    kind: str = "eob"


@dataclass
class BankTxn:
    row_id: str
    txn_date: date
    amount: float
    refs: tuple[str, ...] = ()
    description: str = ""
    observed_at: date | None = None


@dataclass
class MatchResult:
    event_id: str
    tier: str
    reason: str
    amount: float
    source_date: date
    expected_bank_date: date
    actual_bank_date: date | None
    observed_at: date | None
    bank_row_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    drops_inflight: bool = False
    component: str = "known_cash"


def _payer_hit(payer: str, description: str) -> bool:
    token = (payer or "").strip().lower()
    desc = (description or "").lower()
    if len(token) < 4 or not desc:
        return False
    head = token.split()[0]
    return head in desc or token[:12] in desc


def _visible(observed: date | None, as_of: date) -> bool:
    return observed is None or observed <= as_of


def match_cash_events(
    events: Iterable[CashEvent],
    banks: Iterable[BankTxn],
    *,
    as_of: date,
    dead_bank_days: int = FORECAST_DEAD_BANK_DAYS,
) -> list[MatchResult]:
    """Match events using only bank rows observed on or before ``as_of``."""
    bank_rows = [b for b in banks if _visible(b.observed_at, as_of) and b.amount > 0]
    by_ref: dict[str, list[BankTxn]] = {}
    for row in bank_rows:
        for ref in row.refs:
            key = norm_ref(ref)
            if key:
                by_ref.setdefault(key, []).append(row)
    used: set[str] = set()
    results: list[MatchResult] = []
    pending: list[CashEvent] = []

    for ev in events:
        exp = expected_bank_date(ev.source_date, ev.lead_days)
        if ev.amount <= 0:
            results.append(
                _result(ev, exp, TIER_UNMATCHED, REASON_ZERO, drops=True)
            )
            continue
        ref = norm_ref(ev.ref)
        exact = [b for b in by_ref.get(ref, []) if b.row_id not in used] if ref else []
        if len(exact) == 1:
            used.add(exact[0].row_id)
            results.append(
                _result(
                    ev, exp, TIER_EXACT, "", actual=exact[0].txn_date,
                    banks=(exact[0].row_id,), drops=True,
                )
            )
            continue
        if len(exact) > 1:
            results.append(
                _result(
                    ev, exp, TIER_AMBIGUOUS, "",
                    candidates=tuple(b.row_id for b in exact), drops=False,
                )
            )
            continue
        pending.append(ev)

    still: list[CashEvent] = []
    for ev in pending:
        exp = expected_bank_date(ev.source_date, ev.lead_days)
        window_end = ev.source_date + timedelta(days=max(ev.lead_days, 1) + 7)
        cands = [
            b for b in bank_rows
            if b.row_id not in used
            and abs(b.amount - ev.amount) <= AMOUNT_EPS
            and ev.source_date - timedelta(days=2) <= b.txn_date <= window_end
            and _payer_hit(ev.payer, b.description)
        ]
        if len(cands) == 1:
            used.add(cands[0].row_id)
            results.append(
                _result(
                    ev, exp, TIER_PAYER, "", actual=cands[0].txn_date,
                    banks=(cands[0].row_id,), drops=True,
                )
            )
        elif len(cands) > 1:
            results.append(
                _result(
                    ev, exp, TIER_AMBIGUOUS, "",
                    candidates=tuple(b.row_id for b in cands), drops=False,
                )
            )
        else:
            still.append(ev)

    still = _apply_batches(still, bank_rows, used, results)
    for ev in still:
        exp = expected_bank_date(ev.source_date, ev.lead_days)
        heur = [
            b for b in bank_rows
            if b.row_id not in used
            and abs(b.amount - ev.amount) <= AMOUNT_EPS
            and abs((b.txn_date - ev.source_date).days) <= 5
        ]
        age = bank_days_between(ev.source_date, as_of)
        if age > dead_bank_days and not heur:
            results.append(_result(ev, exp, TIER_UNMATCHED, REASON_DEAD, drops=True))
        elif heur:
            results.append(
                _result(
                    ev, exp, TIER_HEURISTIC, "",
                    actual=heur[0].txn_date,
                    candidates=tuple(b.row_id for b in heur),
                    drops=False,
                )
            )
        else:
            results.append(_result(ev, exp, TIER_UNMATCHED, REASON_NOT_YET, drops=False))
    return results


def _apply_batches(
    events: list[CashEvent],
    banks: list[BankTxn],
    used: set[str],
    results: list[MatchResult],
) -> list[CashEvent]:
    """One lump equals the sum of same-payer events on one source date, or stay unmatched."""
    groups: dict[tuple[str, date], list[CashEvent]] = {}
    for ev in events:
        groups.setdefault((ev.payer.strip().lower(), ev.source_date), []).append(ev)
    leftover: list[CashEvent] = []
    for (payer, source), group in groups.items():
        if len(group) < 2:
            leftover.extend(group)
            continue
        total = round(sum(ev.amount for ev in group), 2)
        window_end = source + timedelta(days=12)
        cands = [
            b for b in banks
            if b.row_id not in used
            and abs(b.amount - total) <= AMOUNT_EPS
            and source <= b.txn_date <= window_end
            and _payer_hit(payer, b.description)
        ]
        exp = expected_bank_date(source, max(ev.lead_days for ev in group))
        if len(cands) == 1:
            used.add(cands[0].row_id)
            for ev in group:
                results.append(
                    _result(
                        ev, exp, TIER_BATCH, "", actual=cands[0].txn_date,
                        banks=(cands[0].row_id,), drops=True,
                    )
                )
        elif len(cands) > 1:
            ids = tuple(b.row_id for b in cands)
            for ev in group:
                results.append(_result(ev, exp, TIER_AMBIGUOUS, "", candidates=ids, drops=False))
        else:
            leftover.extend(group)
    return leftover


def _result(
    ev: CashEvent,
    exp: date,
    tier: str,
    reason: str,
    *,
    actual: date | None = None,
    banks: tuple[str, ...] = (),
    candidates: tuple[str, ...] = (),
    drops: bool,
) -> MatchResult:
    if actual is not None and reason == REASON_DEAD:
        reason = REASON_LATE
        drops = True
        tier = TIER_EXACT
    return MatchResult(
        event_id=ev.event_id,
        tier=tier,
        reason=reason,
        amount=round(float(ev.amount), 2),
        source_date=ev.source_date,
        expected_bank_date=exp,
        actual_bank_date=actual,
        observed_at=ev.observed_at,
        bank_row_ids=banks,
        candidate_ids=candidates,
        drops_inflight=drops,
    )


def inflight_events(matches: Iterable[MatchResult], *, as_of: date) -> list[MatchResult]:
    """NOT_YET_DUE and AMBIGUOUS only, and only if the bank date is still ahead of settled."""
    settled = last_settled_bank_date(as_of)
    out: list[MatchResult] = []
    for m in matches:
        if m.drops_inflight or m.amount <= 0:
            continue
        if m.tier == TIER_HEURISTIC:
            continue
        if m.expected_bank_date <= settled:
            continue
        if m.tier == TIER_AMBIGUOUS or m.reason == REASON_NOT_YET:
            out.append(m)
    return out


def promote_late(match: MatchResult, actual: date) -> MatchResult:
    """A dead forecast cutoff that later hits the bank stays in audit as LATE_BANKED."""
    return MatchResult(
        event_id=match.event_id,
        tier=match.tier,
        reason=REASON_LATE,
        amount=match.amount,
        source_date=match.source_date,
        expected_bank_date=match.expected_bank_date,
        actual_bank_date=actual,
        observed_at=match.observed_at,
        bank_row_ids=match.bank_row_ids,
        candidate_ids=match.candidate_ids,
        drops_inflight=True,
        component=match.component,
    )
