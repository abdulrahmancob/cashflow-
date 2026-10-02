"""Manual Eligibility Sheet overrides and audit planning."""

from __future__ import annotations

import inspect

import pytest

from cashflow_db.repository.eligibility import (
    apply_manual_overrides,
    attach_sheet_fields,
    plan_work_item_patch,
    upsert_from_visit,
)
from cashflow_db.db import MIGRATIONS


def test_override_wins_over_recon_overlay():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_total_paid": 85.5,
            "recon_check_number": "CHK1",
            "recon_check_date": "2026-07-15",
            "recon_visit_status": "paid",
            "tracker_date": "2026-07-20",
            "patient_name": "Auto Name",
            "manual_overrides": {
                "paid_amount": 10,
                "check_number": "MANUAL",
                "check_date": "2026-08-01",
                "tracker_date": "2026-08-02",
                "source_visit_status": "pending",
                "patient_name": "Edited Name",
            },
        }
    )
    apply_manual_overrides(row)
    assert row["paid_amount"] == 10
    assert row["check_number"] == "MANUAL"
    assert row["check_date"] == "2026-08-01"
    assert row["tracker_date"] == "2026-08-02"
    assert row["source_visit_status"] == "pending"
    assert row["patient_name"] == "Edited Name"


def test_clear_override_restores_automatic_on_next_apply():
    auto = attach_sheet_fields(
        {
            "recon_total_paid": 85.5,
            "recon_check_number": "CHK1",
            "source_visit_status": "paid",
            "manual_overrides": {"paid_amount": 10, "check_number": "MANUAL"},
        }
    )
    apply_manual_overrides(auto)
    assert auto["paid_amount"] == 10

    item = {
        "paid_amount": 10,
        "check_number": "MANUAL",
        "manual_overrides": {"paid_amount": 10, "check_number": "MANUAL"},
    }
    _direct, new_ov, history, changed = plan_work_item_patch(
        item, {"paid_amount": "", "check_number": None}
    )
    assert changed
    assert new_ov == {}
    assert {h["column_name"] for h in history} == {"paid_amount", "check_number"}

    restored = attach_sheet_fields(
        {
            "recon_total_paid": 85.5,
            "recon_check_number": "CHK1",
            "source_visit_status": "paid",
            "manual_overrides": new_ov,
        }
    )
    apply_manual_overrides(restored)
    assert restored["paid_amount"] == 85.5
    assert restored["check_number"] == "CHK1"


def test_patch_writes_history_for_direct_and_override():
    item = {
        "notes": "old note",
        "insurance_name": "Aetna",
        "paid_amount": 41.77,
        "manual_overrides": {},
    }
    direct, new_ov, history, ov_changed = plan_work_item_patch(
        item,
        {"notes": "new note", "insurance_name": "Cigna", "paid_amount": 12.5},
    )
    assert direct == {"notes": "new note"}
    assert ov_changed
    assert new_ov["insurance_name"] == "Cigna"
    assert new_ov["paid_amount"] == 12.5
    by_col = {h["column_name"]: h for h in history}
    assert by_col["notes"]["old_value"] == "old note"
    assert by_col["notes"]["new_value"] == "new note"
    assert by_col["insurance_name"]["old_value"] == "Aetna"
    assert by_col["insurance_name"]["new_value"] == "Cigna"
    assert by_col["paid_amount"]["old_value"] == "41.77"
    assert by_col["paid_amount"]["new_value"] == "12.5"


def test_unchanged_and_clear_without_override_are_noops():
    item = {"notes": "same", "paid_amount": 5, "manual_overrides": {}}
    direct, new_ov, history, changed = plan_work_item_patch(
        item, {"notes": "same", "paid_amount": ""}
    )
    assert direct == {}
    assert new_ov == {}
    assert history == []
    assert not changed


def test_invalid_paid_and_date_raise():
    with pytest.raises(ValueError, match="paid_amount"):
        plan_work_item_patch({"manual_overrides": {}}, {"paid_amount": "nope"})
    with pytest.raises(ValueError, match="dos"):
        plan_work_item_patch({"manual_overrides": {}}, {"dos": "13/40/2026"})


