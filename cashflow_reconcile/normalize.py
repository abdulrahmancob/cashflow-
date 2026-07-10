"""Name, date, and money normalization helpers."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

WEBPT_NAME_RE = re.compile(r"^\s*([^,]+),\s*(.+?)\s*$")
MONEY_RE = re.compile(r"^\(?\$?\s*([\d,]+(?:\.\d{2})?)\s*\)?$")


def parse_webpt_name(name: str) -> tuple[str, str]:
    text = (name or "").strip()
    match = WEBPT_NAME_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[1], parts[0]
    return text, ""


def normalize_name_key(last: str, first: str) -> str:
    """Collapse to uppercase alnum for cross-system matching."""
    first_token = (first or "").split()[0] if first else ""
    combined = f"{last} {first_token}".upper()
    return re.sub(r"[^A-Z0-9]", "", combined)


def name_key_from_webpt(patient_name: str) -> str:
    last, first = parse_webpt_name(patient_name)
    return normalize_name_key(last, first)


def name_key_from_revflow(last: str, first: str) -> str:
    return normalize_name_key(last or "", first or "")


def parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_date(value: date | None) -> str:
    return value.isoformat() if value else ""


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


def format_money(value: float) -> str:
    return f"{value:.2f}"


def safe_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text or text.startswith("$") or text.startswith("("):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def join_carcs(carcs: list[str]) -> str:
    seen: list[str] = []
    for code in carcs:
        code = (code or "").strip()
        if code and code not in seen:
            seen.append(code)
    return "; ".join(seen)


def split_carcs(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def pick_first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
