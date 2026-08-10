"""Tests for cashflow_forecast core logic."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from cashflow_forecast.audit_linker import link_audit_to_waystar, score_pair
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.forecast_engine import actual_cash_buckets, kpi_summary
from cashflow_forecast.outcome_stages import classify_outcomes
from cashflow_forecast.payer_sla import build_payer_sla, get_lag_days, sla_lookup
from cashflow_forecast.risk_flags import build_risk_flags
from cashflow_forecast.utils import normalize_name_key, parse_date, parse_money


def test_parse_money_and_date():
    assert parse_money("$1,234.50") == 1234.50
    assert parse_money("(35.40)") == -35.40
    assert parse_date("2026-06-09") == date(2026, 6, 9)
    assert parse_date("06/09/2026") == date(2026, 6, 9)
    assert parse_date("01/01/26") == date(2026, 1, 1)


def test_normalize_name_key():
    assert normalize_name_key("Smith, John") == normalize_name_key("JOHN SMITH")


def test_build_payer_sla_visit_level():
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "ins_name": "Healthfirst",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 6, 1),
                "eob_date": date(2026, 6, 10),
                "status": "paid",
                "paid_amount": 50.0,
                "cpt_code": "97110",
            },
            {
                "webpt_patient_id": "1",
                "ins_name": "Healthfirst",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 6, 1),
                "eob_date": date(2026, 6, 10),
                "status": "paid",
                "paid_amount": 40.0,
                "cpt_code": "97140",
            },
            {
                "webpt_patient_id": "2",
                "ins_name": "Healthfirst",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 6, 2),
                "eob_date": date(2026, 6, 12),
                "status": "paid",
                "paid_amount": 55.0,
                "cpt_code": "97110",
            },
            {
                "webpt_patient_id": "3",
                "ins_name": "Healthfirst",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 6, 3),
                "eob_date": date(2026, 6, 11),
                "status": "paid",
                "paid_amount": 60.0,
                "cpt_code": "97110",
            },
        ]
    )
    sla = build_payer_sla(lines)
    assert len(sla) == 1
    assert sla.iloc[0]["sample_count"] == 3  # visit-level, not 4 CPT lines
    assert sla.iloc[0]["median_lag_days"] == 9
    assert sla.iloc[0]["payer_org_code"] == "HEALTHFIRST"
    lookup = sla_lookup(sla)
    assert get_lag_days(lookup, "Healthfirst") == 9
    assert get_lag_days(lookup, "HEALTHFIRST") == 9


def test_outcome_not_replaced_by_risk():
    """Visit can be on_track AND have audit risk — risk is not an outcome stage."""
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "10",
                "patient_name": "Doe, Jane",
                "name_key": normalize_name_key("Doe, Jane"),
                "dob": "",
                "facility_name": "Bay Ridge",
                "ins_name": "Healthfirst",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 1),
                "cpt_code": "97110",
                "modifier": "GP",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
            }
        ]
    )
    fees = FeeEstimator()
    fees._global = 80.0
    outcomes = classify_outcomes(
        lines,
        sla_lookup={"healthfirst": 9},
        fee_estimator=fees,
        as_of=date(2026, 7, 5),
    )
    assert outcomes.iloc[0]["outcome_stage"] == "on_track"

    audit = pd.DataFrame(
        [
            {
                "patient_id": "10",
                "patient_name": "Doe, Jane",
                "name_key": normalize_name_key("Doe, Jane"),
                "date_of_service": date(2026, 7, 1),
                "insurance_name": "Healthfirst",
                "violation_type": "cpt",
                "rule_id": "estim_mismatch",
                "severity": "error",
                "cpt_codes": "97110",
            }
        ]
    )
    risk = build_risk_flags(outcomes, audit, fee_estimator=fees, as_of=date(2026, 7, 5))
    assert not risk.empty
    assert "audit_cpt" in risk["risk_flag"].values
    assert outcomes.iloc[0]["outcome_stage"] == "on_track"  # unchanged


def test_match_score_numeric():
    audit_row = pd.Series(
        {
            "name_key": "DOEJANE",
            "date_of_service": date(2026, 6, 1),
            "insurance_name": "Healthfirst Medicaid",
            "cpt_codes": "97110;97140",
        }
    )
    denial_row = pd.Series(
        {
            "name_key": "DOEJANE",
            "service_date": date(2026, 6, 1),
            "payer": "Healthfirst PHSP",
            "proc_code": "97110",
        }
    )
    score, signals = score_pair(audit_row, denial_row)
    assert score >= 70
    assert "name_exact" in signals
    assert "dos_exact" in signals


def test_fee_estimator():
    lines = pd.DataFrame(
        [
            {"status": "paid", "paid_amount": 40.0, "allowed_amount": 45.0, "cpt_code": "97110", "ins_name": "A"},
            {"status": "paid", "paid_amount": 50.0, "allowed_amount": 55.0, "cpt_code": "97110", "ins_name": "A"},
            {"status": "paid", "paid_amount": 30.0, "allowed_amount": 35.0, "cpt_code": "97140", "ins_name": "B"},
        ]
    )
    est = FeeEstimator.from_paid_lines(lines)
    assert est.estimate("97110", "A") == 50.0  # median allowed


def test_actual_cash_buckets():
    payments = pd.DataFrame(
        [
            {"paid_amount": 100.0, "eob_date": date(2026, 7, 1)},
            {"paid_amount": 50.0, "eob_date": date(2026, 7, 1)},
            {"paid_amount": 25.0, "eob_date": date(2026, 7, 2)},
        ]
    )
    buckets = actual_cash_buckets(payments)
    daily = buckets["daily"].set_index("period")["amount"]
    assert float(daily[date(2026, 7, 1)]) == 150.0
    assert float(daily[date(2026, 7, 2)]) == 25.0


def test_actual_cash_buckets_by_facility_fills_blanks():
    from cashflow_forecast.forecast_engine import actual_cash_buckets_by_facility

    payments = pd.DataFrame(
        [
            {
                "paid_amount": 100.0,
                "eob_date": date(2026, 7, 1),
                "webpt_patient_id": "1",
                "name_key": "SMITHJOHN",
                "facility_name": "",
            },
            {
                "paid_amount": 50.0,
                "eob_date": date(2026, 7, 1),
                "webpt_patient_id": "",
                "name_key": "DOEJANE",
                "facility_name": "",
            },
            {
                "paid_amount": 25.0,
                "eob_date": date(2026, 7, 2),
                "webpt_patient_id": "",
                "name_key": "UNKNOWN",
                "facility_name": "Astoria",
            },
        ]
    )
    lookup = pd.DataFrame(
        [
            {"webpt_patient_id": "1", "name_key": "SMITHJOHN", "facility_name": "Bedstuy"},
            {"webpt_patient_id": "2", "name_key": "DOEJANE", "facility_name": "Bushwick"},
        ]
    )
    buckets = actual_cash_buckets_by_facility(payments, facility_lookup=lookup)
    daily = buckets["daily"]
    by_fac = daily.set_index(["period", "facility_name"])["amount"]
    assert float(by_fac[(date(2026, 7, 1), "Bedstuy")]) == 100.0
    assert float(by_fac[(date(2026, 7, 1), "Bushwick")]) == 50.0
    assert float(by_fac[(date(2026, 7, 2), "Astoria")]) == 25.0


def test_actual_cash_buckets_from_deposits():
    from cashflow_forecast.forecast_engine import actual_cash_buckets_from_deposits

    deposits = pd.DataFrame(
        [
            {"deposit_date": date(2026, 7, 21), "amount": 120226.93},
            {"deposit_date": date(2026, 7, 21), "amount": 100.0},
            {"deposit_date": date(2026, 7, 20), "amount": 50.0},
        ]
    )
    buckets = actual_cash_buckets_from_deposits(deposits)
    daily = buckets["daily"].set_index("period")["amount"]
    assert abs(float(daily[date(2026, 7, 21)]) - 120326.93) < 0.01
    assert abs(float(daily[date(2026, 7, 20)]) - 50.0) < 0.01


def test_load_deposit_ledger_sample():
    from pathlib import Path

    from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger

    path = (
        Path(__file__).resolve().parents[2]
        / "cashflow_reconcile/tests/fixtures/transaction_tracker_sample.xlsx"
    )
    rows = load_deposit_ledger(path)
    assert rows
    assert all("deposit_date" in r and "amount" in r for r in rows)
    total = sum(float(r["amount"]) for r in rows)
    assert total > 0


def test_parse_frequency_and_duration():
    from cashflow_forecast.forward_volume import parse_duration_weeks, parse_frequency_per_week
    from cashflow_forecast.loaders.load_patients import parse_auth_remaining

    assert parse_frequency_per_week("3 times a week") == 3.0
    assert parse_frequency_per_week("2-3 times a week") == 2.5
    assert parse_frequency_per_week("1-2 per week") == 1.5
    assert parse_duration_weeks("12 weeks") == 12.0
    assert parse_duration_weeks("12 MONTH") == 48.0
    assert parse_auth_remaining("3 of 25 Authorized (Expires 12/31/2026)") == 22
    assert parse_auth_remaining("") is None


def test_august_poc_overlap_and_forward_lines():
    from cashflow_forecast.fee_estimator import FeeEstimator
    from cashflow_forecast.forward_volume import build_august_forward_lines

    plans = pd.DataFrame(
        [
            {
                "patient_id": "100",
                "poc_id": "PO1",
                "date_of_plan_of_care": date(2026, 7, 1),
                "frequency": "2 times a week",
                "duration": "8 weeks",
            },
            {
                "patient_id": "200",
                "poc_id": "PO2",
                "date_of_plan_of_care": date(2026, 1, 1),
                "frequency": "3 times a week",
                "duration": "4 weeks",  # ends before Aug
            },
        ]
    )
    patients = pd.DataFrame(
        [
            {
                "patient_id": "100",
                "patient_name": "Smith, Ann",
                "facility_name": "Bedstuy",
                "ins_name": "Healthfirst",
                "auth_remaining": 10,
            }
        ]
    )
    notes = pd.DataFrame()
    cpt = pd.DataFrame(
        [
            {
                "patient_id": "100",
                "date_of_daily_note": date(2026, 7, 10),
                "cpt_code": "97110",
                "modifier": "GP",
                "units": 2.0,
            },
            {
                "patient_id": "100",
                "date_of_daily_note": date(2026, 7, 10),
                "cpt_code": "97140",
                "modifier": "GP",
                "units": 1.0,
            },
        ]
    )
    fees = FeeEstimator()
    fees._global = 40.0
    fees._cpt = {"97110": 40.0, "97140": 30.0}

    lines, summary = build_august_forward_lines(
        plans, patients, notes, cpt, fee_estimator=fees
    )
    assert not lines.empty
    assert set(lines["webpt_patient_id"]) == {"100"}  # patient 200 expired
    assert (lines["source"] == "forward_poc").all()
    assert (lines["date_of_service"].map(lambda d: date(2026, 8, 1) <= d <= date(2026, 8, 31))).all()
    assert summary.iloc[0]["facility_name"] == "Bedstuy"
    assert summary.iloc[0]["projected_visit_count"] <= 10  # auth cap
    # per visit = 40*2 + 30*1 = 110
    assert lines.iloc[0]["precomputed_expected"] == 110.0


def test_may_ar_facility_join(tmp_path):
    from cashflow_forecast.loaders.load_extracted import load_may_ar_lines

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    pd.DataFrame(
        [
            {
                "patient_id": "1",
                "daily_note_id": "DN1",
                "date_of_daily_note": "2026-05-05",
                "patient_name": "Doe, Jane",
                "diagnosis_icd_codes": "M54.2",
                "insurance_name": "Healthfirst",
                "visit_no": "1",
                "note_file": "a.pdf",
                "modifier": "GP",
                "cpt_code": "97110",
                "billing_modifier_suffix": "",
                "modifier_cpt": "GP:97110",
                "units": "2",
                "description": "ex",
            },
            {
                "patient_id": "1",
                "daily_note_id": "DN2",
                "date_of_daily_note": "2026-06-05",
                "patient_name": "Doe, Jane",
                "diagnosis_icd_codes": "M54.2",
                "insurance_name": "Healthfirst",
                "visit_no": "2",
                "note_file": "b.pdf",
                "modifier": "GP",
                "cpt_code": "97110",
                "billing_modifier_suffix": "",
                "modifier_cpt": "GP:97110",
                "units": "1",
                "description": "ex",
            },
        ]
    ).to_csv(extracted / "cpt_codes.csv", index=False)
    pd.DataFrame(
        [
            {
                "patient_id": "1",
                "daily_note_id": "DN1",
                "note_file": "a.pdf",
                "facility_name": "Bushwick",
                "patient_name": "Doe, Jane",
                "date_of_daily_note": "2026-05-05",
                "date_of_birth": "1990-01-01",
                "insurance_name": "Healthfirst",
            }
        ]
    ).to_csv(extracted / "daily_notes.csv", index=False)
    may = load_may_ar_lines(extracted)
    assert len(may) == 1
    assert may.iloc[0]["facility_name"] == "Bushwick"
    assert may.iloc[0]["date_of_service"] == date(2026, 5, 5)
    assert may.iloc[0]["status"] == "pending"
    assert may.iloc[0]["units"] == 2.0
    assert may.iloc[0]["source"] == "extracted_ar"


def test_dimensional_projected_buckets():
    from cashflow_forecast.forecast_engine import (
        filter_period_to_window,
        projected_cash_buckets_by_facility,
        projected_cash_buckets_by_insurance,
    )

    outcomes = pd.DataFrame(
        [
            {
                "outcome_stage": "on_track",
                "forecast_date": date(2026, 6, 15),
                "expected_amount": 100.0,
                "ins_name": "Aetna",
                "facility_name": "Bedstuy",
            },
            {
                "outcome_stage": "overdue",
                "forecast_date": date(2026, 7, 1),
                "expected_amount": 50.0,
                "ins_name": "Aetna",
                "facility_name": "Bushwick",
            },
            {
                "outcome_stage": "on_track",
                "forecast_date": date(2026, 9, 1),
                "expected_amount": 999.0,
                "ins_name": "Aetna",
                "facility_name": "Bedstuy",
            },
        ]
    )
    by_ins = projected_cash_buckets_by_insurance(outcomes)
    monthly = by_ins["monthly"]
    assert "ins_name" in monthly.columns
    jun = monthly[monthly["period"] == "2026-06"]
    assert float(jun["amount"].sum()) == 100.0

    by_fac = projected_cash_buckets_by_facility(outcomes)
    filtered = filter_period_to_window(by_fac["monthly"])
    assert "2026-09" not in set(filtered["period"])
    assert float(filtered["amount"].sum()) == 150.0


def test_units_and_precomputed_in_outcomes():
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "patient_name": "A, B",
                "name_key": "AB",
                "dob": "",
                "facility_name": "X",
                "ins_name": "Ins",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 10),
                "cpt_code": "97110",
                "modifier": "GP",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
                "units": 2.0,
                "source": "extracted_ar",
            },
            {
                "webpt_patient_id": "2",
                "patient_name": "C, D",
                "name_key": "CD",
                "dob": "",
                "facility_name": "Y",
                "ins_name": "Ins",
                "insurance_revflow": "",
                "date_of_service": date(2026, 8, 5),
                "cpt_code": "97110",
                "modifier": "GP",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
                "units": 1.0,
                "precomputed_expected": 125.0,
                "source": "forward_poc",
            },
        ]
    )
    fees = FeeEstimator()
    fees._global = 40.0
    out = classify_outcomes(
        lines, sla_lookup={"ins": 14}, fee_estimator=fees, as_of=date(2026, 7, 17)
    )
    may_row = out[out["source"] == "extracted_ar"].iloc[0]
    assert may_row["expected_amount"] == 80.0  # 40 * 2 units
    fwd_row = out[out["source"] == "forward_poc"].iloc[0]
    assert fwd_row["expected_amount"] == 125.0


def test_sf_visit_overrides_paid_split_and_denied():
    from cashflow_forecast.sf_visit_overrides import apply_sf_visit_overrides

    lines = pd.DataFrame(
        [
            {
                "patient_name": "Doe, Jane",
                "name_key": "DOEJANE",
                "date_of_service": date(2026, 6, 1),
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": date(2026, 6, 10),
                "source": "reconciliation",
            },
            {
                "patient_name": "Doe, Jane",
                "name_key": "DOEJANE",
                "date_of_service": date(2026, 6, 1),
                "cpt_code": "97140",
                "status": "pending",
                "paid_amount": 0.0,
                "eob_date": date(2026, 6, 10),
                "source": "reconciliation",
            },
            {
                "patient_name": "Smith, Bob",
                "name_key": "SMITHBOB",
                "date_of_service": date(2026, 6, 2),
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "source": "reconciliation",
            },
        ]
    )
    overrides = {
        ("DOEJANE", date(2026, 6, 1)): ("paid", 100.0),
        ("SMITHBOB", date(2026, 6, 2)): ("denied", 0.0),
    }
    out = apply_sf_visit_overrides(lines, overrides)
    doe = out[out["name_key"] == "DOEJANE"]
    assert (doe["status"] == "paid").all()
    assert abs(float(doe["paid_amount"].sum()) - 100.0) < 0.01
    assert (doe["source"] == "sf_override").all()

    bob = out[out["name_key"] == "SMITHBOB"].iloc[0]
    assert bob["status"] == "denied"
    assert bob["paid_amount"] == 0.0

    fees = FeeEstimator()
    fees._global = 50.0
    classified = classify_outcomes(
        out, sla_lookup={}, fee_estimator=fees, as_of=date(2026, 7, 17)
    )
    assert set(classified.loc[classified["name_key"] == "DOEJANE", "outcome_stage"]) == {"paid"}
    assert classified.loc[classified["name_key"] == "SMITHBOB", "outcome_stage"].iloc[0] == "denied"
    assert classified.loc[classified["name_key"] == "SMITHBOB", "expected_amount"].iloc[0] == 0.0


def test_remap_emr_overrides_to_name_keys():
    from cashflow_forecast.sf_visit_overrides import remap_emr_overrides_to_name_keys

    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "12345",
                "patient_name": "Doe, Jane",
                "name_key": "DOEJANE",
                "date_of_service": date(2026, 6, 1),
            },
            {
                "webpt_patient_id": "99999",
                "patient_name": "Other, Person",
                "name_key": "OTHERPERSON",
                "date_of_service": date(2026, 6, 1),
            },
        ]
    )
    emr_overrides = {
        ("12345", date(2026, 6, 1)): ("paid", 80.0),
        ("88888", date(2026, 6, 1)): ("denied", 0.0),
    }
    out = remap_emr_overrides_to_name_keys(lines, emr_overrides)
    assert out == {("DOEJANE", date(2026, 6, 1)): ("paid", 80.0)}


def test_cash_velocity_overrides_dos_eob_lag(tmp_path):
    from cashflow_forecast.insurance_behavior_sla import (
        load_cash_velocity_lookup,
        merge_velocity_into_lookup,
    )

    summary = tmp_path / "payor_behavior_summary.csv"
    summary.write_text(
        "payor,dominant_ins_name,eob_to_deposit_n,cash_velocity_median\n"
        '"HEALTHFIRST PHSP, INC.",Healthfirst,10,13\n'
        "TINY PAYOR,Tiny Ins,1,99\n",
        encoding="utf-8",
    )
    velocity = load_cash_velocity_lookup(summary)
    assert velocity["healthfirst"] == 13
    assert velocity["healthfirst phsp, inc."] == 13
    assert "tiny ins" not in velocity  # below MIN_SLA_SAMPLES

    base = {"healthfirst": 9, "other": 14}
    merged = merge_velocity_into_lookup(base, velocity)
    assert get_lag_days(merged, "Healthfirst") == 13  # velocity wins
    assert get_lag_days(merged, "other") == 14


def test_parse_cadence_and_snap_helpers():
    from cashflow_forecast.insurance_behavior_sla import (
        parse_cadence_weekdays,
        snap_to_deposit_weekdays,
    )

    assert parse_cadence_weekdays("weekly_fri") == frozenset({4})
    assert parse_cadence_weekdays("biweekly_tue") == frozenset({1})
    assert parse_cadence_weekdays("multi_weekday_fri_tue") == frozenset({4, 1})
    assert parse_cadence_weekdays("near_daily") == frozenset()
    assert parse_cadence_weekdays("irregular") == frozenset()

    # Thursday 2026-07-23 → Friday
    assert snap_to_deposit_weekdays(date(2026, 7, 23), frozenset({4})) == date(2026, 7, 24)
    # Wednesday → next Fri in {Fri, Tue}
    assert snap_to_deposit_weekdays(date(2026, 7, 22), frozenset({4, 1})) == date(2026, 7, 24)
    # Already Friday → unchanged
    assert snap_to_deposit_weekdays(date(2026, 7, 24), frozenset({4})) == date(2026, 7, 24)


def test_forecast_date_snaps_to_weekly_friday():
    """Healthfirst-style: DOS+lag lands Thu → snap forecast_date to Fri."""
    from cashflow_forecast.insurance_behavior_sla import DepositSchedule

    # DOS 2026-07-10 (Fri) + 13 days = 2026-07-23 (Thu)
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "patient_name": "Doe, Jane",
                "name_key": normalize_name_key("Doe, Jane"),
                "facility_name": "Bay Ridge",
                "ins_name": "Healthfirst-Medicaid",
                "insurance_revflow": "HEALTHFIRST PHSP, INC.",
                "date_of_service": date(2026, 7, 10),
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
            }
        ]
    )
    fees = FeeEstimator()
    fees._global = 80.0
    schedule = DepositSchedule(allowed_weekdays=frozenset({4}), cadence="weekly_fri")
    outcomes = classify_outcomes(
        lines,
        sla_lookup={"healthfirst-medicaid": 13},
        fee_estimator=fees,
        as_of=date(2026, 7, 17),
        deposit_schedule_lookup={"healthfirst-medicaid": schedule},
    )
    row = outcomes.iloc[0]
    assert row["expected_pay_date"] == date(2026, 7, 23)  # raw DOS+lag (Thu)
    assert row["forecast_date"] == date(2026, 7, 24)  # snapped Fri
    assert row["deposit_snap_days"] == 1


def test_forecast_date_multi_weekday_and_near_daily():
    from cashflow_forecast.insurance_behavior_sla import DepositSchedule

    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "patient_name": "A, A",
                "name_key": "AA",
                "facility_name": "X",
                "ins_name": "1199",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 5),  # Sun
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
            },
            {
                "webpt_patient_id": "2",
                "patient_name": "B, B",
                "name_key": "BB",
                "facility_name": "X",
                "ins_name": "Aetna",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 5),
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0.0,
                "allowed_amount": 0.0,
                "eob_date": None,
            },
        ]
    )
    fees = FeeEstimator()
    fees._global = 50.0
    # DOS + 17 = 2026-07-22 (Wed). multi fri/tue → Fri 7/24
    schedules = {
        "1199": DepositSchedule(
            allowed_weekdays=frozenset({4, 1}), cadence="multi_weekday_fri_tue"
        ),
        "aetna": DepositSchedule(allowed_weekdays=frozenset(), cadence="near_daily"),
    }
    # near_daily with empty allowed → no snap via schedule.snaps being False
    # Actually DepositSchedule with empty frozenset has snaps=False; get_deposit_schedule
    # only loads rows with allowed days. Simulate missing snap by omitting aetna.
    outcomes = classify_outcomes(
        lines,
        sla_lookup={"1199": 17, "aetna": 17},
        fee_estimator=fees,
        as_of=date(2026, 7, 10),
        deposit_schedule_lookup={"1199": schedules["1199"]},
    )
    by_ins = {r["ins_name"]: r for _, r in outcomes.iterrows()}
    assert by_ins["1199"]["expected_pay_date"] == date(2026, 7, 22)
    assert by_ins["1199"]["forecast_date"] == date(2026, 7, 24)
    assert by_ins["1199"]["deposit_snap_days"] == 2
    assert by_ins["Aetna"]["forecast_date"] == date(2026, 7, 22)
    assert by_ins["Aetna"]["deposit_snap_days"] == 0


def test_load_deposit_schedule_lookup(tmp_path):
    from cashflow_forecast.insurance_behavior_sla import load_deposit_schedule_lookup

    summary = tmp_path / "payor_behavior_summary.csv"
    summary.write_text(
        "payor,dominant_ins_name,payer_org_code,payer_org,cadence,"
        "top_deposit_weekday,top_deposit_weekday_pct\n"
        '"HEALTHFIRST PHSP, INC.",Healthfirst-Medicaid,HEALTHFIRST,Healthfirst,'
        "weekly_fri,Fri,96.3\n"
        "AETNA,Aetna,AETNA,Aetna,near_daily,Tue,37.4\n"
        "1199SEIU,1199,SEIU1199,1199SEIU,multi_weekday_fri_tue,Fri,45.3\n",
        encoding="utf-8",
    )
    lookup = load_deposit_schedule_lookup(summary)
    assert "healthfirst-medicaid" in lookup
    assert lookup["healthfirst-medicaid"].allowed_weekdays == frozenset({4})
    assert "healthfirst phsp, inc." in lookup
    assert "1199" in lookup
    assert lookup["1199"].allowed_weekdays == frozenset({4, 1})
    assert "aetna" not in lookup  # near_daily row skipped
    assert "seiu1199" not in lookup  # payer_org_code is not indexed