def test_045_ledger_migration_registered():
    assert "045_elig_sheet_ledger.sql" in MIGRATIONS
    assert MIGRATIONS.index("045_elig_sheet_ledger.sql") > MIGRATIONS.index(
        "044_ops_admin_role.sql"
    )
    assert "052_recon_check_breakdown.sql" in MIGRATIONS
    assert MIGRATIONS.index("052_recon_check_breakdown.sql") > MIGRATIONS.index(
        "051_emr_name.sql"
    )


def test_upsert_from_visit_does_not_touch_manual_overrides():
    src = inspect.getsource(upsert_from_visit)
    assert "manual_overrides" not in src
    assert "lookup_insurance_name" in src
    assert "COALESCE(" in src
    assert "EXCLUDED.patient_name" in src


def test_sheet_total_excludes_reduction_and_prefers_sf():
    row = attach_sheet_fields(
        {
            "source_visit_status": "pending",
            "sf_status": "paid",
            "sf_client_payment": 10,
            "sf_insurance_payment": 80,
            "sf_updated_payment": -5,
            "sf_coinsurance_payment": 15,
            "sf_reduction": 20,
            "sf_adjusted": 7,
            "recon_total_paid": 999,
        }
    )
    assert row["insurance_payment"] == 80
    assert row["paid_amount"] == 80
    assert row["source_visit_status"] == "paid"
    assert row["total_amount"] == 90
    assert row["reduction"] == 20
    assert row["added_amount"] == 0
    assert row["deducted_amount"] == 25


def test_override_wins_over_sf_and_recon():
    row = attach_sheet_fields(
        {
            "sf_insurance_payment": 80,
            "recon_total_paid": 90,
            "manual_overrides": {"insurance_payment": 12.5, "client_payment": 3},
        }
    )
    apply_manual_overrides(row)
    assert row["insurance_payment"] == 12.5
    assert row["paid_amount"] == 12.5
    assert row["client_payment"] == 3
    assert row["total_amount"] == 12.5


def test_paid_amount_override_still_aliases_insurance_payment():
    row = attach_sheet_fields(
        {
            "recon_total_paid": 85.5,
            "manual_overrides": {"paid_amount": 10},
        }
    )
    apply_manual_overrides(row)
    assert row["paid_amount"] == 10
    assert row["insurance_payment"] == 10
    assert row["total_amount"] == 10


def test_money_delta_from_paid_patch_maps_to_insurance():
    from cashflow_db.repository.eligibility import money_deltas_from_history

    _direct, new_ov, history, changed = plan_work_item_patch(
        {"paid_amount": 100, "insurance_payment": 100, "manual_overrides": {}},
        {"paid_amount": 80},
    )
    assert changed
    assert new_ov["paid_amount"] == 80
    assert money_deltas_from_history(history) == [("insurance_payment", -20.0)]


def test_export_headers_match_sheet_contract():
    from cashflow_db.repository.eligibility import sheet_export_headers, sheet_export_row

    headers = sheet_export_headers()
    assert headers[0] == "EMR ID"
    assert headers[headers.index("Status") + 1] == "Collection Status"
    assert headers[12] == "Total  Amount"
    assert "Client Payment" not in headers
    assert "Insurance Payemnt Check#" in headers
    assert "Reduction" in headers
    assert "Updated Payment" in headers
    assert "Added" in headers
    assert "Deducted" in headers
    row = sheet_export_row(
        {
            "emr_patient_id": "1",
            "patient_name": "A",
            "client_payment": 10,
            "insurance_payment": 20,
            "updated_payment": 1,
            "coinsurance_payment": 2,
            "total_amount": 23,
            "assigned_to_name": "Sam",
        }
    )
    assert row[0] == "1"
    assert row[4] == 20
    assert row[7] == 1
    assert row[-1] == "Sam"
    collection = sheet_export_headers("collection")
    assert collection[0] == "EMR ID"
    assert "Denial Reason" in collection
    assert "Client Payment" in collection
    assert "Work Status" not in collection
    assert "Updated Payment" not in collection


