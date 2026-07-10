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
    lookup = sla_lookup(sla)
    assert get_lag_days(lookup, "Healthfirst") == 9


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
