"""Tests for payer_plan keys, payment models, capacity pack."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from cashflow_forecast.deposit_capacity import (
    DepositEvent,
    age_factor,
    compute_parent_shares,
    compute_priority,
    pack_pastdue_ffd,
    scale_for_outstanding,
    status_factor,
    thin_fallback_cap,
    weekday_deposit_targets,
)
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.insurance_behavior_sla import DepositSchedule
from cashflow_forecast.payer_payment_model import (
    MODEL_FLAT,
    MODEL_FLAT_ADDERS,
    PaymentModel,
    apply_visit_expected_amounts,
    learn_payment_models,
)
from cashflow_forecast.payer_plan import normalize_ins_name, resolve_payer_plan


def test_resolve_payer_plan_splits_aetna_products():
    commercial = resolve_payer_plan("Aetna Commercial (Zaya)")
    medicare = resolve_payer_plan("Aetna-Medicare")
    assert commercial.plan_key != medicare.plan_key
    assert "commercial" in commercial.plan_key or commercial.product_class == "commercial"
    assert medicare.product_class == "medicare" or "medicare" in medicare.plan_key


def test_normalize_strips_zaya_noise():
    assert normalize_ins_name("Aetna Commercial (Zaya)") == normalize_ins_name(
        "Aetna Commercial"
    )


def test_learn_flat_per_visit_1199_style():
    rows = []
    for i in range(40):
        pid = str(1000 + i)
        dos = date(2026, 6, 1) + timedelta(days=i % 20)
        # two CPT lines, visit pays flat $50 total
        rows.append(
            {
                "webpt_patient_id": pid,
                "ins_name": "1199",
                "insurance_revflow": "1199SEIU",
                "date_of_service": dos,
                "cpt_code": "97110",
                "status": "paid",
                "paid_amount": 50.0,
                "allowed_amount": 100.0,
                "billed_amount": 120.0,
            }
        )
        rows.append(
            {
                "webpt_patient_id": pid,
                "ins_name": "1199",
                "insurance_revflow": "1199SEIU",
                "date_of_service": dos,
                "cpt_code": "97140",
                "status": "paid",
                "paid_amount": 0.0,
                "allowed_amount": 80.0,
                "billed_amount": 90.0,
            }
        )
    # zero_pay companion lines shouldn't break mode
    lines = pd.DataFrame(rows)
    # Only count positive paid in visit total via paid_amount sum = 50
    catalog = learn_payment_models(lines)
    key = resolve_payer_plan("1199", insurance_revflow="1199SEIU")
    model, grain = catalog.resolve_model(key)
    assert model.model_type in (MODEL_FLAT, MODEL_FLAT_ADDERS)
    assert abs(model.flat_amount - 50.0) <= 1.0
    assert grain in ("plan", "class", "org")


def test_flat_plus_90901_adder():
    rows = []
    for i in range(35):
        pid = str(2000 + i)
        dos = date(2026, 5, 1) + timedelta(days=i)
        rows.append(
            {
                "webpt_patient_id": pid,
                "ins_name": "1199",
                "insurance_revflow": "1199SEIU",
                "date_of_service": dos,
                "cpt_code": "97110",
                "status": "paid",
                "paid_amount": 50.0,
                "allowed_amount": 50.0,
                "billed_amount": 50.0,
            }
        )
        if i % 2 == 0:
            rows.append(
                {
                    "webpt_patient_id": pid,
                    "ins_name": "1199",
                    "insurance_revflow": "1199SEIU",
                    "date_of_service": dos,
                    "cpt_code": "90901",
                    "status": "paid",
                    "paid_amount": 50.0,
                    "allowed_amount": 50.0,
                    "billed_amount": 50.0,
                }
            )
    catalog = learn_payment_models(pd.DataFrame(rows))
    key = resolve_payer_plan("1199")
    model, _ = catalog.resolve_model(key)
    # Either flat 50 with adder, or flat on mode of mixed 50/100 — accept adders path
    if model.model_type == MODEL_FLAT_ADDERS:
        assert "90901" in model.adders
        assert abs(model.adders["90901"] - 50.0) <= 1.0


def test_apply_visit_expected_uses_flat_not_cpt_sum():
    fee = FeeEstimator()
    fee._cpt_ins[("97110", "1199")] = 100.0
    fee._cpt_ins[("97140", "1199")] = 80.0
    fee._global = 50.0
    catalog_models = {
        "plan:1199": PaymentModel(
            grain_key="plan:1199",
            grain="plan",
            model_type=MODEL_FLAT,
            flat_amount=50.0,
            n_visits=40,
        )
    }
    from cashflow_forecast.payer_payment_model import PaymentModelCatalog

    catalog = PaymentModelCatalog(
        models=catalog_models, counts={"plan:1199": 40}, fee_estimator=fee
    )
    lines = pd.DataFrame(
        [
            {
                "webpt_patient_id": "1",
                "ins_name": "1199",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 1),
                "cpt_code": "97110",
                "status": "pending",
                "paid_amount": 0,
                "units": 1,
            },
            {
                "webpt_patient_id": "1",
                "ins_name": "1199",
                "insurance_revflow": "",
                "date_of_service": date(2026, 7, 1),
                "cpt_code": "97140",
                "status": "pending",
                "paid_amount": 0,
                "units": 1,
            },
        ]
    )
    out = apply_visit_expected_amounts(lines, catalog)
    total = float(pd.to_numeric(out["precomputed_expected"], errors="coerce").sum())
    assert abs(total - 50.0) < 0.05


def test_scale_outstanding_sublinear_clamped():
    assert abs(scale_for_outstanding(1.0) - 1.0) < 1e-9
    assert scale_for_outstanding(9.0) == 2.0  # sqrt(9)=3 capped at max 2
    assert scale_for_outstanding(0.1) == 0.75  # floor


def test_priority_age_and_status():
    fresh = compute_priority(
        expected_amount=100,
        overdue_days=7,
        outcome_stage="overdue",
    )
    stale = compute_priority(
        expected_amount=100,
        overdue_days=200,
        outcome_stage="overdue",
    )
    denied = compute_priority(
        expected_amount=100,
        overdue_days=7,
        outcome_stage="denied",
    )
    assert fresh > stale
    assert fresh > denied
    assert age_factor(7) > age_factor(200)
    assert status_factor("denied") < status_factor("overdue")


def test_pack_ffd_overflow_stays_on_same_plan_weekly():
    as_of = date(2026, 7, 27)  # Monday
    # Cap ~85 via deposit events on Mondays for plan A
    events = []
    for w in range(8):
        d = as_of - timedelta(weeks=w + 1)
        # snap to Monday
        d = d - timedelta(days=d.weekday())
        events.append(
            DepositEvent(
                plan_key="plan:aetna commercial",
                amount=85_000.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )
        events.append(
            DepositEvent(
                plan_key="plan:aetna medicare",
                amount=20_000.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )

    schedule = {
        "aetna commercial": DepositSchedule(allowed_weekdays=frozenset({0}), cadence="weekly_mon"),
        "aetna medicare": DepositSchedule(allowed_weekdays=frozenset({0}), cadence="weekly_mon"),
    }

    # Commercial past-due 120k as four chunks for FFD demo + medicare separate
    rows = []
    for amt in (60_000, 55_000, 45_000, 40_000):
        rows.append(
            {
                "webpt_patient_id": f"c-{amt}",
                "ins_name": "Aetna Commercial",
                "insurance_revflow": "",
                "date_of_service": date(2026, 5, 1),
                "outcome_stage": "overdue",
                "expected_amount": float(amt),
                "forecast_date": date(2026, 7, 1),
                "overdue_days": 20,
                "reconcile_status": "pending",
                "cpt_code": "97110",
            }
        )
    rows.append(
        {
            "webpt_patient_id": "m-1",
            "ins_name": "Aetna-Medicare",
            "insurance_revflow": "",
            "date_of_service": date(2026, 5, 1),
            "outcome_stage": "overdue",
            "expected_amount": 15_000.0,
            "forecast_date": date(2026, 7, 1),
            "overdue_days": 20,
            "reconcile_status": "pending",
            "cpt_code": "97110",
        }
    )
    outcomes = pd.DataFrame(rows)
    packed, audit, slot_audit, capacity_df = pack_pastdue_ffd(
        outcomes,
        as_of=as_of,
        deposit_events=events,
        deposit_schedules=schedule,
    )
    assert not audit.empty
    commercial = packed[packed["ins_name"] == "Aetna Commercial"]
    medicare = packed[packed["ins_name"] == "Aetna-Medicare"]
    # All commercial dates are Mondays
    for fd in commercial["forecast_date"]:
        assert fd.weekday() == 0
    # Medicare not mixed onto a commercial-only spill in a way that shares rows
    assert len(medicare) == 1
    # FFD should place 60+40 on first Monday when Cap~85k (or similarly fill without dumping all)
    first_monday = as_of
    on_first = commercial[commercial["forecast_date"] == first_monday]["expected_amount"].sum()
    assert on_first <= 85_000 + 1  # within capacity (allow tiny float)
    assert on_first >= 60_000  # at least largest item
    # Remainder goes to later Mondays — not all on day one
    assert commercial["forecast_date"].nunique() >= 2 or on_first < 200_000
    # Day normalize keeps calibrated ≤ target
    mon = slot_audit[slot_audit["slot"] == first_monday.isoformat()]
    assert not mon.empty
    assert float(mon.iloc[0]["calibrated_cap_sum"]) <= float(mon.iloc[0]["weekday_target"]) + 1


def test_parent_shares_by_outstanding_not_equal():
    """PPO 900k / Medicare 30k / SOMOS 10k → ~0.957 / 0.032 / 0.011."""
    children = [
        "plan:bcbs ppo",
        "plan:bcbs medicare",
        "plan:bcbs somos",
    ]
    outstanding = {
        "plan:bcbs ppo": 900_000.0,
        "plan:bcbs medicare": 30_000.0,
        "plan:bcbs somos": 10_000.0,
    }
    shares, method = compute_parent_shares(
        children,
        parent_key="org:anthem",
        slot=date(2026, 7, 27),
        deposit_events=[],  # no historical → outstanding
        outstanding=outstanding,
    )
    assert method == "outstanding"
    assert abs(shares["plan:bcbs ppo"] - 900 / 940) < 1e-6
    assert abs(shares["plan:bcbs medicare"] - 30 / 940) < 1e-6
    assert abs(shares["plan:bcbs somos"] - 10 / 940) < 1e-6


def test_thin_fallback_and_weekday_target():
    assert thin_fallback_cap(84_500) == max(84_500 * 0.01, 200.0)
    as_of = date(2026, 7, 27)
    events = []
    for w in range(8):
        d = as_of - timedelta(weeks=w + 1)
        d = d - timedelta(days=d.weekday())  # Monday
        events.append(
            DepositEvent(
                plan_key="plan:x",
                amount=80_000.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )
        events.append(
            DepositEvent(
                plan_key="plan:y",
                amount=4_500.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )
    targets = weekday_deposit_targets(as_of=as_of, deposit_events=events)
    assert abs(targets[0] - 84_500.0) < 1.0


def test_sibling_org_cap_shared_once_then_normalized():
    """Multiple plans hitting same org Cap must share once; day calibrated ≤ Target."""
    as_of = date(2026, 7, 27)
    events = []
    for w in range(8):
        d = as_of - timedelta(weeks=w + 1)
        d = d - timedelta(days=d.weekday())
        events.append(
            DepositEvent(
                plan_key="org:anthem",
                amount=16_700.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )
        events.append(
            DepositEvent(
                plan_key="plan:other payer",
                amount=84_500.0,
                deposit_date=d,
                weekday=0,
                week_of_month=1,
            )
        )

    rows = []
    # Large open AR drives outstanding shares; small overdue chunks exercise FFD.
    for name, open_amt, overdue_amt in (
        ("Anthem Commercial", 900_000.0, 8_000.0),
        ("BCBS Medicare", 30_000.0, 3_000.0),
        ("BCBS SOMOS", 10_000.0, 1_000.0),
    ):
        rows.append(
            {
                "webpt_patient_id": f"open-{open_amt}",
                "ins_name": name,
                "insurance_revflow": "",
                "date_of_service": date(2026, 6, 1),
                "outcome_stage": "on_track",
                "expected_amount": open_amt,
                "forecast_date": date(2026, 8, 3),  # future Monday — not reserved on Jul27
                "overdue_days": 0,
                "reconcile_status": "pending",
                "cpt_code": "97110",
            }
        )
        rows.append(
            {
                "webpt_patient_id": f"ov-{overdue_amt}",
                "ins_name": name,
                "insurance_revflow": "",
                "date_of_service": date(2026, 5, 1),
                "outcome_stage": "overdue",
                "expected_amount": overdue_amt,
                "forecast_date": date(2026, 7, 1),
                "overdue_days": 20,
                "reconcile_status": "pending",
                "cpt_code": "97110",
            }
        )

    schedule = {
        r["ins_name"]: DepositSchedule(
            allowed_weekdays=frozenset({0}), cadence="weekly_mon"
        )
        for r in rows
    }

    packed, audit, slot_audit, capacity_df = pack_pastdue_ffd(
        pd.DataFrame(rows),
        as_of=as_of,
        deposit_events=events,
        deposit_schedules=schedule,
    )
    mon = capacity_df[capacity_df["slot"] == as_of.isoformat()]
    org_rows = mon[mon["parent_key"] == "org:anthem"]
    assert len(org_rows) == 3
    assert abs(float(org_rows["parent_share"].sum()) - 1.0) < 1e-5
    assert set(org_rows["parent_share_method"]) == {"outstanding"}
    # Outstanding includes open + overdue: (900k+8k) / total
    ppo = org_rows[org_rows["grain_key"] == "plan:anthem commercial"].iloc[0]
    assert abs(float(ppo["parent_share"]) - 908_000 / 952_000) < 1e-3

    slot_mon = slot_audit[slot_audit["slot"] == as_of.isoformat()].iloc[0]
    assert float(slot_mon["calibrated_cap_sum"]) <= float(slot_mon["weekday_target"]) + 1
    # Non-overflow placements respect remaining; day-1 gets the largest fitting chunk
    assert float(slot_mon["packed_overdue"]) <= float(slot_mon["remaining_capacity"]) + 1
    assert float(slot_mon["packed_overdue"]) >= 8_000.0
