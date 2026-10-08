"""Presence streams: explicit states, silence as no signal, closing tabs, stale breaks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from cashflow_db.config import SQL_DIR
from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import client as db_client
from cashflow_db.repository import presence as pr
from cashflow_db.repository import user_away as ua

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
USER = "11111111-1111-1111-1111-111111111111"


class FakeDb:
    """Routes the repository's SQL to in-memory rows and records every write."""

    def __init__(self, *, presence=None, permission=None, away=None, slice_row=None, open_rows=None):
        self.presence = presence or {}
        self.permission = permission
        self.away = away
        self.slice_row = slice_row
        self.open_rows = open_rows or []
        self.writes: list[tuple[str, tuple]] = []

    def fetchone(self, conn, sql, params=()):
        if "FROM auth.app_user u" in sql:
            return {"desk_permission": self.permission, **self.presence}
        if "FROM ops.user_away" in sql:
            return {"kind": self.away} if self.away else None
        if "FROM ops.user_activity_slice" in sql:
            return self.slice_row
        raise AssertionError(sql)

    def fetchall(self, conn, sql, params=()):
        if "FROM ops.user_away" in sql:
            return self.open_rows
        raise AssertionError(sql)

    def execute(self, conn, sql, params=()):
        self.writes.append((sql, params))

    def sql(self, needle):
        return [params for sql, params in self.writes if needle in sql]


def _install(monkeypatch, fake):
    monkeypatch.setattr(db_client, "fetchone", fake.fetchone)
    monkeypatch.setattr(db_client, "fetchall", fake.fetchall)
    monkeypatch.setattr(db_client, "execute", fake.execute)


def _saved(fake):
    return fake.sql("INSERT INTO ops.user_presence")[-1]


def test_migration_is_registered_and_widens_permission_values():
    assert "078_user_presence.sql" in MIGRATIONS
    assert MIGRATIONS.index("078_user_presence.sql") > MIGRATIONS.index("070_desk_permission.sql")
    body = (SQL_DIR / "078_user_presence.sql").read_text(encoding="utf-8")
    for needle in ("ops.user_presence", "seconds_unverified", "auto_closed", "presence_ping_log"):
        assert needle in body
    permission = (SQL_DIR / "070_desk_permission.sql").read_text(encoding="utf-8")
    assert "granted_not_watching" in permission and "'error'" in permission


def test_legacy_bodies_map_to_explicit_states():
    assert pr.normalize_ping(source="tab", state=None, visible=None)["state"] == "active"
    assert pr.normalize_ping(source="tab", state=None, visible=None)["visible"] is True
    hidden = pr.normalize_ping(source="tab", state=None, visible=None, presence=True)
    assert hidden["visible"] is False
    assert pr.normalize_ping(source="tab", state=None, visible=None, closed=True)["closed"]
    assert pr.normalize_ping(source="tab", state="paused", visible=True)["state"] == "unknown"
    assert pr.normalize_ping(source="extension", state="paused", visible=None)["state"] == "paused"


def test_silence_over_the_cap_books_nothing():
    assert pr.stream_gap(NOW - timedelta(seconds=30), NOW) == 30
    assert pr.stream_gap(NOW - timedelta(seconds=120), NOW) == 120
    assert pr.stream_gap(NOW - timedelta(seconds=121), NOW) == 0
    assert pr.stream_gap(NOW + timedelta(seconds=5), NOW) == 0
    assert pr.stream_gap(None, NOW) == 0


def test_booking_table():
    def book(source, state, *, visible=True, away=False, ext=False, gap=30):
        return pr.book_ping(
            source=source, state=state, visible=visible, gap=gap, away=away, extension_live=ext
        )

    assert book("tab", "active") == {"desk": 30, "idle": 0, "unverified": 0, "portal": 30}
    assert book("tab", "active", visible=False) == {"desk": 30, "idle": 0, "unverified": 0, "portal": 0}
    assert book("tab", "idle", visible=False)["idle"] == 30
    assert book("tab", "locked", visible=False)["idle"] == 30
    assert book("tab", "unknown", visible=False) == {"desk": 0, "idle": 0, "unverified": 30, "portal": 0}
    assert book("tab", "active", ext=True) == {"desk": 0, "idle": 0, "unverified": 0, "portal": 30}
    assert book("tab", "unknown", visible=False, ext=True) == {
        "desk": 0, "idle": 0, "unverified": 0, "portal": 0
    }
    assert book("extension", "active", visible=False)["desk"] == 30
    assert book("extension", "locked", visible=False)["idle"] == 30
    assert sum(book("extension", "paused", visible=False).values()) == 0
    assert sum(book("tab", "active", away=True).values()) == 0
    assert sum(book("tab", "active", gap=0).values()) == 0


