"""Bank business day + cadence-aware + weekend historical spill."""

from __future__ import annotations

from datetime import date

import pandas as pd

from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.insurance_behavior_sla import (
    DepositSchedule,
    parse_cadence_weekdays,
    snap_to_bank_business_day,
    snap_to_deposit_weekdays,
    snap_weekend_by_historical_weekday,
)
from cashflow_forecast.outcome_stages import classify_outcomes


def test_snap_to_bank_business_day_weekend_to_monday():
    sat = date(2026, 7, 25)
    sun = date(2026, 7, 26)
    assert snap_to_bank_business_day(sat) == date(2026, 7, 27)
    assert snap_to_bank_business_day(sun) == date(2026, 7, 27)


def test_snap_to_bank_business_day_friday_unchanged():
    fri = date(2026, 7, 24)
    assert snap_to_bank_business_day(fri) == fri


def test_parse_cadence_drops_weekend_tokens():
    assert parse_cadence_weekdays("weekly_sat") == frozenset()
    assert parse_cadence_weekdays("weekly_fri") == frozenset({4})
    assert parse_cadence_weekdays("multi_weekday_tue_thu") == frozenset({1, 3})


def test_weekly_fri_saturday_raw_goes_to_friday_not_monday():
    sat = date(2026, 7, 25)
    snapped = snap_to_deposit_weekdays(sat, frozenset({4}))
    assert snapped == date(2026, 7, 31)
    assert snapped.weekday() == 4


def test_multi_tue_thu_saturday_raw_goes_to_tuesday():
    sat = date(2026, 7, 25)
    snapped = snap_to_deposit_weekdays(sat, frozenset({1, 3}))
    assert snapped == date(2026, 7, 28)
    assert snapped.weekday() == 1


def test_weekend_spill_historical_prefers_thursday():
    sat = date(2026, 7, 25)
    probs = {0: 0.10, 1: 0.35, 2: 0.10, 3: 0.40, 4: 0.05}  # Thu dominant
    result = snap_weekend_by_historical_weekday(
        sat, probs, spill_method="historical", floor=date(2026, 7, 24)
    )
    assert result.forecast_date == date(2026, 7, 30)  # Thursday
    assert result.spill_method == "historical"


def test_weekend_spill_uniform_picks_monday_floor():
    sat = date(2026, 7, 25)
    result = snap_weekend_by_historical_weekday(sat, None, spill_method="uniform")
    assert result.forecast_date == date(2026, 7, 27)  # Monday bank floor
    assert result.spill_method == "uniform"


def test_classify_near_daily_saturday_expected_lands_monday_uniform():
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "patient_name": "Test Patient",
                "dob": "1990-01-01",
                "facility_name": "Clinic",
                "ins_name": "NYCE PPO",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 24),
                "cpt_code": "97110",
                "modifier": "",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 100.0,
                "billed_amount": 120.0,
                "eob_date": None,
                "units": 1,
                "source": "recon",
            }
        ]
    )
    fee = FeeEstimator()
    fee._global = 50.0
    out = classify_outcomes(
        lines,
        as_of=date(2026, 7, 24),
        fee_estimator=fee,
        sla_lookup={"nyce ppo": 1},
        deposit_schedule_lookup={},
        weekday_probs_by_grain={},
        global_weekday_probs={d: 0.2 for d in range(5)},
    )
    fd = out.iloc[0]["forecast_date"]
    assert fd == date(2026, 7, 27)
    assert fd.weekday() < 5


def test_classify_near_daily_saturday_with_thu_history():
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "patient_name": "Test Patient",
                "dob": "1990-01-01",
                "facility_name": "Clinic",
                "ins_name": "NYCE PPO",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 24),
                "cpt_code": "97110",
                "modifier": "",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 100.0,
                "billed_amount": 120.0,
                "eob_date": None,
                "units": 1,
                "source": "recon",
            }
        ]
    )
    fee = FeeEstimator()
    fee._global = 50.0
    # plan grain key from resolve_payer_plan("NYCE PPO")
    from cashflow_forecast.payer_plan import resolve_payer_plan

    key = resolve_payer_plan("NYCE PPO")
    grain = f"plan:{key.plan_key}"
    out = classify_outcomes(
        lines,
        as_of=date(2026, 7, 24),
        fee_estimator=fee,
        sla_lookup={"nyce ppo": 1},
        deposit_schedule_lookup={},
        weekday_probs_by_grain={
            grain: {0: 0.1, 1: 0.2, 2: 0.1, 3: 0.5, 4: 0.1},
        },
        global_weekday_probs={d: 0.2 for d in range(5)},
    )
    fd = out.iloc[0]["forecast_date"]
    assert fd.weekday() == 3  # Thursday


def test_classify_weekly_fri_saturday_raw_lands_friday():
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "2",
                "patient_name": "Fri Payer",
                "dob": "1990-01-01",
                "facility_name": "Clinic",
                "ins_name": "Weekly Fri Plan",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 24),
                "cpt_code": "97110",
                "modifier": "",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 100.0,
                "billed_amount": 120.0,
                "eob_date": None,
                "units": 1,
                "source": "recon",
            }
        ]
    )
    fee = FeeEstimator()
    fee._global = 50.0
    schedules = {
        "weekly fri plan": DepositSchedule(
            allowed_weekdays=frozenset({4}), cadence="weekly_fri"
        )
    }
    out = classify_outcomes(
        lines,
        as_of=date(2026, 7, 24),
        fee_estimator=fee,
        sla_lookup={"weekly fri plan": 1},
        deposit_schedule_lookup=schedules,
    )
    fd = out.iloc[0]["forecast_date"]
    assert fd == date(2026, 7, 31)
    assert fd.weekday() == 4


def test_healthfirst_eob_plus_deposit_lag_snaps_to_friday():
    """EOB Wednesday + eob_to_deposit=2 → Friday land (Tracker deposit day)."""
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "hf1",
                "patient_name": "HF Patient",
                "dob": "1990-01-01",
                "facility_name": "Clinic",
                "ins_name": "Healthfirst-Medicaid",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 7, 10),
                "cpt_code": "97110",
                "modifier": "",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 100.0,
                "billed_amount": 120.0,
                "eob_date": date(2026, 7, 29),  # Wednesday
                "units": 1,
                "source": "recon",
            }
        ]
    )
    fee = FeeEstimator()
    fee._global = 50.0
    schedules = {
        "healthfirst phsp, inc.": DepositSchedule(
            allowed_weekdays=frozenset({4}), cadence="weekly_fri"
        ),
        "healthfirst-medicaid": DepositSchedule(
            allowed_weekdays=frozenset({4}), cadence="weekly_fri"
        ),
    }
    out = classify_outcomes(
        lines,
        as_of=date(2026, 7, 30),
        fee_estimator=fee,
        sla_lookup={"healthfirst-medicaid": 11},
        deposit_schedule_lookup=schedules,
        eob_to_deposit_lookup={
            "healthfirst phsp, inc.": 2,
            "healthfirst-medicaid": 2,
        },
    )
    fd = out.iloc[0]["forecast_date"]
    assert fd == date(2026, 7, 31)
    assert fd.weekday() == 4
