"""Away-board Excel rows: every Cairo day, and each person rolled up by month."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

from cashflow_db.repository import user_away
from cashflow_db.repository.user_away import (
    CAIRO_TZ,
    assemble_board_export,
    export_tables,
)

JANE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _user(user_id: str, name: str) -> dict:
    return {
        "user_id": user_id,
        "display_name": name,
        "username": name.lower(),
        "roles": ["second_submission"],
        "is_active": True,
    }


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CAIRO_TZ)


def _session(
    user_id: str,
    kind: str,
    start: datetime,
    end: datetime | None,
    *,
    planned: int | None = None,
    whom: str | None = None,
) -> dict:
    return {
        "away_id": f"{kind}-{start.isoformat()}",
        "user_id": user_id,
        "kind": kind,
        "started_at": start,
        "ended_at": end,
        "planned_seconds": planned,
        "with_whom": whom,
        "work_day": start.astimezone(CAIRO_TZ).date(),
    }


def test_export_reads_the_same_scope_as_the_board():
    src = inspect.getsource(user_away.away_board_export)
    assert "users_in_board_scope" in src
    assert "ops.user_away" in src
    assert "ops.user_activity_slice" in src
    assert "auth.login_event" in src
    assert "ops.portal_activity" in src


def test_month_rollup_runs_from_the_first_through_the_last_day():
    now = _at(date(2026, 10, 8), 15)
    oct1 = date(2026, 10, 1)
    oct2 = date(2026, 10, 2)
    sep15 = date(2026, 9, 15)
    boundary = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)
    local = boundary.astimezone(CAIRO_TZ)
    later = _at(oct1, 17, 2)

    payload = assemble_board_export(
        [_user(JANE, "Jane"), _user(OTHER, "Other")],
        sessions=[
            _session(JANE, "break", _at(oct1, 9), _at(oct1, 9, 50)),
            _session(JANE, "prayer", _at(oct1, 10), _at(oct1, 10, 10), planned=600),
            _session(
                JANE,
                "meeting",
                _at(oct2, 11),
                _at(oct2, 11, 30),
                planned=1800,
                whom="Mona",
            ),
            _session(OTHER, "break", _at(oct1, 8), _at(oct1, 8, 10)),
        ],
        slices=[
            {
                "user_id": JANE,
                "started_at": _at(oct1, 8),
                "last_ping_at": _at(oct1, 12),
                "seconds_desk": 3600,
                "seconds_idle": 600,
                "seconds_active": 3000,
                "page_path": "/eligibility",
            },
            {
                "user_id": JANE,
                "started_at": _at(oct2, 10),
                "last_ping_at": _at(oct2, 11),
                "seconds_desk": 1800,
                "seconds_idle": 0,
                "seconds_active": 1800,
                "page_path": "/collection",
            },
            {
                "user_id": JANE,
                "started_at": _at(sep15, 9),
                "last_ping_at": _at(sep15, 11),
                "seconds_desk": 7200,
                "seconds_idle": 0,
                "seconds_active": 7200,
                "page_path": "/tracker",
            },
            {
                "user_id": JANE,
                "started_at": _at(oct1, 13),
                "last_ping_at": _at(oct1, 13),
                "seconds_desk": 0,
                "seconds_idle": 0,
                "seconds_active": 0,
                "page_path": "/away",
            },
            {
                "user_id": "33333333-3333-3333-3333-333333333333",
                "started_at": _at(oct1, 8),
                "last_ping_at": _at(oct1, 9),
                "seconds_desk": 9999,
                "seconds_idle": 0,
                "seconds_active": 9999,
                "page_path": "/users",
            },
        ],
        logins=[
            {"user_id": JANE, "logged_in_at": boundary},
            {"user_id": JANE, "logged_in_at": later},
            {"user_id": JANE, "logged_in_at": _at(oct2, 10, 5)},
            {"user_id": JANE, "logged_in_at": _at(sep15, 9)},
        ],
        changes=[
            {
                "user_id": JANE,
                "occurred_at": _at(oct1, 11, 30),
                "action": "updated",
                "area": "eligibility",
                "entity_label": "Visit 10",
                "summary": "Changed status",
            }
        ],
        now=now,
    )

    jane_days = {row["date"]: row for row in payload["days"] if row["user_id"] == JANE}
    assert local.date().isoformat() == "2026-10-01"
    assert local.date() != boundary.date()
    october_first = jane_days["2026-10-01"]
    assert october_first["first_login"] == local.strftime("%H:%M")
    assert october_first["last_login"] == "17:02"
    assert october_first["logins"] == 2
    assert october_first["hours_desk"] == 1
    assert october_first["hours_idle"] == 0.17
    assert october_first["hours_portal"] == 0.83
    assert october_first["break_minutes"] == 50
    assert october_first["break_budget_minutes"] == 45
    assert october_first["over_break"] == "Yes"
    assert october_first["prayers"] == 1
    assert october_first["pages"] == "Eligibility 1.00h"
    assert jane_days["2026-10-02"]["hours_desk"] == 0.5
    assert jane_days["2026-10-02"]["over_break"] == "No"
    assert jane_days["2026-10-02"]["meetings"] == 1
    assert jane_days["2026-10-02"]["meeting_minutes"] == 30
    assert "33333333-3333-3333-3333-333333333333" not in {
        row["user_id"] for row in payload["days"]
    }

    meeting = next(row for row in payload["sessions"] if row["kind"] == "Meeting")
    assert meeting["started"] == "2026-10-02 11:00"
    assert meeting["ended"] == "2026-10-02 11:30"
    assert meeting["minutes"] == 30
    assert meeting["planned_minutes"] == 30
    assert meeting["with_whom"] == "Mona"
    assert meeting["still_open"] == "No"

    change = payload["changes"][0]
    assert change["date"] == "2026-10-01"
    assert change["time"] == "11:30"
    assert change["area"] == "Eligibility"
    assert change["action"] == "Updated"
    assert change["item"] == "Visit 10"

    jane_months = {row["month"]: row for row in payload["monthly"] if row["user_id"] == JANE}
    october = jane_months["2026-10"]
    assert october["period_from"] == "2026-10-01"
    assert october["period_to"] == "2026-10-08"
    assert october["days_in_period"] == 8
    assert october["days_present"] == 2
    assert october["days_absent"] == 6
    assert october["hours_desk"] == 1.5
    assert october["hours_idle"] == 0.17
    assert october["hours_portal"] == 1.33
    assert october["hours_desk_average"] == 0.75
    assert october["break_minutes"] == 50
    assert october["days_over_break"] == 1
    assert october["prayers"] == 1
    assert october["meetings"] == 1
    assert october["meeting_minutes"] == 30
    assert october["top_page"] == "Eligibility"
    assert october["top_page_hours"] == 1

    september = jane_months["2026-09"]
    assert september["period_from"] == "2026-09-01"
    assert september["period_to"] == "2026-09-30"
    assert september["days_in_period"] == 30
    assert september["days_present"] == 1
    assert september["days_absent"] == 29
    assert september["hours_desk"] == 2
    assert september["top_page"] == "Tracker"
    assert september["top_page_hours"] == 2
    assert [row["month"] for row in payload["monthly"] if row["user_id"] == JANE] == [
        "2026-10",
        "2026-09",
    ]

    tables = export_tables(payload)
    assert [title for title, _, _ in tables] == [
        "Days",
        "Sessions",
        "Work",
        "Changes",
        "Monthly",
    ]
    monthly_headers = tables[4][1]
    assert "Days absent" in monthly_headers
    assert "Top page" in monthly_headers


def test_open_session_keeps_running_until_the_export_clock():
    now = _at(date(2026, 10, 8), 15)
    started = now - timedelta(minutes=20)
    payload = assemble_board_export(
        [_user(JANE, "Jane")],
        sessions=[_session(JANE, "break", started, None)],
        slices=[],
        logins=[],
        changes=[],
        now=now,
    )
    day = payload["days"][0]
    session = payload["sessions"][0]
    assert day["date"] == "2026-10-08"
    assert day["break_minutes"] == 20
    assert day["over_break"] == "No"
    assert session["started"] == "2026-10-08 14:40"
    assert session["ended"] == ""
    assert session["still_open"] == "Yes"
    assert session["minutes"] == 20