def test_extension_owns_desk_only_while_live_and_unpaused():
    live = {"ext_last_at": NOW - timedelta(seconds=40), "ext_state": "active"}
    assert pr.extension_drives_desk(live, NOW)
    assert not pr.extension_drives_desk({**live, "ext_state": "paused"}, NOW)
    assert not pr.extension_drives_desk({**live, "ext_last_at": NOW - timedelta(minutes=5)}, NOW)
    assert not pr.extension_drives_desk(None, NOW)


def test_live_status_prefers_a_live_extension():
    tab = {"tab_state": "unknown", "tab_last_at": NOW - timedelta(seconds=20)}
    assert pr.live_status(tab, NOW)["status"] == "unverified"
    both = {**tab, "ext_state": "locked", "ext_last_at": NOW - timedelta(seconds=10)}
    status = pr.live_status(both, NOW)
    assert status["status"] == "locked" and status["source"] == "extension"
    stale = {"tab_state": "active", "tab_last_at": NOW - timedelta(minutes=10)}
    off = pr.live_status(stale, NOW)
    assert off["status"] == "offline" and off["online"] is False
    assert off["since"] == (NOW - timedelta(minutes=10)).isoformat()
    closed = {
        "tab_state": "closed",
        "tab_last_at": NOW - timedelta(seconds=20),
        "tab_closed_at": NOW - timedelta(seconds=20),
    }
    assert pr.live_status(closed, NOW)["status"] == "signed_out"
    assert pr.live_status(None, NOW)["status"] == "offline"


def test_tab_ping_books_the_stream_gap_and_saves_presence(monkeypatch):
    fake = FakeDb(
        presence={"tab_state": "active", "tab_last_at": NOW - timedelta(seconds=30),
                  "tab_state_since": NOW - timedelta(minutes=5)},
        permission="prompt",
        slice_row={"slice_id": "s1", "last_ping_at": NOW - timedelta(seconds=30),
                   "page_path": "/eligibility"},
    )
    _install(monkeypatch, fake)
    out = pr.record_ping(
        object(), USER, state="active", visible=True, tab_id="t1",
        page_path="/eligibility", desk_permission="watching", now=NOW,
    )
    assert out["action"] == "extend"
    assert out["add_desk_seconds"] == 30 and out["add_seconds"] == 30
    assert out["idle_grace_seconds"] == pr.IDLE_GRACE_SECONDS
    assert fake.sql("pg_advisory_xact_lock")
    assert fake.sql("SET desk_permission") == [("watching", USER)]
    update = fake.sql("UPDATE ops.user_activity_slice")[0]
    assert update[1:5] == (30, 30, 0, 0)
    saved = _saved(fake)
    assert saved[1] == "active" and saved[2] == NOW - timedelta(minutes=5)
    assert saved[3] == NOW and saved[4] == "/eligibility"
    assert "t1" in saved[5].obj


def test_unchanged_permission_is_not_rewritten(monkeypatch):
    fake = FakeDb(permission="watching")
    _install(monkeypatch, fake)
    pr.record_ping(object(), USER, state="active", visible=True, desk_permission="watching", now=NOW)
    assert not fake.sql("SET desk_permission")
    pr.record_ping(object(), USER, state="active", visible=True, desk_permission="camera", now=NOW)
    assert not fake.sql("SET desk_permission")


def test_return_after_long_silence_books_no_idle(monkeypatch):
    fake = FakeDb(
        presence={"tab_state": "active", "tab_last_at": NOW - timedelta(minutes=40)},
        slice_row={"slice_id": "s1", "last_ping_at": NOW - timedelta(minutes=40), "page_path": None},
    )
    _install(monkeypatch, fake)
    out = pr.record_ping(object(), USER, state="active", visible=True, page_path="/eligibility", now=NOW)
    assert out["action"] == "open"
    assert out["add_idle_seconds"] == 0 and out["add_desk_seconds"] == 0
    insert = fake.sql("INSERT INTO ops.user_activity_slice")[0]
    assert insert[4:8] == (0, 0, 0, 0)


def test_page_change_opens_a_new_slice(monkeypatch):
    fake = FakeDb(
        presence={"tab_state": "active", "tab_last_at": NOW - timedelta(seconds=30)},
        slice_row={"slice_id": "s1", "last_ping_at": NOW - timedelta(seconds=30),
                   "page_path": "/eligibility"},
    )
    _install(monkeypatch, fake)
    out = pr.record_ping(object(), USER, state="active", visible=True, page_path="/collection", now=NOW)
    assert out["action"] == "open"
    assert fake.sql("INSERT INTO ops.user_activity_slice")[0][-1] == "/collection"


