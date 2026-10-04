"""Finance aggregate SQL stays on the server and matches the AR date rules."""

from __future__ import annotations

from datetime import date

from cashflow_db.repository import forecast as forecast_repo


def _patch(monkeypatch, rows):
    captured: dict[str, object] = {}

    def fake_fetchone(_conn, sql, params=None):
        if "forecast_run" in sql:
            return {"forecast_run_id": "11111111-1111-1111-1111-111111111111"}
        return None

    def fake_fetchall(_conn, sql, params=None):
        captured.setdefault("sqls", []).append(sql)
        captured["sql"] = sql
        captured["params"] = params
        return rows

    monkeypatch.setattr(forecast_repo.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(forecast_repo.client, "fetchall", fake_fetchall)
    return captured


def test_overdue_grid_skips_eligibility_and_splits_clinic(monkeypatch):
    captured = _patch(monkeypatch, [])
    forecast_repo.overdue_grid(
        None,  # type: ignore[arg-type]
        d0=date(2026, 8, 1),
        d1=date(2026, 8, 31),
        facilities=["Bedstuy"],
        insurers=["1199"],
    )
    forecast_repo.overdue_clinic_totals(
        None,  # type: ignore[arg-type]
        d0=date(2026, 8, 1),
        d1=date(2026, 8, 31),
        facilities=["Bedstuy"],
        insurers=["1199"],
    )
    sqls = [str(sql) for sql in captured["sqls"]]
    ins_sql, clinic_sql = sqls[0], sqls[1]
    assert "eligibility_work_item" not in ins_sql
    assert "eligibility_work_item" not in clinic_sql
    assert "outcome_stage = 'overdue'" in ins_sql
    assert "original_forecast_date" in ins_sql
    assert "expected_pay_date" in ins_sql
    assert "YYYY-MM" in ins_sql
    assert "'0_14'" in ins_sql
    assert "GROUP BY 1, 2, 3, 4" in ins_sql
    assert "GROUP BY 1, 2, 3, 4, 5" not in ins_sql
    assert "fp.payload->>'facility_name'" in clinic_sql
    assert "GROUP BY 1" in clinic_sql
    params = list(captured["params"])
    assert date(2026, 8, 1) in params
    assert ["Bedstuy"] in params
    assert ["1199"] in params


def test_ar_trend_reads_stored_stage_counts(monkeypatch):
    run_id = "11111111-1111-1111-1111-111111111111"
    calls: list[str] = []

    def fake_fetchall(_conn, sql, params=None):
        calls.append(sql)
        if "DISTINCT ON" in sql:
            assert params == (12,)
            return [{"forecast_run_id": run_id, "as_of_date": date(2026, 9, 1)}]
        if "forecast_feature" in sql:
            assert "outcome_stage_counts" in sql
            assert "forecast_prediction" not in sql
            return [
                {
                    "forecast_run_id": run_id,
                    "feature_key": "overdue",
                    "payload": {"outcome_stage": "overdue", "amount": 5},
                },
                {
                    "forecast_run_id": run_id,
                    "feature_key": "on_track",
                    "payload": {"outcome_stage": "on_track", "amount": 2},
                },
            ]
        assert "overdue_days, 0) > 90" in sql
        assert "forecast_run_id = ANY" in sql
        return [{"forecast_run_id": run_id, "amount": 1}]

    monkeypatch.setattr(forecast_repo.client, "fetchall", fake_fetchall)
    rows = forecast_repo.ar_trend(None, weeks=12)  # type: ignore[arg-type]
    assert "LEFT JOIN" not in calls[0]
    assert "forecast_prediction" not in calls[0]
    assert "forecast_prediction" not in calls[1]
    assert rows[0]["as_of"] == "2026-09-01"
    assert rows[0]["overdue"] == 5.0
    assert rows[0]["on_track"] == 2.0
    assert rows[0]["overdue_90_plus"] == 1.0


def test_four_week_card_reads_two_feature_runs(monkeypatch):
    latest_id = "11111111-1111-1111-1111-111111111111"
    prior_id = "22222222-2222-2222-2222-222222222222"
    feature_sql = ""

    def fake_fetchone(_conn, sql, params=None):
        if "as_of_date <=" in sql:
            assert params == (date(2026, 8, 8),)
            return {"forecast_run_id": prior_id, "as_of_date": date(2026, 8, 1)}
        return {"forecast_run_id": latest_id, "as_of_date": date(2026, 9, 5)}

    def fake_fetchall(_conn, sql, params=None):
        nonlocal feature_sql
        feature_sql = sql
        assert "forecast_prediction" not in sql
        assert "outcome_stage_counts" in sql
        assert list(params[0]) == [latest_id, prior_id]
        return [
            {
                "forecast_run_id": latest_id,
                "feature_key": "overdue",
                "payload": {"outcome_stage": "overdue", "amount": 180},
            },
            {
                "forecast_run_id": prior_id,
                "feature_key": "overdue",
                "payload": {"outcome_stage": "overdue", "amount": 110},
            },
        ]

    monkeypatch.setattr(forecast_repo.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(forecast_repo.client, "fetchall", fake_fetchall)
    rows = forecast_repo.overdue_four_week_points(None)  # type: ignore[arg-type]
    assert "forecast_feature" in feature_sql
    assert [row["as_of"] for row in rows] == ["2026-08-01", "2026-09-05"]
    assert rows[0]["overdue"] == 110.0
    assert rows[1]["overdue"] == 180.0


def test_forward_expected_window_and_stages(monkeypatch):
    captured = _patch(monkeypatch, [])
    forecast_repo.forward_expected_by_week(
        None,  # type: ignore[arg-type]
        start=date(2026, 10, 1),
        end=date(2026, 10, 31),
        facilities=["Bedstuy"],
    )
    sql = str(captured["sql"])
    params = list(captured["params"])
    assert "outcome_stage IN ('on_track', 'overdue')" in sql
    assert "date_trunc('week'" in sql
    assert "original_forecast_date" in sql
    assert date(2026, 10, 1) in params
    assert date(2026, 10, 31) in params
    assert ["Bedstuy"] in params


def test_collection_rate_groups_service_month(monkeypatch):
    captured = _patch(
        monkeypatch,
        [{"period": "2026-06", "outcome_stage": "paid", "amount": 12}],
    )
    rows = forecast_repo.collection_rate_by_month(
        None,  # type: ignore[arg-type]
        d0=date(2026, 6, 1),
        insurers=["1199"],
    )
    sql = str(captured["sql"])
    assert "date_of_service" in sql
    assert "GROUP BY 1, 2" in sql
    assert "dos IS NOT NULL" in sql
    assert ["1199"] in list(captured["params"])
    assert rows[0]["outcome_stage"] == "paid"


def test_chase_rank_orders_by_age_factor_not_days(monkeypatch):
    captured = _patch(monkeypatch, [])
    forecast_repo.list_overdue_claims(
        None,  # type: ignore[arg-type]
        rank="recovery",
        limit=15,
    )
    sql = str(captured["sql"])
    join_at = sql.find("eligibility_work_item")
    assert join_at > 0
    assert "LIMIT 40" in sql[:join_at]
    assert "snowflake_visit_kpi" in sql[join_at:]
    assert "0.85" in sql
    assert "0.35" in sql
    assert "0.05" in sql
    assert "ORDER BY COALESCE(fp.overdue_days, 0) DESC" not in sql
