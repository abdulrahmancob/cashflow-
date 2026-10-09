"""Eligibility Sheet paid/denied overrides for the forecast."""

from datetime import date

import pandas as pd

from cashflow_forecast.sheet_visit_overrides import (
    apply_sheet_visit_overrides,
    apply_sheet_visit_overrides_reference,
    load_sheet_visit_overrides,
    remember_sheet_override,
    sheet_override_from_row,
    sheet_overrides_enabled,
    sheet_paid_visit_keys,
)


def _row(**kwargs):
    base = {
        "emr_patient_id": "100",
        "dos": date(2026, 6, 1),
        "source_visit_status": "paid",
        "total_amount": 100.0,
        "insurance_check_date": date(2026, 6, 10),
        "tracker_date": None,
        "posting_date_1": None,
        "insurance_name": "Aetna",
    }
    base.update(kwargs)
    return base


def _lines():
    return pd.DataFrame(
        [
            {
                "webpt_patient_id": "100",
                "date_of_service": date(2026, 6, 1),
                "cpt_code": "97110",
                "units": 1,
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": None,
                "source": "reconciliation",
            },
            {
                "webpt_patient_id": "100",
                "date_of_service": date(2026, 6, 1),
                "cpt_code": "97140",
                "units": 3,
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": None,
                "source": "reconciliation",
            },
            {
                "webpt_patient_id": "200",
                "date_of_service": date(2026, 6, 2),
                "cpt_code": "97110",
                "units": 1,
                "status": "paid",
                "paid_amount": 40.0,
                "eob_date": date(2026, 6, 5),
                "source": "reconciliation",
            },
            {
                "webpt_patient_id": "300",
                "date_of_service": date(2026, 6, 3),
                "cpt_code": "97110",
                "units": 1,
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": None,
                "source": "reconciliation",
            },
            {
                "webpt_patient_id": "400",
                "date_of_service": date(2026, 6, 4),
                "cpt_code": "97110",
                "units": 1,
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": None,
                "source": "reconciliation",
            },
        ]
    )


def test_fast_sheet_apply_matches_reference():
    overrides = {
        ("100", date(2026, 6, 1)): ("paid", 100.0, date(2026, 6, 10)),
        ("200", date(2026, 6, 2)): ("denied", 0.0, None),
        ("300", date(2026, 6, 3)): ("denied", 0.0, None),
        ("400", date(2026, 6, 4)): ("paid", 10.0, None),
    }
    fast = apply_sheet_visit_overrides(_lines(), overrides)
    slow = apply_sheet_visit_overrides_reference(_lines(), overrides)
    pd.testing.assert_frame_equal(fast.reset_index(drop=True), slow.reset_index(drop=True))


def test_waystar_paid_untouched_when_sheet_says_denied():
    overrides = {
        ("200", date(2026, 6, 2)): ("denied", 0.0, None),
    }
    out = apply_sheet_visit_overrides(_lines(), overrides)
    row = out[out["webpt_patient_id"] == "200"].iloc[0]
    assert row["status"] == "paid"
    assert row["paid_amount"] == 40.0
    assert row["source"] == "reconciliation"
    assert row["eob_date"] == date(2026, 6, 5)


def test_sheet_paid_splits_total_and_sets_eob():
    overrides = {
        ("100", date(2026, 6, 1)): ("paid", 100.0, date(2026, 6, 10)),
    }
    out = apply_sheet_visit_overrides(_lines(), overrides)
    visit = out[out["webpt_patient_id"] == "100"].sort_values("cpt_code")
    assert list(visit["status"]) == ["paid", "paid"]
    assert list(visit["source"]) == ["eligibility_sheet", "eligibility_sheet"]
    assert abs(float(visit["paid_amount"].sum()) - 100.0) < 0.01
    # 1 unit + 3 units -> 25 and 75
    assert list(visit["paid_amount"]) == [25.0, 75.0]
    assert list(visit["eob_date"]) == [date(2026, 6, 10), date(2026, 6, 10)]


def test_sheet_denied_zeros_the_visit():
    overrides = {
        ("300", date(2026, 6, 3)): ("denied", 0.0, None),
    }
    out = apply_sheet_visit_overrides(_lines(), overrides)
    row = out[out["webpt_patient_id"] == "300"].iloc[0]
    assert row["status"] == "denied"
    assert row["paid_amount"] == 0.0
    assert row["source"] == "eligibility_sheet"


def test_sheet_pending_stays_open():
    built = sheet_override_from_row(
        _row(emr_patient_id="400", dos=date(2026, 6, 4), source_visit_status="pending"),
        as_of=date(2026, 7, 1),
        backtest=False,
    )
    assert built is None
    out = apply_sheet_visit_overrides(_lines(), {})
    row = out[out["webpt_patient_id"] == "400"].iloc[0]
    assert row["status"] == "pending"
    assert row["paid_amount"] == 0.0
    assert row["source"] == "reconciliation"


