"""Equivalence of the fast forecast path, and the replay keep rule."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from cashflow_db.repository.visits import get_service_lines_for_reconcile
from cashflow_forecast.payer_payment_model import (
    apply_visit_expected_amounts,
    apply_visit_expected_amounts_reference,
    learn_payment_models,
    learn_payment_models_reference,
)
from cashflow_forecast.replay_history import (
    cairo_pause_seconds,
    close_tolerance,
    forecasts_close,
    load_done_dates,
    replay_created_at,
)


def _paid_lines(n: int = 40) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "webpt_patient_id": str(i),
                "date_of_service": date(2026, 6, 1) + timedelta(days=i % 20),
                "ins_name": "1199",
                "insurance_revflow": "1199",
                "cpt_code": "97110",
                "status": "paid",
                "paid_amount": 50.0,
                "billed_amount": 80.0,
                "allowed_amount": 60.0,
                "units": 1,
                "visit_paid_total": 50.0,
            }
        )
    rows.append(
        {
            "webpt_patient_id": "extra",
            "date_of_service": date(2026, 7, 1),
            "ins_name": "1199",
            "insurance_revflow": "1199",
            "cpt_code": "97140",
            "status": "paid",
            "paid_amount": 20.0,
            "billed_amount": 30.0,
            "allowed_amount": 25.0,
            "units": 2,
            "visit_paid_total": 70.0,
        }
    )
    rows.append(
        {
            "webpt_patient_id": "extra",
            "date_of_service": date(2026, 7, 1),
            "ins_name": "1199",
            "insurance_revflow": "1199",
            "cpt_code": "97110",
            "status": "paid",
            "paid_amount": 50.0,
            "billed_amount": 80.0,
            "allowed_amount": 60.0,
            "units": 1,
            "visit_paid_total": 70.0,
        }
    )
    return pd.DataFrame(rows)


def _model_rows(catalog):
    rows = []
    for key, model in sorted(catalog.models.items()):
        rows.append(
            (
                key,
                model.model_type,
                round(model.flat_amount, 2),
                round(model.percent, 4),
                tuple(sorted(model.adders.items())),
                model.n_visits,
            )
        )
    return rows


def test_payment_models_match_reference():
    lines = _paid_lines()
    fast = learn_payment_models(lines)
    slow = learn_payment_models_reference(lines)
    assert _model_rows(fast) == _model_rows(slow)


def test_expected_amounts_match_reference():
    lines = _paid_lines()
    catalog = learn_payment_models_reference(lines)
    pending = pd.DataFrame(
        [
            {
                "webpt_patient_id": "p",
                "date_of_service": date(2026, 8, 1),
                "ins_name": "1199",
                "insurance_revflow": "1199",
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "units": 1,
                "billed_amount": 80.0,
                "allowed_amount": 60.0,
            },
            {
                "webpt_patient_id": "p",
                "date_of_service": date(2026, 8, 1),
                "ins_name": "1199",
                "insurance_revflow": "1199",
                "cpt_code": "97140",
                "status": "pending",
                "paid_amount": 0.0,
                "units": 3,
                "billed_amount": 40.0,
                "allowed_amount": 30.0,
            },
        ]
    )
    fast = apply_visit_expected_amounts(pending, catalog)
    slow = apply_visit_expected_amounts_reference(pending, catalog)
    pd.testing.assert_series_equal(
        fast["precomputed_expected"].astype(float),
        slow["precomputed_expected"].astype(float),
    )


def test_close_rule_is_max_of_two_percent_and_floor():
    assert forecasts_close(100, 100)
    assert forecasts_close(102, 100)
    assert close_tolerance(100) == 25_000
    assert forecasts_close(25_000, 0)
    assert forecasts_close(25_001, 0) is False
    old = 2_000_000
    assert close_tolerance(old) == old * 0.02
    assert forecasts_close(old + old * 0.02, old)
    assert forecasts_close(old + old * 0.02 + 0.01, old) is False


def test_replay_created_at_is_end_of_that_day_utc():
    stamp = replay_created_at(date(2026, 8, 1))
    assert stamp == datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc)
    assert stamp < datetime(2026, 10, 6, 0, 59, tzinfo=timezone.utc)


def test_cairo_pause_covers_nightly_window():
    inside = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)  # 03:00 Cairo
    assert cairo_pause_seconds(inside) > 0
    outside = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert cairo_pause_seconds(outside) == 0


def test_done_log_skips_decided_days():
    text = '\n'.join(
        [
            '{"as_of":"2026-08-01","decision":"keep-old"}',
            '{"as_of":"2026-08-02","decision":"replace"}',
            '{"as_of":"2026-08-03","decision":"insert"}',
            '{"as_of":"2026-08-04","decision":"failed"}',
        ]
    )
    assert load_done_dates(text) == {"2026-08-01", "2026-08-02", "2026-08-03"}


def test_old_ar_sql_excludes_recon_lines():
    source = get_service_lines_for_reconcile.__doc__ or ""
    import inspect

    body = inspect.getsource(get_service_lines_for_reconcile)
    assert "NOT EXISTS" in body
    assert "billing.reconciliation_line" in body
    assert "exclude_reconciliation_run_id" in body
    assert source
