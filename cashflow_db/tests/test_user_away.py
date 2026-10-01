"""Away-session rules (no live DB)."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cashflow_db.config import SQL_DIR
from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import user_away
from cashflow_db.repository.user_away import (
    BREAK_BUDGET_SECONDS,
    KIND_BREAK,
    KIND_MEETING,
    KIND_PRAYER,
    ONLINE_SECONDS,
    PRAYER_LIMIT,
    SS_BOARD_ROLES,
    aggregate_away_days,
    assemble_board,
    board_scope_roles,
    online_user_ids,
    offline_since_at,
    summarize_day,
    users_in_board_scope,
    validate_start,
    warning_level,
)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=timezone.utc)


def test_migration_registers_one_open_session():
    assert "061_user_away.sql" in MIGRATIONS
    assert MIGRATIONS.index("061_user_away.sql") > MIGRATIONS.index(
        "060_collection_queue_member.sql"
    )
    body = (Path(SQL_DIR) / "061_user_away.sql").read_text(encoding="utf-8")
    assert "ux_user_away_open" in body
    assert "ended_at IS NULL" in body
    assert "ops.user_away" in body


def test_break_pool_and_warning_thresholds():
    closed = 40 * 60
    assert (
        warning_level(
            open_kind=KIND_BREAK,
            open_elapsed=0,
            open_planned=None,
            break_seconds_closed=closed,
        )
        == "soon"
    )
    assert (
        warning_level(
            open_kind=KIND_BREAK,
            open_elapsed=5 * 60,
            open_planned=None,
            break_seconds_closed=closed,
        )
        == "over"
    )
    assert (
        warning_level(
            open_kind=None,
            open_elapsed=0,
            open_planned=None,
            break_seconds_closed=BREAK_BUDGET_SECONDS,
        )
        == "none"
    )


def test_prayer_is_ten_minutes_and_fourth_is_rejected():
    with pytest.raises(ValueError, match="3 prayers"):
        validate_start(
            kind=KIND_PRAYER,
            planned_minutes=10,
            with_whom=None,
            has_open=False,
            prayer_count=PRAYER_LIMIT,
        )
    for minutes in (None, 5, 11):
        ok = validate_start(
            kind=KIND_PRAYER,
            planned_minutes=minutes,
            with_whom=None,
            has_open=False,
            prayer_count=2,
        )
        assert ok["planned_seconds"] == 600


def test_meeting_requires_duration_and_who():
    with pytest.raises(ValueError, match="duration"):
        validate_start(
            kind=KIND_MEETING,
            planned_minutes=None,
            with_whom="Sara",
            has_open=False,
            prayer_count=0,
        )
    with pytest.raises(ValueError, match="who"):
        validate_start(
            kind=KIND_MEETING,
            planned_minutes=30,
            with_whom="  ",
            has_open=False,
            prayer_count=0,
        )
    ok = validate_start(
        kind=KIND_MEETING,
        planned_minutes=30,
        with_whom="  Sara Ali  ",
        has_open=False,
        prayer_count=0,
    )
    assert ok["with_whom"] == "Sara Ali"
    assert ok["planned_seconds"] == 1800


def test_second_session_is_rejected_while_one_is_open():
    with pytest.raises(ValueError, match="current break"):
        validate_start(
            kind=KIND_BREAK,
            planned_minutes=None,
            with_whom=None,
            has_open=True,
            prayer_count=0,
        )


def test_break_minutes_sum_closed_sessions_and_the_open_one():
    rows = [
        {
            "away_id": "a",
            "kind": KIND_BREAK,
            "started_at": _at(8, 0),
            "ended_at": _at(8, 20),
            "planned_seconds": None,
            "with_whom": None,
            "work_day": _at(8).date(),
        },
        {
            "away_id": "b",
            "kind": KIND_BREAK,
            "started_at": _at(10, 0),
            "ended_at": None,
            "planned_seconds": None,
            "with_whom": None,
            "work_day": _at(8).date(),
        },
        {
            "away_id": "c",
            "kind": KIND_PRAYER,
            "started_at": _at(9, 0),
            "ended_at": _at(9, 8),
            "planned_seconds": 600,
            "with_whom": None,
            "work_day": _at(8).date(),
        },
    ]
    summary = summarize_day(rows, now=_at(10, 10))
    assert summary["break_seconds"] == 30 * 60
    assert summary["prayer_count"] == 1
    assert summary["warning"] == "none"
    assert summary["open"]["kind"] == KIND_BREAK


def test_away_days_count_distinct_people_newest_first():
    rows = [
        {"user_id": "a", "work_day": date(2026, 9, 24)},
        {"user_id": "b", "work_day": date(2026, 9, 24)},
        {"user_id": "a", "work_day": date(2026, 9, 24)},
        {"user_id": "a", "work_day": date(2026, 9, 25)},
    ]
    assert aggregate_away_days(rows) == [
        {"work_day": "2026-09-25", "people": 1},
        {"work_day": "2026-09-24", "people": 2},
    ]


def test_past_day_excludes_an_open_session_from_another_day():
    past = date(2026, 9, 25)
    today = date(2026, 9, 26)
    users = [
        {
            "user_id": "u1",
            "display_name": "Nour",
            "username": "nour",
            "roles": ["collector"],
        }
    ]
    past_break = {
        "away_id": "past",
        "user_id": "u1",
        "kind": KIND_BREAK,
        "started_at": datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 9, 25, 8, 20, tzinfo=timezone.utc),
        "planned_seconds": None,
        "with_whom": None,
        "work_day": past,
    }
    open_today = {
        "away_id": "open",
        "user_id": "u1",
        "kind": KIND_MEETING,
        "started_at": datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc),
        "ended_at": None,
        "planned_seconds": 1800,
        "with_whom": "Sara",
        "work_day": today,
    }
    board = assemble_board(
        users,
        day_sessions=[past_break, open_today],
        open_sessions=[open_today],
        day_counts=[
            {"work_day": today, "people": 1},
            {"work_day": past, "people": 1},
        ],
        selected=past,
        today=today,
        now=datetime(2026, 9, 26, 9, 10, tzinfo=timezone.utc),
    )
    person = board["people"][0]
    assert board["is_today"] is False
    assert board["work_day"] == "2026-09-25"
    assert person["open"] is None
    assert person["status"] == "working"
    assert person["break_seconds"] == 20 * 60
    assert person["meetings"] == []
    assert board["live"] == [
        {
            "user_id": "u1",
            "display_name": "Nour",
            "kind": KIND_MEETING,
            "elapsed_seconds": 10 * 60,
            "with_whom": "Sara",
            "started_at": "2026-09-26T09:00:00+00:00",
        }
    ]
    assert board["days"][0]["work_day"] == "2026-09-26"
    src = inspect.getsource(user_away.away_board)
    assert "OR ended_at IS NULL" not in src
    assert "ended_at IS NULL" in src
    assert "GROUP BY work_day" in src
    assert "role_keys" in src
    assert "_id_clause" in src
    assert "user_id = ANY" in inspect.getsource(user_away._id_clause)


def test_board_scope_is_team_for_lead_and_open_for_admin():
    assert board_scope_roles(["second_submission_lead"]) == SS_BOARD_ROLES
    assert board_scope_roles(["ops_admin", "second_submission_lead"]) is None
    assert board_scope_roles(["super_admin"]) is None
    assert board_scope_roles(["sub_admin"]) is None
    with pytest.raises(PermissionError):
        board_scope_roles(["second_submission"])
    users = [
        {"user_id": "ss", "is_active": True, "roles": ["second_submission"]},
        {"user_id": "lead", "is_active": True, "roles": ["second_submission_lead"]},
        {"user_id": "collector", "is_active": True, "roles": ["collector"]},
        {"user_id": "inactive", "is_active": False, "roles": ["second_submission"]},
        {"user_id": "ops", "is_active": True, "roles": ["ops_admin"]},
        {"user_id": "sub", "is_active": True, "roles": ["sub_admin", "second_submission_lead"]},
        {"user_id": "super", "is_active": True, "roles": ["super_admin"]},
    ]
    scoped = [row["user_id"] for row in users_in_board_scope(users, SS_BOARD_ROLES)]
    assert scoped == ["ss", "lead"]
    everyone = [row["user_id"] for row in users_in_board_scope(users, None)]
    assert everyone == ["ss", "lead", "collector"]


def test_working_requires_a_recent_portal_ping():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(seconds=20)
    still = now - timedelta(seconds=100)
    stale = now - timedelta(seconds=ONLINE_SECONDS + 5)
    online = online_user_ids(
        [
            {"user_id": "fresh", "last_ping_at": fresh},
            {"user_id": "still", "last_ping_at": still},
            {"user_id": "stale", "last_ping_at": stale},
        ],
        now,
    )
    assert online == {"fresh", "still"}
    assert ONLINE_SECONDS == 150
    today = date(2026, 9, 26)
    users = [
        {"user_id": "fresh", "display_name": "Fresh", "username": "fresh", "roles": []},
        {"user_id": "stale", "display_name": "Stale", "username": "stale", "roles": []},
        {"user_id": "away", "display_name": "Away", "username": "away", "roles": []},
    ]
    open_break = {
        "away_id": "open",
        "user_id": "away",
        "kind": KIND_BREAK,
        "started_at": now - timedelta(minutes=2),
        "ended_at": None,
        "planned_seconds": None,
        "with_whom": None,
        "work_day": today,
    }
    board = assemble_board(
        users,
        day_sessions=[open_break],
        open_sessions=[open_break],
        day_counts=[],
        selected=today,
        today=today,
        now=now,
        online_ids=online | {"away"},
    )
    by_id = {person["user_id"]: person for person in board["people"]}
    assert by_id["fresh"]["online"] is True
    assert by_id["stale"]["online"] is False
    assert [person["user_id"] for person in board["people"]] == ["away", "fresh", "stale"]
    working = [
        person["user_id"]
        for person in board["people"]
        if person["online"] and person["user_id"] not in {item["user_id"] for item in board["live"]}
    ]
    assert working == ["fresh"]


def test_offline_clock_starts_after_today_ping_until_they_return():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    today = date(2026, 9, 26)
    left = now - timedelta(minutes=18)
    yesterday = now - timedelta(days=1)
    assert offline_since_at(left, now=now, online=False, away=False, is_today=True) == left.isoformat()
    assert offline_since_at(left, now=now, online=True, away=False, is_today=True) is None
    assert offline_since_at(left, now=now, online=False, away=True, is_today=True) is None
    assert offline_since_at(yesterday, now=now, online=False, away=False, is_today=True) is None
    assert offline_since_at(left, now=now, online=False, away=False, is_today=False) is None
    users = [
        {"user_id": "gone", "display_name": "Gone", "username": "gone", "roles": []},
        {"user_id": "away", "display_name": "Away", "username": "away", "roles": []},
    ]
    open_break = {
        "away_id": "open",
        "user_id": "away",
        "kind": KIND_BREAK,
        "started_at": now - timedelta(minutes=2),
        "ended_at": None,
        "planned_seconds": None,
        "with_whom": None,
        "work_day": today,
    }
    board = assemble_board(
        users,
        day_sessions=[open_break],
        open_sessions=[open_break],
        day_counts=[],
        selected=today,
        today=today,
        now=now,
        online_ids=set(),
        last_pings={"gone": left, "away": left},
    )
    by_id = {person["user_id"]: person for person in board["people"]}
    assert by_id["gone"]["offline_since"] == left.isoformat()
    assert by_id["away"]["offline_since"] is None


def test_board_login_is_the_first_login_of_the_selected_day():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    today = date(2026, 9, 26)
    first = datetime(2026, 9, 26, 6, 14, tzinfo=timezone.utc)
    users = [
        {"user_id": "in", "display_name": "In", "username": "in", "roles": [], "desk_permission": "denied"},
        {"user_id": "out", "display_name": "Out", "username": "out", "roles": [], "desk_permission": "watching"},
    ]
    board = assemble_board(
        users,
        day_sessions=[],
        open_sessions=[],
        day_counts=[],
        selected=today,
        today=today,
        now=now,
        logins=[{"user_id": "in", "logged_in_at": first}],
    )
    by_id = {person["user_id"]: person for person in board["people"]}
    assert by_id["in"]["logged_in_at"] == first.isoformat()
    assert by_id["out"]["logged_in_at"] is None
    assert by_id["in"]["desk_permission"] == "denied"
    assert by_id["out"]["desk_permission"] == "watching"
    src = inspect.getsource(user_away.away_board)
    assert "auth.login_event" in src
    assert "min(logged_in_at)" in src
