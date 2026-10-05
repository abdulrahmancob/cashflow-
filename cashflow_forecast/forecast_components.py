"""One forecasted dollar belongs to exactly one component.

known_cash OR recurring_stream OR residual_capacity. Never two.
"""

from __future__ import annotations

import pandas as pd

COMPONENT_KNOWN = "known_cash"
COMPONENT_STREAM = "recurring_stream"
COMPONENT_RESIDUAL = "residual_capacity"
COMPONENTS = frozenset({COMPONENT_KNOWN, COMPONENT_STREAM, COMPONENT_RESIDUAL})

# AR covered by the NYNM biweekly stream, not by residual packing.
STREAM_AR_TOKENS = ("fidelis", "new york network", "nynm", "nynym")


def is_stream_payer(name: str) -> bool:
    text = (name or "").strip().lower()
    return any(tok in text for tok in STREAM_AR_TOKENS)


def tag_component(frame: pd.DataFrame, component: str) -> pd.DataFrame:
    if component not in COMPONENTS:
        raise ValueError(f"unknown forecast component {component}")
    out = frame.copy() if frame is not None else pd.DataFrame()
    out["component"] = component
    return out


def exclude_stream_ar(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Drop Fidelis/NYNM AR from residual packing. The stream owns those dollars."""
    if outcomes is None or outcomes.empty or "ins_name" not in outcomes.columns:
        return outcomes
    out = outcomes.copy()
    mask = out["ins_name"].fillna("").astype(str).map(is_stream_payer)
    if "expected_amount" in out.columns:
        out.loc[mask, "expected_amount"] = 0.0
    if "unscheduled" in out.columns:
        out.loc[mask, "unscheduled"] = True
    return out


def offset_known_from_ar(outcomes: pd.DataFrame, known: pd.DataFrame) -> pd.DataFrame:
    """Reduce residual AR by known-cash dollars of the same payer (proportional)."""
    if outcomes is None or outcomes.empty or known is None or known.empty:
        return outcomes
    if "ins_name" not in outcomes.columns or "expected_amount" not in outcomes.columns:
        return outcomes
    if "ins_name" not in known.columns or "expected_amount" not in known.columns:
        return outcomes
    budgets: dict[str, float] = {}
    for row in known.itertuples(index=False):
        name = str(getattr(row, "ins_name", "") or "").strip().lower()
        amt = float(getattr(row, "expected_amount", 0) or 0)
        if name and amt > 0 and not is_stream_payer(name):
            budgets[name] = budgets.get(name, 0.0) + amt
    if not budgets:
        return outcomes
    out = outcomes.copy()
    amounts = pd.to_numeric(out["expected_amount"], errors="coerce").fillna(0.0)
    for name, budget in budgets.items():
        mask = out["ins_name"].fillna("").astype(str).str.strip().str.lower().eq(name)
        idxs = list(out.index[mask & (amounts > 0)])
        left = budget
        for idx in idxs:
            if left <= 1e-9:
                break
            cur = float(amounts.at[idx])
            take = min(cur, left)
            amounts.at[idx] = round(cur - take, 2)
            left -= take
    out["expected_amount"] = amounts
    return out


def assert_exclusive(frame: pd.DataFrame, *, amount_col: str = "expected_amount") -> None:
    """Raise if a live dollar is missing a component or a line is tagged twice."""
    if frame is None or frame.empty:
        return
    if "component" not in frame.columns:
        raise AssertionError("forecast frame has no component column")
    live = frame
    if amount_col in frame.columns:
        amt = pd.to_numeric(frame[amount_col], errors="coerce").fillna(0.0)
        stages = frame["outcome_stage"] if "outcome_stage" in frame.columns else None
        live_mask = amt > 0
        if stages is not None:
            live_mask = live_mask & stages.isin(["on_track", "overdue"])
        live = frame.loc[live_mask]
    bad = ~live["component"].isin(COMPONENTS)
    if bad.any():
        raise AssertionError(f"{int(bad.sum())} forecast rows lack a single component")
    if "line_key" in live.columns:
        dup = live["line_key"].dropna().duplicated()
        if dup.any():
            raise AssertionError("forecast line appears in more than one component")


def component_totals(frame: pd.DataFrame, *, amount_col: str = "expected_amount") -> dict[str, float]:
    if frame is None or frame.empty or amount_col not in frame.columns:
        return {c: 0.0 for c in COMPONENTS}
    amt = pd.to_numeric(frame[amount_col], errors="coerce").fillna(0.0)
    out = {c: 0.0 for c in COMPONENTS}
    comp = frame.get("component")
    if comp is None:
        return out
    for name in COMPONENTS:
        out[name] = round(float(amt[comp.eq(name)].sum()), 2)
    return out


def components_cover_total(frame: pd.DataFrame, *, amount_col: str = "expected_amount") -> bool:
    totals = component_totals(frame, amount_col=amount_col)
    if frame is None or frame.empty or amount_col not in frame.columns:
        return True
    amt = pd.to_numeric(frame[amount_col], errors="coerce").fillna(0.0)
    if "outcome_stage" in frame.columns:
        amt = amt[frame["outcome_stage"].isin(["on_track", "overdue"])]
    return abs(sum(totals.values()) - float(amt.sum())) < 0.05
