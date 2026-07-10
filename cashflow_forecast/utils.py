"""Shared parse helpers for forecast loaders."""

from __future__ import annotations

import re
from datetime import date, datetime


def parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 10:
        candidates.append(text[:10])
    for candidate in candidates:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def parse_money(value: str | None) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.replace("$", "").replace(",", "").strip("() ")
    if not cleaned:
        return 0.0
    try:
        amount = float(cleaned)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def normalize_name_key(name: str) -> str:
    """Collapse patient name to uppercase alnum for matching."""
    text = (name or "").strip()
    if "," in text:
        last, first = text.split(",", 1)
        first_token = first.strip().split()[0] if first.strip() else ""
        combined = f"{last.strip()} {first_token}"
    else:
        parts = text.split()
        if len(parts) >= 2:
            combined = f"{parts[-1]} {parts[0]}"
        else:
            combined = text
    return re.sub(r"[^A-Z0-9]", "", combined.upper())


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))
