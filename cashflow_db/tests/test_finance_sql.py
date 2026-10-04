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
        captured["sql"] = sql
        captured["params"] = params
        return rows

    monkeypatch.setattr(forecast_repo.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(forecast_repo.client, "fetchall", fake_fetchall)
    return captured


def test_overdue_grid_uses_land_date_and_age_buckets(monkeypatch):
    captured = _patch(monkeypatch, [])
    forecast_repo.overdue_grid(
        None,  # type: ignore[arg-type]
        d0=date(2026, 8, 1),
        d1=date(2026, 8, 31),
        facilities=["Bedstuy"],
        insurers=["1199"],
    )
    sql = str(captured["sql"])
    params = list(captured["params"])
    assert "outcome_stage = 'overdue'" in sql
    assert "original_forecast_date" in sql
    assert "expected_pay_date" in sql
    assert "YYYY-MM" in sql
    assert "'0_14'" in sql
    assert "fp.payload->>'facility_name'" in sql
    assert "GROUP BY 1, 2, 3, 4, 5" in sql
    assert date(2026, 8, 1) in params
    assert ["Bedstuy"] in params
    assert ["1199"] in params


def test_ar_trend_picks_one_success_run_per_week(monkeypatch):
    captured = _patch(
        monkeypatch,
        [{"as_of": date(2026, 9, 1), "overdue": 5, "on_track": 2, "overdue_90_plus": 1}],
    )
    rows = forecast_repo.ar_trend(
        None,  # type: ignore[arg-type]
        insurers=["1199"],
        weeks=12,
    )
    sql = str(captured["sql"])
    params = list(captured["params"])
    assert "DISTINCT ON" in sql
    assert "date_trunc('week'" in sql
    assert "status = 'success'" in sql
    assert "LEFT JOIN" in sql
    assert "overdue_days, 0) > 90" in sql
    assert "date_of_service >=" not in sql
    assert params[0] == 12
    assert ["1199"] in params
    assert rows[0]["as_of"] == "2026-09-01"
    assert rows[0]["overdue"] == 5.0


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
    assert "0.85" in sql
    assert "0.35" in sql
    assert "0.05" in sql
    assert "ORDER BY COALESCE(fp.overdue_days, 0) DESC" not in sql
