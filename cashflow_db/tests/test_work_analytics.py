"""Team work analytics helpers (no live DB)."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cashflow_db.config import SQL_DIR
from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import work_analytics as wa
from cashflow_db.repository.eligibility import COLLECTION_VISIT_SQL, DENIED_VISIT_SQL
from cashflow_db.repository.work_analytics import (
    OPS_ROLE_KEYS,
    SCOPE_ALL,
    SCOPE_OPS,
    SCOPE_SS,
    SS_ROLE_KEYS,
    SS_SUBMITTER_JOIN,
    SS_TEAM_JOIN,
    TEAM_COLLECTION,
    TEAM_ELIGIBILITY,
    TEAM_ROLE_KEYS,
    TEAM_SS,
    TEAM_SUBMISSION,
    _ss_rows,
    add_ss_fact,
    add_ss_status_batch,
    classify_workload_outcome,
    empty_breakdown_row,
    empty_user_metrics,
    fill_breakdown_rows,
    include_ss_history_row,
    list_scoped_users,
    total_submitted_claims,
    merge_user_row,
    parse_payment_amount,
    plan_heartbeat,
    resolve_period,
    resolve_team,
    scope_role_keys,
    summarize_kpis,
    team_role_keys,
    viewer_scope,
    visible_teams,
)


def test_migration_registered():
    assert "053_work_analytics.sql" in MIGRATIONS
    assert MIGRATIONS.index("053_work_analytics.sql") > MIGRATIONS.index(
        "052_recon_check_breakdown.sql"
    )
    text = (SQL_DIR / "053_work_analytics.sql").read_text(encoding="utf-8")
    assert "auth.login_event" in text
    assert "ops.user_activity_slice" in text
    assert "seconds_active" in text


def test_sql_file_exists():
    assert (Path(SQL_DIR) / "053_work_analytics.sql").exists()


def test_parse_payment_amount():
    assert parse_payment_amount("$1,200.50") == Decimal("1200.50")
    assert parse_payment_amount("paid") is None
    assert parse_payment_amount("") is None
    assert parse_payment_amount(None) is None
    assert "(?:" in wa.SS_PAYMENT_SQL
    assert "(\\.[0-9]+)" not in wa.SS_PAYMENT_SQL.replace("(?:\\.[0-9]+)", "")


def test_all_team_january_uses_dos_not_submitter():
    dos = date(2026, 1, 7)
    assert wa.period_start_for(dos, "month") == date(2026, 1, 1)
    assert include_ss_history_row(
        submitter_user_id=None,
        submission_date=None,
        scoped_ids=("u1",),
    )
    assert not include_ss_history_row(
        submitter_user_id=None,
        submission_date=date(2026, 1, 7),
        scoped_ids=("u1",),
        member_id="u1",
    )


def test_classify_workload_outcome():
    assert classify_workload_outcome(None) == "not_entered"
    assert classify_workload_outcome("  ") == "not_entered"
    assert classify_workload_outcome("paid") == "paid"
    assert classify_workload_outcome("Paid") == "paid"
    assert classify_workload_outcome("Denied") == "denied"
    assert classify_workload_outcome("Not Paid") == "denied"
    assert classify_workload_outcome("pending") == "pending"
    assert classify_workload_outcome("submitted") == "submitted"
    assert classify_workload_outcome("corrected") == "corrected"
    assert classify_workload_outcome("Timely Filing") == "timely_filing"
    assert classify_workload_outcome("Timely Filing") != "denied"


def test_ss_submitter_only_sql():
    join = SS_SUBMITTER_JOIN.lower()
    assert "join auth.app_user" in join
    assert "left join" not in join
    assert "flag.submitter" in join
    assert "display_name" in join
    assert "su.username" in join
    assert "regexp_replace" in join
    src = inspect.getsource(_ss_rows)
    assert "updated_by" not in src
    assert "SS_SUBMITTER_JOIN" in src
    assert "SS_TEAM_JOIN" not in src
    assert "SS_WORK_DAY_SQL" in src
    mine = inspect.getsource(wa.ss_claims_today_for_user)
    assert "SS_SUBMITTER_JOIN" in mine
    assert "SS_WORK_DAY_SQL" in mine
    assert "SS_WORKLOAD_WHERE" in mine
    assert "completed_today" not in mine
    assert wa.ss_claims_today_for_user(None, "") == 0  # type: ignore[arg-type]
    detail_src = inspect.getsource(wa.user_detail)
    assert "updated_by" not in detail_src
    team_join = SS_TEAM_JOIN.lower()
    assert "left join" in team_join
    assert "regexp_replace" in team_join
    assert "su.username" in team_join
    team_src = inspect.getsource(wa._ss_team_totals)
    assert "SS_DOS_SQL" in team_src
    assert "SS_TEAM_INCLUDE_SQL" not in team_src
    summary_src = inspect.getsource(wa.team_summary)
    assert "summarize_kpis" in summary_src
    assert "_ss_rows" in summary_src
    assert "apply_team_ss_kpis" not in summary_src
    assert "_ss_team_totals" not in summary_src
    bd = inspect.getsource(wa.ss_breakdown)
    assert "SS_DOS_SQL" in bd
    assert "SS_SUBMITTER_JOIN" in bd
    assert "(?:" in wa.SS_PAYMENT_SQL
    work_day = wa.SS_WORK_DAY_SQL.upper()
    assert "GREATEST" not in work_day
    assert "COALESCE" in work_day
    assert "SUBMISSION_DATE" in work_day
    assert "UPDATED_AT" in work_day
    assert "flag.dos" not in wa.SS_WORK_DAY_SQL.lower()


def test_completed_today_matches_team_filters():
    src = inspect.getsource(wa.completed_today_for_user)
    assert "ELIG_SHEET_WHERE" in src
    assert "eligibility_status IN ('completed', 'rejected')" in src
    assert "completed_at::date" in src
    assert "COLLECTION_QUEUE_WHERE" in src
    assert "COLLECTION_WORKED_SQL" in src
    assert "COLLECTION_WORK_DAY_SQL" in src
    assert "workflow_status IN ('resolved', 'ignored')" in src
    assert "CPT_LIVE_DOMAIN_SQL" in src
    assert "resolved_at::date" in src
    assert wa.completed_today_for_user(None, "", "eligibility") == 0  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        wa.completed_today_for_user(None, "11111111-1111-1111-1111-111111111111", "finance")  # type: ignore[arg-type]


def test_include_ss_history_row_all_team_vs_member():
    team = ("u1", "u2")
    assert include_ss_history_row(
        submitter_user_id=None,
        submission_date=date(2026, 2, 10),
        scoped_ids=team,
    )
    assert not include_ss_history_row(
        submitter_user_id=None,
        submission_date=date(2026, 2, 10),
        scoped_ids=team,
        member_id="u1",
    )
    assert include_ss_history_row(
        submitter_user_id="u1",
        submission_date=None,
        scoped_ids=team,
    )
    assert include_ss_history_row(
        submitter_user_id="u1",
        submission_date=None,
        scoped_ids=team,
        member_id="u1",
    )
    assert not include_ss_history_row(
        submitter_user_id="u1",
        submission_date=None,
        scoped_ids=team,
        member_id="u2",
    )
    assert include_ss_history_row(
        submitter_user_id=None,
        submission_date=None,
        scoped_ids=team,
    )


def test_team_summary_kpis_use_people_sum_not_dos():
    src = inspect.getsource(wa.team_summary)
    assert "summarize_kpis(merged)" in src
    assert "apply_team_ss_kpis" not in src
    assert "_ss_team_totals" not in src
    people = [
        {**empty_user_metrics(), "ss_claims_today": 2, "ss_claims_month": 10, "ss_paid": 4, "ss_money": 15},
        {**empty_user_metrics(), "ss_claims_today": 1, "ss_claims_month": 5, "ss_paid": 2, "ss_money": 7},
    ]
    kpis = summarize_kpis(people)
    assert kpis["ss_claims_today"] == 3
    assert kpis["ss_claims_month"] == 15
    assert kpis["ss_paid"] == 6
    assert kpis["ss_money"] == 22
    assert kpis["people"] == 2
    bd = inspect.getsource(wa.ss_breakdown)
    assert "SS_DOS_SQL" in bd
    assert "SS_SUBMITTER_JOIN" in bd


def test_tfl_and_corrected_buckets_are_separate():
    paid = add_ss_fact(empty_breakdown_row(), status="paid", payment=100)
    denied = add_ss_fact(empty_breakdown_row(), status="Denied", payment=0)
    tfl = add_ss_fact(empty_breakdown_row(), status="Timely Filing", payment=0)
    corrected = add_ss_fact(empty_breakdown_row(), status="corrected", payment=25)
    submitted = add_ss_fact(
        empty_breakdown_row(), status="submitted", payment=0, submission_date=date(2026, 9, 1)
    )
    assert paid["paid"] == 1 and paid["denied"] == 0 and paid["timely_filing"] == 0
    assert denied["denied"] == 1 and denied["timely_filing"] == 0
    assert tfl["timely_filing"] == 1 and tfl["denied"] == 0 and tfl["corrected"] == 0
    assert corrected["corrected"] == 1 and corrected["denied"] == 0
    assert submitted["submitted"] == 1 and submitted["pending"] == 0

    batch = empty_breakdown_row()
    add_ss_status_batch(batch, status="Timely Filing", claims=4, payment=0)
    add_ss_status_batch(batch, status="corrected", claims=2, payment=10)
    add_ss_status_batch(batch, status="Denied", claims=3, payment=0)
    assert batch["timely_filing"] == 4
    assert batch["corrected"] == 2
    assert batch["denied"] == 3
    assert batch["paid"] == 0


def test_monthly_breakdown_has_twelve_rows_and_total():
    grouped = {
        date(2026, 1, 1): {
            **empty_breakdown_row(),
            "claims": 2,
            "paid": 1,
            "denied": 1,
            "payment": 50,
            "submitted": 2,
        },
        date(2026, 9, 1): {
            **empty_breakdown_row(),
            "claims": 5,
            "timely_filing": 2,
            "corrected": 1,
            "payment": 80,
            "submitted": 4,
        },
    }
    payload = fill_breakdown_rows("month", 2026, grouped)
    assert payload["grain"] == "month"
    assert len(payload["rows"]) == 12
    assert payload["rows"][0]["period"] == "Jan 2026"
    assert payload["rows"][0]["total_submitted_claims"] == 4
    assert payload["rows"][8]["period"] == "Sep 2026"
    assert payload["rows"][8]["timely_filing"] == 2
    assert payload["rows"][8]["corrected"] == 1
    assert payload["rows"][8]["total_submitted_claims"] == 5
    assert payload["totals"]["period"] == "Total"
    assert payload["totals"]["claims"] == 7
    assert payload["totals"]["payment"] == 130
    assert payload["totals"]["denied"] == 1
    assert payload["totals"]["timely_filing"] == 2
    assert payload["totals"]["total_submitted_claims"] == 9
    assert payload["totals"]["total_submitted_claims"] == total_submitted_claims(
        payload["totals"]
    )
    assert payload["totals"]["total_submitted_claims"] != payload["totals"]["claims"]

    days = fill_breakdown_rows("day", 2026, {}, month=9)
    assert len(days["rows"]) == 30
    weeks = fill_breakdown_rows("week", 2026, {})
    assert len(weeks["rows"]) >= 52


def test_resolve_team_ss_lead_scope():
    assert resolve_team(["second_submission_lead"], None) == TEAM_SS
    assert resolve_team(["ops_admin"], TEAM_SS) == TEAM_SS
    assert resolve_team(["super_admin"], "second_submission") == TEAM_SS
    assert resolve_team(["ops_admin"], "eligibility") == "eligibility"
    assert resolve_team(["super_admin"], "eligibility") == "eligibility"
    assert resolve_team(["ops_admin"], "collection") == "collection"
    assert resolve_team(["analytics_viewer"], "eligibility") == "eligibility"
    assert resolve_team(["sub_admin"], "submission") == "submission"
    with pytest.raises(ValueError):
        resolve_team(["ops_admin"], "tracker")
    with pytest.raises(PermissionError):
        resolve_team(["second_submission_lead"], "eligibility")
    with pytest.raises(PermissionError):
        resolve_team(["second_submission_lead"], "collection")
    with pytest.raises(PermissionError):
        resolve_team(["second_submission_lead"], "submission")


def test_viewer_scope_roles():
    assert viewer_scope(["super_admin"]) == SCOPE_ALL
    assert viewer_scope(["sub_admin"]) == SCOPE_ALL
    assert viewer_scope(["ops_admin"]) == SCOPE_OPS
    assert viewer_scope(["analytics_viewer"]) == SCOPE_OPS
    assert viewer_scope(["second_submission_lead"]) == SCOPE_SS
    assert viewer_scope(["ops_admin", "second_submission_lead"]) == SCOPE_OPS
    assert viewer_scope(["analytics_viewer", "second_submission_lead"]) == SCOPE_OPS
    assert scope_role_keys(SCOPE_ALL) is None
    assert set(scope_role_keys(SCOPE_OPS) or ()) == set(OPS_ROLE_KEYS)
    assert set(scope_role_keys(SCOPE_SS) or ()) == set(SS_ROLE_KEYS)
    with pytest.raises(PermissionError):
        viewer_scope(["posting_team"])
    with pytest.raises(PermissionError):
        viewer_scope(["finance"])
    with pytest.raises(PermissionError):
        viewer_scope(["second_submission"])


def test_resolve_period_presets():
    today = date(2026, 9, 7)
    start, end = resolve_period("today", today=today)
    assert start.date() == today
    assert end.date() == date(2026, 9, 8)

    start, end = resolve_period("week", today=today)
    assert start.date() == date(2026, 9, 7)
    assert end.date() == date(2026, 9, 14)

    start, end = resolve_period("month", today=today)
    assert start.date() == date(2026, 9, 1)
    assert end.date() == date(2026, 9, 8)

    start, end = resolve_period(
        "custom", date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), today=today
    )
    assert start.date() == date(2026, 8, 1)
    assert end.date() == date(2026, 9, 1)


def test_plan_heartbeat_idle_and_gap():
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert plan_heartbeat(None, now, idle=True)["action"] == "skip"
    assert plan_heartbeat(None, now)["action"] == "open"

    last = {"last_ping_at": now - timedelta(seconds=30)}
    ext = plan_heartbeat(last, now)
    assert ext["action"] == "extend"
    assert ext["add_seconds"] == 30

    last = {"last_ping_at": now - timedelta(seconds=121)}
    assert plan_heartbeat(last, now)["action"] == "open"

    last = {"last_ping_at": now - timedelta(seconds=120)}
    assert plan_heartbeat(last, now)["action"] == "extend"


def test_presence_heartbeat_refreshes_ping_without_work_seconds(monkeypatch):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    captured: dict = {}

    def fetchone(conn, sql, params):
        return {
            "slice_id": "slice-1",
            "last_ping_at": now - timedelta(seconds=40),
            "seconds_active": 100,
        }

    def execute(conn, sql, params):
        captured["params"] = params

    monkeypatch.setattr(wa.client, "fetchone", fetchone)
    monkeypatch.setattr(wa.client, "execute", execute)
    user_id = "11111111-1111-1111-1111-111111111111"
    background = wa.record_heartbeat(object(), user_id, presence=True, now=now)
    assert background["action"] == "extend"
    assert background["add_seconds"] == 0
    assert background["add_desk_seconds"] == 40
    assert captured["params"][1] == 0
    assert captured["params"][2] == 40
    assert captured["params"][3] is None

    visible = wa.record_heartbeat(
        object(), user_id, page_path="/eligibility", presence=False, now=now
    )
    assert visible["add_seconds"] == 40
    assert visible["add_desk_seconds"] == 40
    assert captured["params"][1] == 40
    assert captured["params"][2] == 40
    assert captured["params"][3] == "/eligibility"


def test_desk_permission_is_stored_without_changing_time(monkeypatch):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    calls: list[tuple] = []

    def fetchone(conn, sql, params):
        return {
            "slice_id": "slice-1",
            "last_ping_at": now - timedelta(seconds=40),
            "seconds_active": 100,
        }

    def execute(conn, sql, params):
        calls.append((sql, params))

    monkeypatch.setattr(wa.client, "fetchone", fetchone)
    monkeypatch.setattr(wa.client, "execute", execute)
    user_id = "11111111-1111-1111-1111-111111111111"
    saved = wa.record_heartbeat(
        object(),
        user_id,
        presence=True,
        desk_permission=" Denied ",
        now=now,
    )
    assert saved["add_seconds"] == 0
    assert saved["add_desk_seconds"] == 40
    assert saved["add_idle_seconds"] == 0
    permission = [params for sql, params in calls if "desk_permission" in sql]
    assert permission == [("denied", user_id)]
    slice_update = [params for sql, params in calls if "seconds_desk" in sql][0]
    assert slice_update[1] == 0
    assert slice_update[2] == 40

    calls.clear()
    ignored = wa.record_heartbeat(
        object(),
        user_id,
        presence=True,
        desk_permission="camera",
        now=now,
    )
    assert ignored["add_desk_seconds"] == 40
    assert ignored["add_idle_seconds"] == 0
    assert not any("desk_permission" in sql for sql, _params in calls)
    assert "070_desk_permission.sql" in MIGRATIONS


def test_closed_heartbeat_stamps_ping_without_desk_time(monkeypatch):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    captured: dict = {}

    def fetchone(conn, sql, params):
        return {
            "slice_id": "slice-1",
            "last_ping_at": now - timedelta(seconds=20),
            "seconds_active": 100,
        }

    def execute(conn, sql, params):
        captured["sql"] = sql
        captured["params"] = params

    monkeypatch.setattr(wa.client, "fetchone", fetchone)
    monkeypatch.setattr(wa.client, "execute", execute)
    user_id = "11111111-1111-1111-1111-111111111111"
    closed = wa.record_heartbeat(object(), user_id, closed=True, now=now)
    assert closed["action"] == "close"
    assert closed["add_seconds"] == 0
    assert closed["add_desk_seconds"] == 0
    assert "seconds_desk" not in captured["sql"]
    assert captured["params"] == (now, "slice-1")

    monkeypatch.setattr(wa.client, "fetchone", lambda *a, **k: None)
    missing = wa.record_heartbeat(object(), user_id, closed=True, now=now)
    assert missing["action"] == "skip"
    assert missing["add_desk_seconds"] == 0


def test_return_gap_counts_idle_until_three_hours(monkeypatch):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert wa.idle_seconds_for_gap(10 * 60) == 10 * 60
    assert wa.idle_seconds_for_gap(5 * 60 * 60) == 0
    assert wa.idle_seconds_for_gap(10 * 60, away_overlap_seconds=4 * 60) == 6 * 60
    captured: dict = {}

    def fetchone(conn, sql, params):
        return {
            "slice_id": "slice-1",
            "last_ping_at": now - timedelta(minutes=10),
            "seconds_active": 100,
        }

    def fetchall(conn, sql, params):
        return [
            {
                "started_at": now - timedelta(minutes=8),
                "ended_at": now - timedelta(minutes=4),
            }
        ]

    def execute(conn, sql, params):
        captured["sql"] = sql
        captured["params"] = params

    monkeypatch.setattr(wa.client, "fetchone", fetchone)
    monkeypatch.setattr(wa.client, "fetchall", fetchall)
    monkeypatch.setattr(wa.client, "execute", execute)
    user_id = "11111111-1111-1111-1111-111111111111"
    opened = wa.record_heartbeat(object(), user_id, now=now)
    assert opened["action"] == "open"
    assert opened["add_seconds"] == 0
    assert opened["add_desk_seconds"] == 0
    assert opened["add_idle_seconds"] == 6 * 60
    assert captured["params"][4] == 6 * 60

    def fetchone_long(conn, sql, params):
        return {
            "slice_id": "slice-1",
            "last_ping_at": now - timedelta(hours=5),
            "seconds_active": 100,
        }

    monkeypatch.setattr(wa.client, "fetchone", fetchone_long)
    long_gap = wa.record_heartbeat(object(), user_id, now=now)
    assert long_gap["add_idle_seconds"] == 0
    assert captured["params"][4] == 0


def test_merge_and_kpis():
    base = {
        "user_id": "u1",
        "display_name": "Nouran",
        "username": "n@x.com",
        "roles": ["second_submission"],
        "is_active": True,
        "last_login_at": None,
    }
    row = merge_user_row(
        base,
        {"seconds_today": 3600, "seconds_period": 3600, "last_activity_at": None},
        {"elig_touched": 4, "elig_completed": 2, "elig_money": Decimal("10.5")},
        {
            "ss_claims": 3,
            "ss_claims_today": 1,
            "ss_claims_week": 2,
            "ss_claims_month": 3,
            "ss_paid": 1,
            "ss_denied": 1,
            "ss_pending": 0,
            "ss_timely_filing": 1,
            "ss_corrected": 0,
            "ss_not_entered": 0,
            "ss_money": 200,
        },
    )
    assert row["elig_touched"] == 4
    assert row["ss_denied"] == 1
    assert row["ss_not_entered"] == 0
    assert row["ss_timely_filing"] == 1
    assert row["ss_claims_today"] == 1
    assert row["money_total"] == 210.5
    assert row["seconds_today"] == 3600
    kpis = summarize_kpis([row, empty_user_metrics() | {"display_name": "x"}])
    assert kpis["people"] == 2
    assert kpis["ss_claims"] == 3
    assert kpis["ss_timely_filing"] == 1
    assert kpis["elig_touched"] == 4


def test_visible_teams_and_role_map():
    assert wa.KNOWN_TEAMS == (TEAM_SS, TEAM_ELIGIBILITY, TEAM_COLLECTION, TEAM_SUBMISSION)
    assert team_role_keys(TEAM_ELIGIBILITY) == ("posting_team",)
    assert team_role_keys(TEAM_COLLECTION) == ("collector",)
    assert team_role_keys(TEAM_SUBMISSION) == ("submission",)
    assert TEAM_ROLE_KEYS[TEAM_SS] == SS_ROLE_KEYS
    admin = visible_teams(["ops_admin"])
    assert [t["key"] for t in admin] == list(wa.KNOWN_TEAMS)
    assert [t["key"] for t in visible_teams(["analytics_viewer"])] == list(wa.KNOWN_TEAMS)
    assert {t["label"] for t in admin} == {
        "Second Submission",
        "Eligibility",
        "Collection",
        "Submission",
    }
    lead = visible_teams(["second_submission_lead"])
    assert lead == [{"key": TEAM_SS, "label": "Second Submission"}]
    scoped = inspect.getsource(list_scoped_users)
    assert "team_role_keys" in scoped
    assert "scope_role_keys" not in scoped


def test_elig_collection_cpt_sql_split():
    assert DENIED_VISIT_SQL in wa.COLLECTION_QUEUE_WHERE
    assert COLLECTION_VISIT_SQL in wa.COLLECTION_QUEUE_WHERE
    assert "NOT" in wa.ELIG_SHEET_WHERE
    assert DENIED_VISIT_SQL in wa.ELIG_SHEET_WHERE
    assert COLLECTION_VISIT_SQL in wa.ELIG_SHEET_WHERE
    elig_src = inspect.getsource(wa._elig_rows)
    assert "ELIG_SHEET_WHERE" in elig_src
    assert "COLLECTION_QUEUE_WHERE" not in elig_src
    coll_src = inspect.getsource(wa._collection_rows)
    assert "COLLECTION_QUEUE_WHERE" in coll_src
    assert "ELIG_SHEET_WHERE" not in coll_src
    assert "COLLECTION_WORKED_SQL" in coll_src
    assert "COLLECTION_WORK_DAY_SQL" in coll_src
    assert "GREATEST" in wa.COLLECTION_WORK_DAY_SQL.upper()
    assert "collection_status" in wa.COLLECTION_WORKED_SQL
    cpt_src = inspect.getsource(wa._cpt_rows)
    assert "CPT_LIVE_DOMAIN_SQL" in cpt_src
    assert "audit_domain" in cpt_src
    assert "demo" in wa.CPT_LIVE_DOMAIN_SQL
    assert "resolved" in cpt_src
    assert "ignored" in cpt_src
    rows = wa._expand_domain_counts(
        [
            {
                "user_id": "u1",
                "audit_domain": "cpt",
                "n": 3,
                "n_today": 1,
                "n_week": 2,
                "n_month": 3,
            },
            {
                "user_id": "u1",
                "audit_domain": "icd",
                "n": 4,
                "n_today": 0,
                "n_week": 1,
                "n_month": 4,
            },
        ],
        "resolved",
    )
    assert len(rows) == 1
    assert rows[0]["cpt_resolved"] == 3
    assert rows[0]["icd_resolved"] == 4
    assert rows[0]["icd_resolved_month"] == 4


def test_team_summary_wires_team_metrics_not_ss_totals():
    src = inspect.getsource(wa.team_summary)
    assert "summarize_kpis(merged)" in src
    assert "_ss_rows" in src
    assert "_elig_rows" in src
    assert "_collection_rows" in src
    assert "_cpt_rows" in src
    assert "if team_key == TEAM_SS" in src
    assert "apply_team_ss_kpis" not in src
    assert "_ss_team_totals" not in src
    assert "_tracker_rows" not in src
    people = [
        {
            **empty_user_metrics(),
            "elig_completed_month": 2,
            "elig_touched_today": 1,
            "elig_money": 9,
        },
        {
            **empty_user_metrics(),
            "elig_completed_month": 5,
            "elig_touched_today": 3,
            "elig_money": 1,
        },
    ]
    kpis = summarize_kpis(people)
    assert kpis["elig_completed_month"] == 7
    assert kpis["elig_touched_today"] == 4
    assert kpis["elig_money"] == 10
    assert kpis["ss_claims_month"] == 0
    row = merge_user_row(
        {"user_id": "u1", "display_name": "A", "username": "a", "roles": ["collector"], "is_active": True},
        {"coll_worked_today": 2, "coll_worked_month": 6, "coll_touched": 4},
    )
    assert row["coll_worked_today"] == 2
    assert row["coll_worked_month"] == 6
    assert row["ss_claims"] == 0
    detail_src = inspect.getsource(wa.user_detail)
    assert "updated_by" not in detail_src
    assert "_ss_recent_rows" in detail_src
    assert "_elig_recent_rows" in detail_src
    assert "_collection_recent_rows" in detail_src
    assert "_cpt_recent_rows" in detail_src


def test_collection_status_canonical_and_recovered_rule():
    assert wa.canonical_collection_status("action taken") == "Action Taken"
    assert wa.canonical_collection_status(" Dead ") == "Dead"
    assert wa.canonical_collection_status("Paid") == "Paid"
    assert wa.canonical_collection_status("mystery") == "Other"
    assert wa.canonical_collection_status("  ") == ""
    bucket = wa.empty_status_counts()
    wa.apply_status_count(bucket, "Action Taken", 2)
    wa.apply_status_count(bucket, "action taken", 1)
    wa.apply_status_count(bucket, "not a status", 4)
    assert bucket["Action Taken"] == 3
    assert bucket["Other"] == 4
    assert wa.claim_recovered(set_paid=True, visit_paid=False) is True
    assert wa.claim_recovered(set_paid=False, visit_paid=True) is True
    assert wa.claim_recovered(set_paid=False, visit_paid=False) is False


def test_collection_recovered_sql_uses_history_not_current_queue():
    status_src = inspect.getsource(wa._collection_status_rows)
    assert "ops.eligibility_history" in status_src
    assert "column_name = 'collection_status'" in status_src
    assert "COLLECTION_QUEUE_WHERE" not in status_src
    assert "DISTINCT ON" in status_src
    person_sql = wa._collection_worked_sql(by_user=True)
    team_sql = wa._collection_worked_sql(by_user=False)
    assert "ops.eligibility_history" in person_sql
    assert "h.changed_by AS user_id" in person_sql
    assert "GROUP BY h.changed_by, h.work_item_id" in person_sql
    assert "h.changed_by AS user_id" not in team_sql
    assert "GROUP BY h.work_item_id" in team_sql
    assert "COLLECTION_QUEUE_WHERE" not in person_sql
    assert "set_paid_today" in wa.COLLECTION_RECOVERED_TODAY_SQL
    assert "paid" in wa.COLLECTION_RECOVERED_TODAY_SQL
    assert "deduct" in wa.COLLECTION_RECOVERED_TODAY_SQL
    assert "insurance_payment" in wa.COLLECTION_PAYMENT_SQL
    assert "client_payment" in wa.COLLECTION_PAYMENT_SQL
    assert "COLLECTION_QUEUE_WHERE" not in inspect.getsource(wa._collection_recovered_rows)
    assert "_collection_team_recovered" in inspect.getsource(wa.team_summary)
    row = merge_user_row(
        {"user_id": "u1", "display_name": "A", "username": "a", "roles": ["collector"], "is_active": True},
        {
            "coll_recovered_today": 1,
            "coll_recovered_month": 2,
            "coll_money_today": 10.5,
            "coll_money_month": 20.25,
            "coll_status_today": {"Dead": 1},
            "coll_status_month": {"Action Taken": 2},
        },
    )
    assert row["coll_status_today"]["Dead"] == 1
    assert row["coll_status_month"]["Action Taken"] == 2
    kpis = summarize_kpis(
        [
            row,
            merge_user_row(
                {"user_id": "u2", "display_name": "B", "username": "b", "roles": ["collector"], "is_active": True},
                {"coll_money_month": 1.75, "coll_recovered_month": 1},
            ),
        ]
    )
    assert kpis["coll_recovered_month"] == 3
    assert kpis["coll_money_month"] == 22.0


def test_collection_root_cause_month_rollup():
    payload = wa.rollup_root_causes(
        2026,
        [
            {"month_start": date(2026, 1, 1), "root_cause": "Auth delay", "n": 2},
            {"month_start": date(2026, 1, 1), "root_cause": "auth delay", "n": 1},
            {"month_start": date(2026, 1, 1), "root_cause": "Case got denied", "n": 2},
            {"month_start": date(2026, 3, 1), "root_cause": "brand new cause", "n": 4},
            {"month_start": date(2025, 12, 1), "root_cause": "Auth delay", "n": 9},
        ],
    )
    assert payload["year"] == 2026
    assert len(payload["rows"]) == 12
    january = payload["rows"][0]
    assert january["period"] == "Jan 2026"
    assert january["counts"]["Auth delay"] == 3
    assert january["counts"]["Case got denied"] == 2
    assert january["top"] == "Auth delay"
    assert january["top_count"] == 3
    march = payload["rows"][2]
    assert march["counts"]["Other"] == 4
    assert march["top"] == "Other"
    assert payload["rows"][1]["top"] == ""
    assert payload["rows"][1]["top_count"] == 0
    src = inspect.getsource(wa.collection_root_cause_breakdown)
    assert "column_name = 'root_cause'" in src
    assert "date_trunc('month', wi.dos)" in src
    assert "date_trunc('month', h.changed_at)" not in src
    assert "DISTINCT ON (h.work_item_id)" in src
    assert "eligibility_work_item" in src
    assert "h.changed_by" in src
    assert "COLLECTION_QUEUE_WHERE" not in src
    assert "rollup_root_causes" in src
    assert "rollup_root_cause_people" in src


def test_root_cause_counts_once_for_last_writer_in_dos_month():
    first = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    facts = wa.last_root_cause_write(
        [
            {
                "work_item_id": "claim-1",
                "changed_at": first,
                "changed_by": "alice",
                "root_cause": "Auth delay",
                "dos": date(2026, 1, 15),
            },
            {
                "work_item_id": "claim-1",
                "changed_at": later,
                "changed_by": "bob",
                "root_cause": "Case got denied",
                "dos": date(2026, 1, 15),
            },
        ]
    )
    assert len(facts) == 1
    assert facts[0]["user_id"] == "bob"
    assert facts[0]["month_start"] == date(2026, 1, 1)
    assert facts[0]["root_cause"] == "Case got denied"
    people = wa.rollup_root_cause_people(
        facts,
        [
            {"user_id": "alice", "display_name": "Alice"},
            {"user_id": "bob", "display_name": "Bob"},
        ],
        ["Auth delay", "Case got denied"],
    )
    by_name = {row["display_name"]: row for row in people}
    assert by_name["Alice"]["counts"]["Auth delay"] == 0
    assert by_name["Alice"]["top"] == ""
    assert by_name["Bob"]["counts"]["Case got denied"] == 1
    assert by_name["Bob"]["top"] == "Case got denied"


def test_latest_assignment_and_finish_after_status():
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
    me = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    collector = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    latest = wa.latest_assignment_event(
        [
            {"changed_at": t0, "changed_by": other, "new_value": collector},
            {"changed_at": t1, "changed_by": me, "new_value": collector},
        ]
    )
    assert latest["assigner_id"] == me
    assert latest["assignee_id"] == collector
    cleared = wa.latest_assignment_event(
        [
            {"changed_at": t0, "changed_by": me, "new_value": collector},
            {"changed_at": t2, "changed_by": me, "new_value": ""},
        ]
    )
    assert cleared is None
    assert wa.status_finishes_assignment(
        assignee_id=collector,
        assigned_at=t1,
        status_by=collector,
        status_at=t0,
        status_value="Action Taken",
    ) is False
    assert wa.status_finishes_assignment(
        assignee_id=collector,
        assigned_at=t1,
        status_by=other,
        status_at=t2,
        status_value="Dead",
    ) is False
    assert wa.status_finishes_assignment(
        assignee_id=collector,
        assigned_at=t1,
        status_by=collector,
        status_at=t2,
        status_value="Action Taken",
    ) is True
    sql = wa.LATEST_ASSIGNMENT_SQL
    assert "DISTINCT ON (h.work_item_id)" in sql
    assert "column_name = 'assigned_to'" in sql
    assert "column_name = 'collection_status'" in sql
    assert "s.changed_at > open_assign.changed_at" in sql
    assert "assigner_id = %s::uuid" in inspect.getsource(wa._viewer_assignment_rows)
    assert "assignee_id = %s" in inspect.getsource(wa.my_assignment_progress)
    assert "viewer_id" in inspect.getsource(wa.team_summary)


def test_dead_root_cause_rollup_counts_and_share():
    payload = wa.rollup_dead_root_causes(
        [
            {"root_cause": "Auth delay", "n": 2},
            {"root_cause": "auth delay", "n": 1},
            {"root_cause": None, "n": 1},
            {"root_cause": "brand new cause", "n": 2},
        ]
    )
    assert payload["total"] == 6
    assert payload["rows"][0] == {"label": "Auth delay", "count": 3, "percent": 50.0}
    assert payload["rows"][1] == {"label": "Other", "count": 2, "percent": 33.3}
    assert payload["rows"][2] == {"label": "No root cause", "count": 1, "percent": 16.7}
    sql = wa.dead_root_cause_sql()
    assert "collection_queue_member" in sql
    assert "m.bucket = 'dead'" in sql
    assert "manual_overrides->>'root_cause'" in sql
    assert "context->>'root_cause'" in sql
    assert "snowflake_visit_kpi" in sql