def test_sf_overlay_mode_pending_take_all_paid_match_and_skip():
    from cashflow_db.repository.eligibility import (
        SF_OVERLAY_EXTRAS_ONLY,
        SF_OVERLAY_SKIP,
        SF_OVERLAY_TAKE_ALL,
        sf_overlay_mode,
    )

    assert sf_overlay_mode("pending", 0, "paid", 80) == SF_OVERLAY_TAKE_ALL
    assert sf_overlay_mode("pending", 0, "denied", 0) == SF_OVERLAY_TAKE_ALL
    assert sf_overlay_mode("paid", 100, "paid", 100) == SF_OVERLAY_EXTRAS_ONLY
    assert sf_overlay_mode("paid", 100.004, "paid", 100) == SF_OVERLAY_EXTRAS_ONLY
    assert sf_overlay_mode("paid", 100, "paid", 80) == SF_OVERLAY_SKIP
    assert sf_overlay_mode("paid", 100, "denied", 0) == SF_OVERLAY_SKIP
    assert sf_overlay_mode("pending", 0, "pending", 0) == SF_OVERLAY_EXTRAS_ONLY


def test_pending_take_all_copies_sf_status_money_and_collector():
    row = attach_sheet_fields(
        {
            "source_visit_status": "pending",
            "recon_visit_status": "pending",
            "recon_total_paid": 0,
            "sf_status": "paid",
            "sf_insurance_payment": 80,
            "sf_client_payment": 10,
            "sf_insurance_check_number": "SFCHK",
            "sf_collector_1": "Ann",
        }
    )
    assert row["source_visit_status"] == "paid"
    assert row["insurance_payment"] == 80
    assert row["paid_amount"] == 80
    assert row["client_payment"] == 10
    assert row["insurance_check_number"] == "SFCHK"
    assert row["collector_1"] == "Ann"


def test_paid_match_fills_extras_without_changing_paid_or_status():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 100,
            "recon_check_number": "OURS",
            "sf_status": "paid",
            "sf_insurance_payment": 100,
            "sf_client_payment": 12,
            "sf_coinsurance_payment": 3,
            "sf_insurance_check_number": "SFCHK",
            "sf_collector_1": "Ann",
            "sf_posting_date_1": "2026-07-01",
        }
    )
    assert row["source_visit_status"] == "paid"
    assert row["insurance_payment"] == 100
    assert row["paid_amount"] == 100
    assert row["check_number"] == "OURS"
    assert row["insurance_check_number"] == "OURS"
    assert row["client_payment"] == 12
    assert row["coinsurance_payment"] == 3
    assert row["collector_1"] == "Ann"
    assert row["posting_date_1"] == "2026-07-01"


def test_paid_mismatch_skips_sf_extras():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 100,
            "recon_check_number": "OURS",
            "sf_status": "paid",
            "sf_insurance_payment": 80,
            "sf_client_payment": 12,
            "sf_insurance_check_number": "SFCHK",
            "sf_collector_1": "Ann",
        }
    )
    assert row["source_visit_status"] == "paid"
    assert row["insurance_payment"] == 100
    assert row["paid_amount"] == 100
    assert row["check_number"] == "OURS"
    assert row["client_payment"] is None
    assert row["collector_1"] is None


def test_pr1_fills_reduction_over_sf():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_total_paid": 80,
            "recon_pr1_amount": 12.5,
            "sf_reduction": 99,
            "context": {"reduction": 40},
        }
    )
    assert row["reduction"] == 12.5


def test_recon_same_payer_updated_does_not_keep_sf_coins():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 22.84,
            "recon_check_amount": 18.2,
            "recon_third_check_amount": 4.64,
            "sf_coinsurance_payment": 4.64,
            "context": {"secondary_check_amount": 4.64, "coinsurance_payment": 4.64},
        }
    )
    assert row["insurance_payment"] == 18.2
    assert row["updated_payment"] == 4.64
    assert row["coinsurance_payment"] is None
    assert row["total_amount"] == 22.84


def test_denial_reason_fills_details_over_sf():
    row = attach_sheet_fields(
        {
            "source_visit_status": "denied",
            "recon_denial_reason": "CO-16 / missing auth",
            "sf_details": "snowflake note",
            "sf_denial_reason": "sf denial",
            "context": {"details": "old details", "denial_reason": "old denial"},
        }
    )
    assert row["details"] == "CO-16 / missing auth"
    assert row["denial_reason"] == "CO-16 / missing auth"