def test_away_ping_keeps_the_person_present_without_booking(monkeypatch):
    fake = FakeDb(
        presence={"tab_state": "active", "tab_last_at": NOW - timedelta(seconds=30)},
        away="break",
    )
    _install(monkeypatch, fake)
    out = pr.record_ping(object(), USER, state="active", visible=True, now=NOW)
    assert out["away"] is True and out["action"] == "skip"
    assert not fake.sql("ops.user_activity_slice")
    assert _saved(fake)[3] == NOW
    assert fake.sql("INSERT INTO ops.presence_ping_log")[0][3] == "away"


def test_closing_one_of_two_tabs_keeps_the_person_present(monkeypatch):
    tabs = {
        "t1": (NOW - timedelta(seconds=10)).isoformat(),
        "t2": (NOW - timedelta(seconds=20)).isoformat(),
    }
    fake = FakeDb(presence={"tab_state": "active", "tab_last_at": NOW, "tab_open": tabs})
    _install(monkeypatch, fake)
    out = pr.record_ping(object(), USER, closed=True, tab_id="t1", now=NOW)
    assert out["action"] == "close"
    saved = _saved(fake)
    assert saved[1] == "active" and list(saved[5].obj) == ["t2"]

    fake = FakeDb(presence={"tab_state": "active", "tab_last_at": NOW,
                            "tab_open": {"t2": tabs["t2"]}})
    _install(monkeypatch, fake)
    pr.record_ping(object(), USER, closed=True, tab_id="t2", now=NOW)
    saved = _saved(fake)
    assert saved[1] == "closed" and saved[6] == NOW
    assert not fake.sql("ops.user_activity_slice")


def test_extension_ping_updates_its_own_stream(monkeypatch):
    fake = FakeDb(
        presence={"ext_state": "active", "ext_last_at": NOW - timedelta(seconds=30),
                  "tab_state": "unknown", "tab_last_at": NOW - timedelta(seconds=5)},
        slice_row={"slice_id": "s1", "last_ping_at": NOW - timedelta(seconds=5), "page_path": None},
    )
    _install(monkeypatch, fake)
    out = pr.record_ping(object(), USER, source="extension", state="locked", version="1.0.0", now=NOW)
    assert out["add_idle_seconds"] == 30
    saved = _saved(fake)
    assert saved[8] == "locked" and saved[9] == NOW and saved[10] == NOW and saved[11] == "1.0.0"
    assert saved[1] == "unknown"


def test_stale_caps():
    day = date(2026, 10, 9)
    start = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    brk = {"kind": "break", "started_at": start, "work_day": day}
    assert ua.stale_cap(brk) == start + timedelta(minutes=75)
    prayer = {"kind": "prayer", "started_at": start, "work_day": day, "planned_seconds": 600}
    assert ua.stale_cap(prayer) == start + timedelta(minutes=20)
    meeting = {"kind": "meeting", "started_at": start, "work_day": day, "planned_seconds": 3600}
    assert ua.stale_cap(meeting) == start + timedelta(hours=2)
    late = {"kind": "meeting", "started_at": datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc),
            "work_day": day, "planned_seconds": 8 * 3600}
    midnight_cairo = datetime(2026, 10, 10, 0, 0, tzinfo=ua.CAIRO_TZ)
    assert ua.stale_cap(late) == midnight_cairo


def test_close_stale_away_only_closes_past_their_cap(monkeypatch):
    start = NOW - timedelta(hours=2)
    rows = [
        {"away_id": "old", "kind": "break", "started_at": start, "work_day": date(2026, 10, 9),
         "planned_seconds": None},
        {"away_id": "new", "kind": "break", "started_at": NOW - timedelta(minutes=10),
         "work_day": date(2026, 10, 9), "planned_seconds": None},
    ]
    fake = FakeDb(open_rows=rows)
    _install(monkeypatch, fake)
    assert ua.close_stale_away(object(), user_id=USER, now=NOW) == 1
    update = fake.sql("SET ended_at = %s, auto_closed = true")
    assert update == [(start + timedelta(minutes=75), "old")]


def test_board_shows_live_status_today_only():
    users = [{"user_id": "u1", "display_name": "Nour", "username": "nour", "roles": [],
              "desk_permission": "watching"}]
    presence = {"u1": {"tab_state": "idle", "tab_last_at": NOW - timedelta(seconds=20),
                       "tab_state_since": NOW - timedelta(minutes=7)}}
    today = ua.cairo_today(NOW)
    board = ua.assemble_board(
        users, day_sessions=[], open_sessions=[], day_counts=[], selected=today,
        today=today, now=NOW, presence=presence,
    )
    person = board["people"][0]
    assert person["live_status"] == "idle" and person["online"] is True
    assert person["tracker"]["tab"]["permission"] == "watching"
    past = ua.assemble_board(
        users, day_sessions=[], open_sessions=[], day_counts=[],
        selected=today - timedelta(days=1), today=today, now=NOW, presence=presence,
    )
    assert past["people"][0]["live_status"] is None and past["people"][0]["online"] is False
