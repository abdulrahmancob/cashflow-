"""Collection lookups, overdue bucket SQL, and work-date autofill."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cashflow_db.db import MIGRATIONS
from cashflow_db.repository import collection
from cashflow_db.repository.eligibility import (
    COLLECTION_EXPORT_COLUMNS,
    COLLECTION_VISIT_SQL,
    DENIED_VISIT_SQL,
    OVERDUE_PENDING_SQL,
    STILL_PENDING_VISIT_SQL,
    _build_filters,
    normalize_edit_value,
    plan_work_item_patch,
    sheet_export_headers,
)


ROOT = Path(__file__).resolve().parents[2]


def test_migration_058_is_registered():
    assert "058_list_perf_indexes.sql" in MIGRATIONS
    assert MIGRATIONS.index("058_list_perf_indexes.sql") > MIGRATIONS.index(
        "057_analytics_viewer_role.sql"
    )
    sql = (ROOT / "cashflow_db" / "sql" / "058_list_perf_indexes.sql").read_text(
        encoding="utf-8"
    )
    assert "ix_recon_visit_run_emr_dos" in sql
    assert "ix_forecast_pred_run_stage_emr_dos" in sql
    assert "gin_trgm_ops" in sql


def test_migration_059_keep_visit_is_registered():
    assert "059_elig_keep_visit.sql" in MIGRATIONS
    assert MIGRATIONS.index("059_elig_keep_visit.sql") > MIGRATIONS.index(
        "058_list_perf_indexes.sql"
    )
    sql = (ROOT / "cashflow_db" / "sql" / "059_elig_keep_visit.sql").read_text(
        encoding="utf-8"
    )
    assert "analytics.elig_keep_visit" in sql


def test_migration_056_is_registered():
    assert "056_collection_lookups.sql" in MIGRATIONS
    assert MIGRATIONS.index("056_collection_lookups.sql") > MIGRATIONS.index(
        "055_portal_activity.sql"
    )


def test_sql_seed_labels():
    sql = (ROOT / "cashflow_db" / "sql" / "056_collection_lookups.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS ops.collection_lookup" in sql
    assert "ops.fold_insurance_name(label)" in sql
    for label in collection.DENIAL_REASON_SEED:
        assert f"'{label}'" in sql
    for label in collection.ROOT_CAUSE_SEED:
        assert f"'{label}'" in sql
    for label in collection.COLLECTION_STATUS_SEED:
        assert f"'{label}'" in sql
    assert "Auth delay" in sql
    assert "Auth dealy" not in sql


def test_fold_label_strips_non_alnum():
    assert collection.fold_label("Auth delay") == "authdelay"
    assert collection.fold_label("  AUTH-DELAY ") == "authdelay"
    assert collection.fold_label("Canceled - No Show") == "cancelednoshow"


def test_migration_060_collection_member_is_registered():
    assert "060_collection_queue_member.sql" in MIGRATIONS
    assert MIGRATIONS.index("060_collection_queue_member.sql") > MIGRATIONS.index(
        "059_elig_keep_visit.sql"
    )
    sql = (ROOT / "cashflow_db" / "sql" / "060_collection_queue_member.sql").read_text(
        encoding="utf-8"
    )
    assert "analytics.collection_queue_member" in sql
    assert "PRIMARY KEY (bucket, work_item_id)" in sql


def test_collection_queue_default_is_denied():
    from cashflow_db.repository.eligibility import COLLECTION_QUEUE_MEMBER_SQL

    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
    )
    assert COLLECTION_QUEUE_MEMBER_SQL in sql
    assert params == ["denied"]
    assert DENIED_VISIT_SQL not in sql
    assert "forecast_prediction" not in sql


def test_collection_refresh_keeps_bucket_rules():
    from cashflow_db.repository.eligibility import collection_bucket_predicate
    from cashflow_db.repository.visits import KEEP_WORK_ITEM_SQL

    denied = collection_bucket_predicate("denied")
    overdue = collection_bucket_predicate("overdue")
    collection_status = collection_bucket_predicate("collection")
    assert DENIED_VISIT_SQL in denied
    assert OVERDUE_PENDING_SQL in overdue
    assert "expected_pay_date + 3 < CURRENT_DATE" in overdue
    assert f"NOT {DENIED_VISIT_SQL}" in overdue
    assert f"NOT {COLLECTION_VISIT_SQL}" in overdue
    assert "total_remit_amount" not in overdue.replace(DENIED_VISIT_SQL, "")
    assert "NULLIF(btrim(el.carcs), '')" in overdue
    assert "IN ('paid', 'deduct')" in overdue
    assert "analytics.snowflake_visit_kpi" in overdue
    assert "rv.visit_status" not in overdue
    assert "forecast_payload_dos" in overdue
    assert " = 'collection'" in collection_status
    assert "NOT IN ('paid', 'deduct')" in COLLECTION_VISIT_SQL
    assert " = 'collection'" in COLLECTION_VISIT_SQL
    assert "NOT " in denied and "PR-?3" in denied
    refresh_src = (
        ROOT / "cashflow_db" / "repository" / "eligibility.py"
    ).read_text(encoding="utf-8")
    assert "DELETE FROM analytics.collection_queue_member" in refresh_src
    assert "KEEP_WORK_ITEM_SQL" in refresh_src
    refresh_fn = refresh_src.split("def refresh_collection_queue", 1)[1].split("\ndef ", 1)[0]
    assert "OR {COLLECTION_VISIT_SQL}" in refresh_fn
    assert '"collection":' not in refresh_fn
    overdue_rule = refresh_fn.split('"overdue":', 1)[1].split('"arbitration":', 1)[0]
    assert "tmp_waystar_past_sla" in overdue_rule
    assert "NOT {SKIPPED_VISIT_SQL}" in overdue_rule
    assert "NOT {COLLECTION_VISIT_SQL}" in overdue_rule
    assert overdue_rule.find("tmp_waystar_past_sla") < overdue_rule.find("tmp_waystar_zero")
    patch_fn = refresh_src.split("def patch_work_item", 1)[1].split("\ndef ", 1)[0]
    assert "DELETE FROM analytics.collection_queue_member" in patch_fn
    assert '"paid", "deduct"' in patch_fn
    assert "paid_patient_responsibility" in refresh_fn
    assert "source_visit_status = 'patient_responsibility'" in refresh_fn
    assert "manual_overrides->>'source_visit_status'" in refresh_fn


def test_visit_status_leave_deduct_rehomes_collection_member():
    src = (ROOT / "cashflow_db" / "repository" / "eligibility.py").read_text(
        encoding="utf-8"
    )
    patch_fn = src.split("def patch_work_item", 1)[1].split("\ndef ", 1)[0]
    assert '"paid", "deduct"' in patch_fn
    paid_branch, rest = patch_fn.split('in ("paid", "deduct"):', 1)[1].split(
        "elif", 1
    )
    assert "DELETE FROM analytics.collection_queue_member" in paid_branch
    assert "_rehome_collection_member" not in paid_branch
    assert "INSERT INTO analytics.collection_queue_member" not in paid_branch
    assert '"source_visit_status" in updates' in rest
    assert "_collection_membership_bucket" in rest
    assert "_rehome_collection_member" in rest

    helper = src.split("def _rehome_collection_member", 1)[1].split("\ndef ", 1)[0]
    assert "DELETE FROM analytics.collection_queue_member" in helper
    assert "INSERT INTO analytics.collection_queue_member" in helper
    assert "DENIED_VISIT_SQL" in helper
    assert "COLLECTION_VISIT_SQL" in helper
    assert "PR3_UNPAID_SQL" in helper
    assert "OVERDUE_PENDING_SQL" in helper
    assert "ROUTED_COLLECTION_SQL" in helper
    assert '"denied"' in helper
    assert '"overdue"' in helper


def test_collection_overdue_is_pending_after_sla():
    from cashflow_db.repository.eligibility import (
        COLLECTION_QUEUE_MEMBER_SQL,
        collection_bucket_predicate,
    )

    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="overdue",
    )
    predicate = collection_bucket_predicate("overdue")
    assert COLLECTION_QUEUE_MEMBER_SQL in sql
    assert params == ["overdue"]
    assert OVERDUE_PENDING_SQL in predicate
    assert "expected_pay_date + 3 < CURRENT_DATE" in predicate
    assert "analytics.forecast_prediction" in predicate
    assert f"NOT {DENIED_VISIT_SQL}" in predicate
    assert f"NOT {COLLECTION_VISIT_SQL}" in predicate
    assert "total_remit_amount" not in predicate.replace(DENIED_VISIT_SQL, "")
    assert "NULLIF(btrim(el.carcs), '')" in predicate
    assert "IN ('paid', 'deduct')" in predicate
    assert "analytics.snowflake_visit_kpi" in predicate
    assert "rv.visit_status" not in predicate
    assert "forecast_payload_dos" in predicate
    assert "fp.webpt_patient_id = wi.emr_patient_id" in predicate
    assert "fp.date_of_service = wi.dos" in predicate
    assert "fp.webpt_patient_id IS NULL" in predicate


def test_collection_status_routes_to_tabs():
    assert collection.collection_status_bucket("Arbitration") == "arbitration"
    assert collection.collection_status_bucket("Action Taken") == "action"
    assert collection.collection_status_bucket("pending") == "action"
    assert collection.collection_status_bucket("Submitted without Auth") == "at_risk"
    assert collection.collection_status_bucket("Dead") == "dead"
    assert collection.collection_status_bucket("Paid") is None
    assert "064_collection_status_buckets.sql" in MIGRATIONS
    assert "069_paid_patient_responsibility.sql" in MIGRATIONS
    assert "071_collection_dead_bucket.sql" in MIGRATIONS
    assert "073_collection_exit_indexes.sql" in MIGRATIONS
    assert "074_eligibility_sheet_facet.sql" in MIGRATIONS
    assert MIGRATIONS.index("074_eligibility_sheet_facet.sql") > MIGRATIONS.index(
        "073_collection_exit_indexes.sql"
    )
    assert MIGRATIONS.index("073_collection_exit_indexes.sql") > MIGRATIONS.index(
        "072_billing_collect_visit.sql"
    )
    assert MIGRATIONS.index("071_collection_dead_bucket.sql") > MIGRATIONS.index(
        "070_desk_permission.sql"
    )


def test_dead_tab_uses_its_own_bucket():
    from cashflow_db.repository.eligibility import ROUTED_COLLECTION_SQL

    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="dead",
    )
    assert params == ["dead"]
    assert "collection_queue_member" in sql
    assert "'dead'" in ROUTED_COLLECTION_SQL
    refresh_src = (ROOT / "cashflow_db" / "repository" / "eligibility.py").read_text(
        encoding="utf-8"
    )
    refresh_fn = refresh_src.split("def refresh_collection_queue", 1)[1].split("\ndef ", 1)[0]
    assert "denied_for_dead" in refresh_fn
    assert "overdue_for_dead" in refresh_fn
    assert "ROUTED_EXCEPT_DEAD_SQL" in refresh_fn
    assert "= 'dead'" in refresh_fn
    assert "dead_tab_membership_sql" in refresh_src
    migration = (ROOT / "cashflow_db" / "sql" / "071_collection_dead_bucket.sql").read_text(
        encoding="utf-8"
    )
    assert "'dead'" in migration
    page = (ROOT / "rcm_portal" / "src" / "pages" / "CollectionQueue.tsx").read_text(
        encoding="utf-8"
    )
    assert "{ key: 'dead', label: 'Dead' }" in page
    assert "Denied and Overdue visits appear here after Collection Status is set to Dead." in page


def test_follow_up_is_aged_action():
    follow_sql, follow_params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="follow_up",
    )
    action_sql, action_params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="action",
    )
    assert follow_params == ["action"]
    assert action_params == ["action"]
    assert "manual_overrides->>'follow_up_after'" in follow_sql
    assert "CURRENT_DATE -" in follow_sql
    assert ",\n    30\n)" in follow_sql
    assert "NOT (" in action_sql
    assert ("follow_up_after", "Follow up after") in COLLECTION_EXPORT_COLUMNS
    assert "submittedwithoutauth" in (
        ROOT / "cashflow_db" / "repository" / "eligibility.py"
    ).read_text(encoding="utf-8")


def test_assign_filter_uses_list_filters():
    from cashflow_db.repository.eligibility import assign_filter_statement

    actor = "11111111-1111-1111-1111-111111111111"
    assignee = "33333333-3333-3333-3333-333333333333"
    sql, params = assign_filter_statement(
        actor_id=actor,
        assignee_id=assignee,
        month=["2026-09"],
        insurance=["Aetna"],
        bucket="denied",
    )
    assert "IS DISTINCT FROM" in sql
    assert "ops.eligibility_history" in sql
    assert "analytics.collection_queue_member" in sql
    assert date(2026, 9, 1) in params
    assert ["aetna"] in params
    assert "denied" in params
    assert params[-6:] == [assignee, assignee, actor, assignee, assignee, actor]


def test_follow_up_after_days_override():
    _direct, overrides, history, changed = plan_work_item_patch(
        {"manual_overrides": {}},
        {"follow_up_after": "14"},
    )
    assert changed
    assert overrides["follow_up_after"] == "14"
    assert history[0]["column_name"] == "follow_up_after"
    assert history[0]["new_value"] == "14"
    assert normalize_edit_value("follow_up_after", "0") == "0"
    assert normalize_edit_value("follow_up_after", "") is None
    assert normalize_edit_value("follow_up_after", None) is None
    cleared, cleared_ov, cleared_history, cleared_changed = plan_work_item_patch(
        {"manual_overrides": {"follow_up_after": "14"}, "follow_up_after": "14"},
        {"follow_up_after": ""},
    )
    assert cleared_changed
    assert "follow_up_after" not in cleared_ov
    assert cleared_history[0]["new_value"] is None
    assert cleared == {}
    with pytest.raises(ValueError):
        normalize_edit_value("follow_up_after", "soon")
    with pytest.raises(ValueError):
        normalize_edit_value("follow_up_after", "366")
    with pytest.raises(ValueError):
        normalize_edit_value("follow_up_after", "-1")


def test_collection_overdue_unknown_bucket_falls_back_to_denied():
    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="all",
    )
    assert params == ["denied"]
    assert OVERDUE_PENDING_SQL not in sql


def test_collection_bucket_lists_collection_status():
    from cashflow_db.repository.eligibility import (
        COLLECTION_QUEUE_MEMBER_SQL,
        PR3_UNPAID_SQL,
        collection_bucket_predicate,
    )

    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        bucket="collection",
    )
    predicate = collection_bucket_predicate("collection")
    assert COLLECTION_QUEUE_MEMBER_SQL in sql
    assert params == ["collection"]
    assert " = 'collection'" in predicate
    assert OVERDUE_PENDING_SQL not in predicate
    assert f"NOT {PR3_UNPAID_SQL}" in predicate


def test_sheet_queue_keeps_collection_visits():
    from cashflow_db.repository.eligibility import (
        COLLECTION_VISIT_SQL,
        SKIPPED_VISIT_SQL,
        collection_tab_label,
        sheet_export_headers,
        sheet_export_row,
    )

    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="sheet",
    )
    assert f"NOT {COLLECTION_VISIT_SQL}" not in sql
    assert f"NOT {DENIED_VISIT_SQL}" not in sql
    assert f"NOT {SKIPPED_VISIT_SQL}" in sql
    assert collection_tab_label(["overdue", "denied"]) == "Denied, Overdue"
    assert collection_tab_label([]) is None
    headers = sheet_export_headers()
    status_at = headers.index("Status")
    assert headers[status_at + 1] == "Collection Status"
    exported = sheet_export_row(
        {
            "source_visit_status": "denied",
            "collection_status": None,
            "collection_tab": "Denied",
        }
    )
    assert exported[status_at + 1] == "Denied"
    exported_set = sheet_export_row(
        {"collection_status": "Action Taken", "collection_tab": "Denied"}
    )
    assert exported_set[headers.index("Collection Status")] == "Action Taken"


def test_sheet_queue_ignores_overdue_bucket():
    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="sheet",
        bucket="overdue",
    )
    assert OVERDUE_PENDING_SQL not in sql
    assert f"NOT {DENIED_VISIT_SQL}" not in sql


def test_collection_denied_includes_pr3_unpaid():
    from cashflow_db.repository.eligibility import (
        PR3_PAID_COLLECTION_SQL,
        PR3_UNPAID_SQL,
        collection_bucket_predicate,
    )

    predicate = collection_bucket_predicate("denied")
    paid = collection_bucket_predicate("paid_patient_responsibility")
    assert DENIED_VISIT_SQL in predicate
    assert PR3_UNPAID_SQL in predicate
    assert f"NOT {PR3_UNPAID_SQL}" not in predicate
    assert f"NOT ({PR3_PAID_COLLECTION_SQL})" in predicate
    assert "PR-?3" in predicate
    assert "eob_carc_raw" in predicate
    assert "el.carcs" in predicate
    assert " = 'paid'" in paid
    assert "eob_carc_raw" in paid


def test_patient_responsibility_paid_joins_that_tab():
    from cashflow_db.repository.eligibility import (
        PAID_PATIENT_RESPONSIBILITY_VISIT_SQL,
        collection_bucket_predicate,
    )

    paid = collection_bucket_predicate("paid_patient_responsibility")
    denied = collection_bucket_predicate("denied")
    overdue = collection_bucket_predicate("overdue")
    assert "patient_responsibility" in PAID_PATIENT_RESPONSIBILITY_VISIT_SQL
    assert "= 'paid'" in PAID_PATIENT_RESPONSIBILITY_VISIT_SQL
    assert PAID_PATIENT_RESPONSIBILITY_VISIT_SQL in paid
    assert f"NOT ({PAID_PATIENT_RESPONSIBILITY_VISIT_SQL})" in denied
    assert f"NOT ({PAID_PATIENT_RESPONSIBILITY_VISIT_SQL})" in overdue
    refresh_src = (ROOT / "cashflow_db" / "repository" / "eligibility.py").read_text(
        encoding="utf-8"
    )
    refresh_fn = refresh_src.split("def refresh_collection_queue", 1)[1].split("\ndef ", 1)[0]
    assert "PAID_PATIENT_RESPONSIBILITY_VISIT_SQL" in refresh_fn
    assert "visit_status == \"patient_responsibility\"" in refresh_src
    assert "_visit_is_patient_responsibility" in refresh_src
    page = (ROOT / "rcm_portal" / "src" / "pages" / "CollectionQueue.tsx").read_text(
        encoding="utf-8"
    )
    assert "Patient Responsibility visits appear here after Collection Status is set to Paid." in page


def test_collection_denied_exposure_sql_membership_and_charged(monkeypatch):
    from cashflow_db.repository import eligibility
    from cashflow_db.repository.eligibility import (
        COLLECTION_DENIED_CHARGED_SQL,
        DENIED_VISIT_SQL,
        PR3_UNPAID_SQL,
        collection_denied_exposure,
    )

    captured: list[str] = []
    captured_params: list[list] = []

    def fake_fetchone(_conn, sql, params=None):
        captured.append(sql)
        captured_params.append(list(params or []))
        return {"exposure_amount": 120.0, "visit_count": 3}

    def fake_fetchall(_conn, sql, params=None):
        captured.append(sql)
        captured_params.append(list(params or []))
        return [{"ins_name": "Aetna", "exposure_amount": 120.0, "visit_count": 3}]

    monkeypatch.setattr(eligibility.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(eligibility.client, "fetchall", fake_fetchall)
    payload = collection_denied_exposure(
        object(),
        d0=date(2026, 8, 1),
        d1=date(2026, 8, 31),
        facilities=["Bedstuy"],
        insurers=["1199"],
    )
    blob = "\n".join(captured)
    assert "analytics.collection_queue_member" in blob
    assert "m.bucket = %s" in blob
    assert DENIED_VISIT_SQL not in blob
    assert PR3_UNPAID_SQL not in blob
    assert "sf.charged_amount" in blob
    assert "analytics.snowflake_visit_kpi" in blob
    assert "wi.context->>'charged_amount'" in blob
    assert COLLECTION_DENIED_CHARGED_SQL in blob
    assert "wi.dos >= %s" in blob
    assert "wi.facility_name = ANY(%s)" in blob
    assert "wi.insurance_name = ANY(%s)" in blob
    assert " = 'dead'" not in blob
    assert "canceled" not in blob.lower()
    assert captured_params
    assert all(p[0] == "denied" for p in captured_params)
    assert date(2026, 8, 1) in captured_params[0]
    assert date(2026, 8, 31) in captured_params[0]
    assert ["Bedstuy"] in captured_params[0]
    assert ["1199"] in captured_params[0]
    assert abs(float(payload["exposure_amount"]) - 120.0) < 0.01
    assert payload["visit_count"] == 3


def test_collection_overdue_excludes_pr3_unpaid():
    from cashflow_db.repository.eligibility import (
        PR3_UNPAID_SQL,
        collection_bucket_predicate,
    )

    predicate = collection_bucket_predicate("overdue")
    assert OVERDUE_PENDING_SQL in predicate
    assert f"NOT {PR3_UNPAID_SQL}" in predicate


def test_work_date_autofills_once_on_collection_edit():
    item = {
        "denial_reason": None,
        "work_date": None,
        "manual_overrides": {},
    }
    _direct, new_ov, history, changed = plan_work_item_patch(
        item, {"denial_reason": "Auth Absent"}
    )
    assert changed
    assert new_ov["denial_reason"] == "Auth Absent"
    assert new_ov["work_date"] == date.today().isoformat()
    by_col = {h["column_name"]: h for h in history}
    assert by_col["work_date"]["new_value"] == date.today().isoformat()

    again = {
        "denial_reason": "Auth Absent",
        "work_date": new_ov["work_date"],
        "manual_overrides": new_ov,
    }
    _direct2, ov2, history2, changed2 = plan_work_item_patch(
        again, {"root_cause": "Auth delay"}
    )
    assert changed2
    assert ov2["work_date"] == new_ov["work_date"]
    assert "work_date" not in {h["column_name"] for h in history2}


def test_work_date_not_autofilled_when_explicit():
    item = {"denial_reason": None, "work_date": None, "manual_overrides": {}}
    _direct, new_ov, history, changed = plan_work_item_patch(
        item, {"denial_reason": "Auth Absent", "work_date": "2026-01-15"}
    )
    assert changed
    assert new_ov["work_date"] == "2026-01-15"
    assert {h["column_name"] for h in history} == {"denial_reason", "work_date"}


def test_collection_export_matches_sheet():
    headers = sheet_export_headers("collection")
    assert headers[0] == "EMR ID"
    assert "Client Payment" in headers
    assert "Facility" in headers
    assert headers.index("Facility") == headers.index("Insurance Name") + 1
    assert "Denial Reason" in headers
    assert "Follow up after" in headers
    assert "Work Status" not in headers
    assert "Updated Payment" not in headers
    assert "Account #" in headers
    assert headers.index("Account #") == headers.index("EMR ID") + 1


def test_paid_requires_saved_insurance_payment():
    import pytest

    from cashflow_db.repository.eligibility import (
        _PAID_SORT_SQL,
        assert_paid_has_manual_payment,
    )

    bare = {"manual_overrides": {}, "insurance_payment": 10}
    with pytest.raises(ValueError, match="Insurance Payment"):
        assert_paid_has_manual_payment(bare, {"source_visit_status": "paid"})
    assert_paid_has_manual_payment(
        bare, {"source_visit_status": "paid", "insurance_payment": "25"}
    )
    assert_paid_has_manual_payment(
        {"manual_overrides": {"paid_amount": 12}},
        {"source_visit_status": "paid"},
    )
    with pytest.raises(ValueError, match="Insurance Payment"):
        assert_paid_has_manual_payment(
            {"manual_overrides": {"insurance_payment": 25}},
            {"source_visit_status": "paid", "insurance_payment": ""},
        )
    assert_paid_has_manual_payment(bare, {"source_visit_status": "deduct"})
    assert _PAID_SORT_SQL.index("insurance_payment") < _PAID_SORT_SQL.index("paid_amount")


def test_portal_collection_page_is_ss_style():
    app = (ROOT / "rcm_portal" / "src" / "App.tsx").read_text(encoding="utf-8")
    page = (ROOT / "rcm_portal" / "src" / "pages" / "Collection.tsx").read_text(
        encoding="utf-8"
    )
    queue = (ROOT / "rcm_portal" / "src" / "pages" / "CollectionQueue.tsx").read_text(
        encoding="utf-8"
    )
    lookups = (ROOT / "rcm_portal" / "src" / "pages" / "CollectionLookups.tsx").read_text(
        encoding="utf-8"
    )
    assert "import { CollectionPage }" in app
    assert "<CollectionPage />" in app
    assert 'queue="collection"' not in app
    assert "CollectionQueueTab" in page
    assert "CollectionLookupsTab" in page
    assert "SearchableSelect" in queue
    assert "Facility" in queue
    assert "facility_name" in queue
    assert "account_number" in queue
    assert "Account #" in queue
    assert "visit_status" in queue
    assert "All root causes" in queue
    assert "All collection status" in queue
    assert "bucket" in queue
    assert "Overdue" in queue
    assert "Follow up" in queue
    assert "At risk" in queue
    assert "elig-sticky-patient_name" in queue
    assert "Enter Insurance Payment before marking the visit Paid" in queue
    assert "{ key: 'collection'" not in queue
    assert "No collection visits" not in queue
    assert "Drawer" not in queue
    assert "/api/collection/lookups" in lookups
    assert "expected_pay_date + 3 < CURRENT_DATE" in OVERDUE_PENDING_SQL
    assert f"NOT {DENIED_VISIT_SQL}" in OVERDUE_PENDING_SQL
    assert f"NOT {COLLECTION_VISIT_SQL}" in OVERDUE_PENDING_SQL
    assert "total_remit_amount" not in OVERDUE_PENDING_SQL.replace(DENIED_VISIT_SQL, "")
    assert STILL_PENDING_VISIT_SQL not in OVERDUE_PENDING_SQL


def test_denied_sql_reads_sheet_override():
    assert "manual_overrides->>'source_visit_status'" in DENIED_VISIT_SQL
    assert "manual_overrides->>'source_visit_status'" in STILL_PENDING_VISIT_SQL


def test_visit_status_filter_reads_sheet_override():
    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        visit_status=["Denied"],
        queue="collection",
    )
    assert "manual_overrides->>'source_visit_status'" in sql
    assert ["denied"] in params
    assert params[-1] == "denied"


def test_collection_status_and_root_cause_filters():
    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
        collection_status=["Pending"],
        root_cause=["Auth delay"],
    )
    assert "manual_overrides->>'collection_status'" in sql
    assert "facet.collection_status" in sql
    assert "manual_overrides->>'root_cause'" in sql
    assert "ROOTCAUSE" in sql
    assert ["pending"] in params
    assert ["auth delay"] in params


def test_digit_search_matches_account_number():
    sql, params = _build_filters(
        q="10649740",
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
    )
    assert "revflow_patient_id" in sql
    assert "10649740" in params
    assert "%10649740%" in params


def test_collection_blank_filters_and_case_insensitive_insurance():
    from cashflow_db.repository.eligibility import FILTER_BLANK
    from cashflow_db.repository.insurance import is_blank_sql

    named, named_params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=["1199 SEIU National Benefit Fund"],
        status=None,
        assigned_to=None,
        queue="collection",
    )
    assert "lower(btrim(COALESCE(wi.insurance_name, ''))) = ANY(%s)" in named
    assert "wi.insurance_name = ANY(%s)" not in named
    assert ["1199 seiu national benefit fund"] in named_params
    assert is_blank_sql("wi.insurance_name") not in named

    sql, params = _build_filters(
        q=None,
        facility=["Bedstuy", FILTER_BLANK],
        month=None,
        insurance=["Aetna", FILTER_BLANK],
        status=None,
        assigned_to=None,
        visit_status=["Denied", FILTER_BLANK],
        queue="collection",
        collection_status=["Pending", FILTER_BLANK],
        root_cause=[FILTER_BLANK],
    )
    assert "wi.facility_name = ANY(%s)" in sql
    assert "SELECT f.name FROM ref.facility f" in sql
    assert ")), '') IS NULL" in sql
    assert ["Bedstuy"] in params
    assert is_blank_sql("wi.insurance_name") in sql
    assert ["aetna"] in params
    assert " OR " in sql
    assert "manual_overrides->>'source_visit_status'" in sql
    assert "), '') IS NULL" in sql
    assert ["denied"] in params
    assert "manual_overrides->>'collection_status'" in sql
    assert "<> ''" in sql
    assert ["pending"] in params
    assert "manual_overrides->>'root_cause'" in sql
    assert "ROOTCAUSE" in sql
    assert FILTER_BLANK not in params
    assert params[-1] == "denied"

    blank_only, blank_params = _build_filters(
        q=None,
        facility=[FILTER_BLANK],
        month=None,
        insurance=[FILTER_BLANK],
        status=None,
        assigned_to=None,
        visit_status=[FILTER_BLANK],
        queue="collection",
        collection_status=[FILTER_BLANK],
        root_cause=[FILTER_BLANK],
    )
    assert "wi.facility_name = ANY(%s)" not in blank_only
    assert "lower(btrim(COALESCE(wi.insurance_name, ''))) = ANY(%s)" not in blank_only
    assert "= ANY(%s)" not in blank_only
    assert is_blank_sql("wi.insurance_name") in blank_only
    assert "source_visit_status" in blank_only
    assert blank_params == ["denied"]


def test_account_number_sql_avoids_psycopg_percent_placeholder():
    from cashflow_db.repository.eligibility import ACCOUNT_NUMBER_SQL

    assert "PV4%" not in ACCOUNT_NUMBER_SQL
    assert "%'" not in ACCOUNT_NUMBER_SQL
    assert "left(upper(" in ACCOUNT_NUMBER_SQL


def test_waystar_payment_returns_denied_and_overdue_to_eligibility_paid():
    from cashflow_db.repository.eligibility import (
        PAID_OR_DEDUCT_SQL,
        ROUTED_COLLECTION_SQL,
        SHEET_PAID_OR_DEDUCT_SQL,
        SKIPPED_VISIT_SQL,
        WAYSTAR_COLLECTION_EXIT_REASON,
        WAYSTAR_PAID_CHECK_SQL,
        waystar_paid_exit_sql,
    )

    paid_check = WAYSTAR_PAID_CHECK_SQL
    assert "total_remit_amount, 0) > 0" in paid_check
    assert "total_remit_amount, 0) = 0" not in paid_check
    assert "ZEROPAY%%" in paid_check
    assert "[0-9]" in paid_check
    assert "remit_numbers" in paid_check
    assert "waystar_webpt_map" in paid_check
    assert "c.from_date = wi.dos" in paid_check

    sql = waystar_paid_exit_sql()
    assert "source_visit_status = 'paid'" not in sql
    assert "UPDATE ops.eligibility_work_item" not in sql
    assert WAYSTAR_COLLECTION_EXIT_REASON == "Exited collection after payment"
    assert "arbitration" in ROUTED_COLLECTION_SQL
    assert "actiontaken" in ROUTED_COLLECTION_SQL
    assert "submittedwithoutauth" in ROUTED_COLLECTION_SQL

    sheet_sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="sheet",
    )
    assert f"NOT {DENIED_VISIT_SQL}" not in sheet_sql
    assert f"NOT {COLLECTION_VISIT_SQL}" not in sheet_sql
    assert f"NOT {SKIPPED_VISIT_SQL}" in sheet_sql
    assert "'paid'" in SHEET_PAID_OR_DEDUCT_SQL
    assert "'deduct'" in SHEET_PAID_OR_DEDUCT_SQL
    assert "snowflake_visit_kpi" not in SHEET_PAID_OR_DEDUCT_SQL
    assert SHEET_PAID_OR_DEDUCT_SQL in DENIED_VISIT_SQL
    assert SHEET_PAID_OR_DEDUCT_SQL in OVERDUE_PENDING_SQL
    assert PAID_OR_DEDUCT_SQL not in DENIED_VISIT_SQL
    assert PAID_OR_DEDUCT_SQL not in OVERDUE_PENDING_SQL

    refresh_src = (ROOT / "cashflow_db" / "repository" / "eligibility.py").read_text(
        encoding="utf-8"
    )
    refresh_fn = refresh_src.split("def refresh_collection_queue", 1)[1].split(
        "\ndef ", 1
    )[0]
    assert refresh_fn.find("_promote_waystar_paid_collection") < refresh_fn.find(
        "DELETE FROM analytics.collection_queue_member"
    )
    assert 'counts["promoted_paid"]' in refresh_fn
    assert "source_visit_status = 'paid'" not in refresh_fn
    assert "HAS_INSURANCE_PAYMENT_SQL" not in refresh_fn
    assert "SHEET_PAID_OR_DEDUCT_SQL" in refresh_fn

    loader = (ROOT / "cashflow_db" / "loaders" / "load_waystar_claims.py").read_text(
        encoding="utf-8"
    )
    load_fn = loader.split("def load_waystar_claims", 1)[1].split("\ndef ", 1)[0]
    assert "if claims_path:" in load_fn
    assert "refresh_collection_queue" in load_fn
    assert load_fn.find('status="success"') < load_fn.find("refresh_collection_queue")

    generator = (
        ROOT / "cashflow_db" / "services" / "eligibility_generator.py"
    ).read_text(encoding="utf-8")
    generate_fn = generator.split("def generate_eligibility_work_items", 1)[1]
    assert generate_fn.find("upsert_from_visit") < generate_fn.find(
        "refresh_collection_queue"
    )
    assert "refresh_eligibility_sheet_facet" in generate_fn


def test_eligibility_sheet_facet_view_has_unique_status_key():
    from cashflow_db.repository.eligibility import _facet_join

    sql = (ROOT / "cashflow_db" / "sql" / "074_eligibility_sheet_facet.sql").read_text(
        encoding="utf-8"
    )
    assert "analytics.eligibility_sheet_facet" in sql
    assert "CREATE UNIQUE INDEX" in sql
    assert "uq_eligibility_sheet_facet" in sql
    assert "(work_item_id)" in sql
    assert "AS collection_status" in sql
    assert "primary_check_date" in sql
    assert "LEFT JOIN" in _facet_join("facet.collection_status = %s")
    assert _facet_join("wi.dos = %s") == ""
