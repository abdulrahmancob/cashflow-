"""Categorize denial CARC/RARC codes and derive RCM analytics columns."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from denials_normalize import extract_carc_codes, extract_rarc_codes, parse_money, split_pipe_codes

CARC_CATEGORY_RULES = [
    ("Benefit maximum / limit reached", ["CO-119", "N362"]),
    ("Bundling / included in another service", ["CO-97", "CO-151", "M15"]),
    ("Fee schedule / maximum allowable", ["CO-45", "CO-42"]),
    ("Frequency / units exceeded", ["PI-151", "CO-151"]),
    ("Network / provider not on file", ["PR-242", "CO-242"]),
    ("Processed in excess of charges", ["PI-94"]),
    ("Medical necessity", ["CO-50", "CO-4"]),
    ("Patient responsibility", ["PR-1", "PR-2", "PR-3"]),
    ("Duplicate claim", ["CO-18", "CO-B7"]),
    ("Timely filing", ["CO-29"]),
    ("Authorization / precertification", ["CO-197", "CO-15"]),
    ("Denied / remark required", ["OA-A1", "OA-23"]),
]

NON_APPEALABLE_CARCS = {"CO-45", "CO-97", "CO-119", "PI-94", "PR-1", "PR-2", "PR-3"}
PREVENTABLE_CARCS = {"CO-97", "CO-18", "CO-29", "CO-197", "CO-15", "M15", "OA-A1"}
PREVENTABLE_DENIAL_CATEGORIES = {
    "coding",
    "billing",
    "authorization",
    "duplicate",
    "timely filing",
}

RESOLUTION_CATEGORIES = {
    "WriteOff": "Automatic write-off",
    "Paid": "Manually paid/posted",
    "Active": "Open — not resolved",
    "Inactive": "Superseded / inactive remit",
}

LINK_CONFIDENCE = {
    "claim_id": ("high", 100),
    "claim_number+patient+service_date": ("medium", 70),
    "unmatched": ("none", 0),
}


def categorize_carc(carc_codes: str, remark_codes: str = "") -> str:
    combined = f"{carc_codes} {remark_codes}".upper()
    for name, codes in CARC_CATEGORY_RULES:
        if any(code.upper() in combined for code in codes):
            return name
    if combined.strip():
        return "Other payer-specific"
    return "Uncoded"


def categorize_resolution(action: str, status: str = "") -> str:
    key = (action or status or "").strip()
    return RESOLUTION_CATEGORIES.get(key, key or "Unknown")


def root_cause_category(carc_codes: str, remark_codes: str, denial_category: str = "") -> str:
    carc_cat = categorize_carc(carc_codes, remark_codes)
    if carc_cat != "Uncoded" and carc_cat != "Other payer-specific":
        return carc_cat
    denial_lower = (denial_category or "").lower()
    if "medical necessity" in denial_lower:
        return "Medical necessity"
    if "benefit" in denial_lower:
        return "Benefit maximum / limit reached"
    if denial_category:
        return denial_category
    return carc_cat


def is_preventable(carc_codes: str, remark_codes: str = "", denial_category: str = "") -> bool:
    carcs = {c.upper() for c in extract_carc_codes(carc_codes)}
    if carcs & PREVENTABLE_CARCS:
        return True
    denial_lower = (denial_category or "").lower()
    return any(token in denial_lower for token in PREVENTABLE_DENIAL_CATEGORIES)


def requires_appeal(stage: str, carc_codes: str = "", adjustment_status: str = "") -> bool:
    stage_lower = (stage or "").lower()
    if "appeal" in stage_lower:
        return True
    if (adjustment_status or "").strip() != "Active":
        return False
    carcs = {c.upper() for c in extract_carc_codes(carc_codes)}
    return bool(carcs - NON_APPEALABLE_CARCS)


def recommended_action(
    *,
    stage: str = "",
    carc_codes: str = "",
    adjustment_status: str = "",
    can_resubmit: bool = False,
    resolution_action: str = "",
) -> str:
    status = (adjustment_status or "").strip()
    action = (resolution_action or "").strip()
    if status == "Closed" and action == "Paid":
        return "No Action"
    if status == "Closed" and action == "WriteOff":
        return "Review Write-Off"
    if status == "Inactive":
        return "No Action"
    if requires_appeal(stage, carc_codes, status):
        return "Appeal"
    if can_resubmit:
        return "Resubmit"
    carcs = {c.upper() for c in extract_carc_codes(carc_codes)}
    if "CO-97" in carcs or "M15" in extract_rarc_codes(carc_codes):
        return "Review Coding"
    if "PR-242" in carcs:
        return "Call Payer"
    if status == "Active":
        return "Review"
    return "No Action"


def resolution_success(resolution_action: str, adjustment_status: str = "") -> str:
    action = (resolution_action or "").strip()
    status = (adjustment_status or "").strip()
    if action == "Paid":
        return "1"
    if action == "WriteOff" or status == "Closed":
        return "0"
    if status in {"Active", "Inactive"}:
        return ""
    return ""


def final_outcome(resolution_action: str, adjustment_status: str = "", remit_active: bool = True) -> str:
    status = (adjustment_status or "").strip()
    action = (resolution_action or "").strip()
    if status == "Inactive" or not remit_active:
        return "Superseded"
    if action == "Paid":
        return "Recovered"
    if action == "WriteOff" or status == "Closed":
        return "Written_Off"
    if status == "Active":
        return "Pending"
    return "Unknown"


def recoverable_amount_line(adjustment_amount: float, is_actionable: bool) -> float:
    if not is_actionable:
        return 0.0
    return abs(adjustment_amount)


def recovery_priority(recoverable_amount: float, days_since_denial: int | None, requires_appeal_flag: bool) -> int:
    score = 0.0
    score += min(recoverable_amount / 100.0, 50.0)
    if days_since_denial is not None:
        score += min(days_since_denial / 2.0, 25.0)
    if requires_appeal_flag:
        score += 25.0
    return int(round(score))


def link_confidence_score(link_method: str) -> tuple[str, int]:
    return LINK_CONFIDENCE.get(link_method, ("none", 0))


def analyze_denial_lines(rows: list[dict]) -> dict:
    carc_counts: Counter = Counter()
    remark_counts: Counter = Counter()
    proc_counts: Counter = Counter()
    payer_counts: Counter = Counter()
    category_counts: Counter = Counter()
    resolution_counts: Counter = Counter()
    category_amounts: defaultdict[str, float] = defaultdict(float)
    payer_amounts: defaultdict[str, float] = defaultdict(float)

    for row in rows:
        carcs = extract_carc_codes(row.get("carc_codes", ""))
        remarks = extract_rarc_codes(row.get("remark_codes", "")) or split_pipe_codes(
            row.get("rarc_codes_clean", "") or row.get("remark_codes", "")
        )
        for code in carcs:
            carc_counts[code] += 1
        for code in remarks:
            remark_counts[code] += 1

        proc = (row.get("proc_code") or "").strip()
        if proc:
            proc_counts[proc] += 1

        payer = (row.get("payer_name") or row.get("remit_payer_name") or row.get("payer") or "?").strip()
        payer_counts[payer] += 1

        category = row.get("carc_category") or categorize_carc(
            row.get("carc_codes", ""), row.get("remark_codes", "")
        )
        category_counts[category] += 1

        amount = abs(parse_money(row.get("recoverable_amount_line") or row.get("adjustment_amount", "")))
        category_amounts[category] += amount
        payer_amounts[payer] += amount

        resolution = row.get("resolution_category") or categorize_resolution(
            row.get("resolution_action", ""),
            row.get("adjustment_status", ""),
        )
        resolution_counts[resolution] += 1

    return {
        "line_count": len(rows),
        "carc_counts": carc_counts,
        "remark_counts": remark_counts,
        "proc_counts": proc_counts,
        "payer_counts": payer_counts,
        "category_counts": category_counts,
        "category_amounts": category_amounts,
        "payer_amounts": payer_amounts,
        "resolution_counts": resolution_counts,
    }