def test_backtest_future_check_date_stays_open():
    future = sheet_override_from_row(
        _row(insurance_check_date=date(2026, 8, 1)),
        as_of=date(2026, 7, 1),
        backtest=True,
    )
    assert future is None
    live = sheet_override_from_row(
        _row(insurance_check_date=date(2026, 8, 1)),
        as_of=date(2026, 7, 1),
        backtest=False,
    )
    assert live is not None
    assert live[1][0] == "paid"


def test_backtest_paid_without_date_is_skipped_and_live_applies():
    undated = _row(insurance_check_date=None, tracker_date=None, posting_date_1=None)
    assert sheet_override_from_row(undated, as_of=date(2026, 7, 1), backtest=True) is None
    live = sheet_override_from_row(undated, as_of=date(2026, 7, 1), backtest=False)
    assert live is not None
    assert live[1] == ("paid", 100.0, None)


def test_eob_date_falls_through_tracker_then_posting():
    tracker = sheet_override_from_row(
        _row(insurance_check_date=None, tracker_date=date(2026, 6, 12)),
        as_of=date(2026, 7, 1),
        backtest=True,
    )
    assert tracker[1][2] == date(2026, 6, 12)
    posting = sheet_override_from_row(
        _row(
            insurance_check_date=None,
            tracker_date=None,
            posting_date_1="2026-06-15",
        ),
        as_of=date(2026, 7, 1),
        backtest=True,
    )
    assert posting[1][2] == date(2026, 6, 15)


def test_deduct_is_paid_and_denied_has_no_date_gate():
    deduct = sheet_override_from_row(
        _row(source_visit_status="Deduct", total_amount=12),
        as_of=date(2026, 7, 1),
        backtest=False,
    )
    assert deduct[1][0] == "paid"
    assert deduct[1][1] == 12.0
    denied = sheet_override_from_row(
        _row(source_visit_status="denied", insurance_check_date=None),
        as_of=date(2026, 7, 1),
        backtest=True,
    )
    assert denied[1] == ("denied", 0.0, None)


def test_paid_wins_when_the_same_visit_appears_twice():
    out = {}
    remember_sheet_override(out, ("100", date(2026, 6, 1)), ("denied", 0.0, None))
    remember_sheet_override(out, ("100", date(2026, 6, 1)), ("paid", 50.0, date(2026, 6, 2)))
    remember_sheet_override(out, ("100", date(2026, 6, 1)), ("denied", 0.0, None))
    assert out[("100", date(2026, 6, 1))][0] == "paid"
    assert sheet_paid_visit_keys(out) == {("100", date(2026, 6, 1))}


def test_sheet_paid_visits_drop_from_clinical_ar():
    from cashflow_forecast.__main__ import _exclude_recon_covered_ar

    ar = pd.DataFrame(
        [
            {
                "webpt_patient_id": "100",
                "date_of_service": date(2026, 1, 2),
                "cpt_code": "97110",
            },
            {
                "webpt_patient_id": "200",
                "date_of_service": date(2026, 1, 3),
                "cpt_code": "97110",
            },
        ]
    )
    out = _exclude_recon_covered_ar(
        ar,
        pd.DataFrame(),
        pd.DataFrame(),
        sheet_paid_visits={("100", date(2026, 1, 2))},
    )
    assert list(out["webpt_patient_id"]) == ["200"]


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("CASHFLOW_FORECAST_DISABLE_SHEET_OVERRIDES", raising=False)
    assert sheet_overrides_enabled() is True
    monkeypatch.setenv("CASHFLOW_FORECAST_DISABLE_SHEET_OVERRIDES", "1")
    assert sheet_overrides_enabled() is False
    assert load_sheet_visit_overrides(as_of=date(2026, 7, 1), backtest=False) == {}


def test_pack_call_drops_kwargs_the_deployed_function_lacks():
    from cashflow_forecast.__main__ import _holiday_shifts_from_schedules, _supported_kwargs

    def pack(outcomes, *, as_of, deposit_events):
        return outcomes, as_of, deposit_events

    kept = _supported_kwargs(
        pack,
        {"as_of": date(2026, 10, 6), "deposit_events": [], "stream_by_slot": {"x": 1}},
    )
    assert kept == {"as_of": date(2026, 10, 6), "deposit_events": []}

    class _Sch:
        holiday_shift = -1

    class _Bare:
        pass

    shifts = _holiday_shifts_from_schedules({"Aetna": _Sch(), "Other": _Bare(), "Skip": None})
    assert shifts == {"aetna": -1}


def test_list_forecast_sheet_visits_is_slim(monkeypatch):
    from cashflow_db.repository import eligibility

    row = _row(notes="drop me")
    monkeypatch.setattr(eligibility, "iter_export_work_items", lambda _conn: iter([row]))
    out = eligibility.list_forecast_sheet_visits(None)
    assert len(out) == 1
    assert out[0]["emr_patient_id"] == "100"
    assert out[0]["total_amount"] == 100.0
    assert "notes" not in out[0]
