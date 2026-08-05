"""Shared parsing helpers for ETL loaders."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def normalize_name_key(name: str | None) -> str:
    """Normalize 'Last, First Middle' → LASTFIRST (letters only, upper)."""
    if not name:
        return ""
    text = str(name).strip()
    if "," in text:
        last, _, rest = text.partition(",")
        first = rest.strip().split()[0] if rest.strip() else ""
    else:
        parts = text.split()
        if len(parts) >= 2:
            last, first = parts[-1], parts[0]
        else:
            last, first = text, ""
    return re.sub(r"[^A-Z]", "", (last + first).upper())


# Matches numeric(14, 2): abs value must be < 10^12
_MONEY_ABS_MAX = Decimal("999999999999.99")


def parse_money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float)):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        if not text or text.upper() in {"#N/A", "N/A", "NA", "-"}:
            return None
        neg = text.startswith("(") and text.endswith(")")
        cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
        if not cleaned or cleaned in {".", "-"}:
            return None
        try:
            amount = Decimal(cleaned)
        except InvalidOperation:
            return None
        if neg:
            amount = -amount
    if abs(amount) > _MONEY_ABS_MAX:
        return None
    return amount


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:32], fmt).date()
        except ValueError:
            continue
    # ISO datetime prefix
    if "T" in text or " " in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse schedule appointment timestamps (naive local wall time)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    for fmt, size in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%dT%H:%M", 16),
        ("%m/%d/%Y %H:%M:%S", 19),
        ("%m/%d/%Y %I:%M %p", 19),
    ):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def parse_ampm_on_date(service_date: date | None, time_text: Any) -> datetime | None:
    """Combine a calendar date with WebPT '9:43 am' check-in/out strings."""
    if service_date is None or time_text is None or time_text == "":
        return None
    text = str(time_text).strip()
    if not text:
        return None
    day = service_date.isoformat()
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{day} {text}", fmt)
        except ValueError:
            continue
    return None


def split_multi(value: str | None, sep: str = ";") -> list[str]:
    if not value:
        return []
    return [p.strip() for p in str(value).split(sep) if p.strip()]


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
