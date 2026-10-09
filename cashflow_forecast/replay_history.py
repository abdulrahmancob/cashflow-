"""Replay stored forecasts from 1 Aug 2026 through 5 Oct 2026.

A replay run is point-in-time (backtest gates) and is written with
``created_at`` on that day so it cannot become the live forecast.
The eligibility sheet is only read.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
REPLAY_START = date(2026, 8, 1)
REPLAY_END = date(2026, 10, 5)
CLOSE_PCT = 0.02
CLOSE_FLOOR = 25_000.0


def forecasts_close(new_open_ar: float, old_open_ar: float) -> bool:
    """True when the new book is close enough to keep the stored run.

    abs(new - old) <= max(old * 0.02, 25000)
    """
    return abs(new_open_ar - old_open_ar) <= max(old_open_ar * CLOSE_PCT, CLOSE_FLOOR)


def close_tolerance(old_open_ar: float) -> float:
    return max(old_open_ar * CLOSE_PCT, CLOSE_FLOOR)


def replay_created_at(as_of: date) -> datetime:
    """23:00 UTC on the as-of day, older than the live October runs."""
    return datetime(as_of.year, as_of.month, as_of.day, 23, 0, tzinfo=timezone.utc)


def open_ar_from_stage_rows(rows: list[dict[str, Any]] | None) -> float:
    total = 0.0
    for row in rows or []:
        stage = str(row.get("outcome_stage") or "")
        if stage in {"on_track", "overdue"}:
            total += float(row.get("amount") or 0)
    return round(total, 2)


def iter_replay_days(
    start: date = REPLAY_START,
    end: date = REPLAY_END,
):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def cairo_pause_seconds(now: datetime) -> float:
    """Seconds to wait so a replay day does not start during the nightly window."""
    local = now.astimezone(CAIRO)
    start = local.replace(hour=0, minute=30, second=0, microsecond=0)
    end = local.replace(hour=5, minute=0, second=0, microsecond=0)
    if start <= local < end:
        return (end - local).total_seconds()
    return 0.0


def load_done_dates(text: str) -> set[str]:
    done: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("decision") in {"keep-old", "replace", "insert"} and row.get("as_of"):
            done.add(str(row["as_of"])[:10])
    return done
