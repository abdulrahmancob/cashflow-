"""Eligibility generate + sheet field flattening."""

from datetime import date
from pathlib import Path

from cashflow_db.repository.eligibility import (
    _build_filters,
    attach_sheet_fields,
    overlay_eft_totals,
    sort_visit_statuses,
)
from cashflow_db.services.eligibility_generator import (
    attach_unique_names,
    visit_queue_action,
)


def test_attach_unique_names_fills_and_leaves_conflicts(monkeypatch):
    from cashflow_db.repository import identity_backfill as id_repo

    monkeypatch.setattr(
        id_repo,
        "lookup_unique_names",
        lambda _c, emrs: {"E1": {"name": "DOE, JANE", "fill_source": "snowflake"}},
    )
    visits = [
        {"webpt_patient_id": "E1", "patient_name": "", "visit_status": "pending"},
        {"webpt_patient_id": "E2", "patient_name": None, "visit_status": "pending"},
        {"webpt_patient_id": "E3", "patient_name": "Already", "visit_status": "pending"},
    ]
    result = attach_unique_names(None, visits)
    assert visits[0]["patient_name"] == "DOE, JANE"
    assert visits[1]["patient_name"] is None
    assert visits[2]["patient_name"] == "Already"
    assert result["filled"] == 1
    assert result["leftover"] == ["E2"]


def test_generate_skips_enqueue_without_name():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "eligibility_generator.py"
    ).read_text(encoding="utf-8")
    assert "skipped_no_name" in text
    assert "attach_unique_names" in text
    assert "persist_names_for_emrs" in text


def test_pending_enqueues():
    assert visit_queue_action("pending") == "enqueue"
    assert visit_queue_action("") == "enqueue"
    assert visit_queue_action(None) == "enqueue"
    assert visit_queue_action("denied") == "enqueue"
    assert visit_queue_action("patient_responsibility") == "enqueue"


def test_settled_closes():
    for st in ("paid", "partial", "deduct"):
        assert visit_queue_action(st) == "close"
        assert visit_queue_action(st.upper()) == "close"


def test_collection_enqueues():
    assert visit_queue_action("collection") == "enqueue"
    assert visit_queue_action("COLLECTION") == "enqueue"


def test_unknown_skips():
    assert visit_queue_action("scheduled") == "skip"
    assert visit_queue_action("blank") == "skip"


def test_attach_sheet_fields_prefers_total_paid():
    row = attach_sheet_fields(
        {
            "reference_number": "ops-ref",
            "context": {
                "total_paid": "41.77",
                "visit_paid_total": "10",
                "primary_check_number": "26050B1000562275",
                "primary_check_date": "2026-02-24T00:00:00",
                "primary_check_amount": "526",
            },
        }
    )
    assert row["paid_amount"] == 41.77
    assert row["check_number"] == "26050B1000562275"
    assert row["check_date"] == "2026-02-24"


def test_attach_sheet_fields_prefers_live_recon_total_paid():
    row = attach_sheet_fields(
        {
            "reference_number": "ops-ref",
            "source_visit_status": "",
            "recon_total_paid": 85.5,
            "recon_check_number": "CHK1",
            "recon_check_date": "2026-07-15T00:00:00",
            "recon_visit_status": "paid",
            "context": {
                "total_paid": 0,
                "primary_check_number": "OLD",
                "primary_check_amount": "526",
            },
        }
    )
    assert row["paid_amount"] == 85.5
    assert row["check_number"] == "CHK1"
    assert row["check_date"] == "2026-07-15"
    assert row["source_visit_status"] == "paid"
    assert row["pending_reason"] is None


def test_attach_sheet_fields_passes_pending_reason():
    row = attach_sheet_fields(
        {
            "source_visit_status": "pending",
            "recon_pending_reason": "pending_tracker",
            "recon_check_number": "91720497",
            "recon_check_date": "2026-08-20",
        }
    )
    assert row["pending_reason"] == "pending_tracker"
    assert row["check_number"] == "91720497"


def test_compact_check_key_strips_separators_and_leading_zeros():
    from cashflow_db.repository.eligibility import compact_check_key

    assert compact_check_key("917-204-97") == "91720497"
    assert compact_check_key("091720497") == "91720497"
    assert compact_check_key("  91720497 ") == "91720497"
    assert compact_check_key("90004573535.0") == "90004573535"
    assert compact_check_key(None) is None
    assert compact_check_key("ABC-12") == "ABC12"


def test_attach_sheet_fields_does_not_use_check_amount():
    row = attach_sheet_fields(
        {
            "reference_number": "ABC123",
            "context": {"primary_check_amount": "526", "primary_check_number": "X"},
        }
    )
    assert row["paid_amount"] is None
    assert row["check_number"] == "X"


def test_attach_sheet_fields_falls_back_to_reference():
    row = attach_sheet_fields({"reference_number": "ABC123", "context": {}})
    assert row["paid_amount"] is None
    assert row["check_number"] == "ABC123"
    assert row["check_date"] is None


def test_sort_visit_statuses_excel_order():
    assert sort_visit_statuses(["Denied", "paid", "pending", "deduct", "PAID", ""]) == [
        "pending",
        "paid",
        "denied",
        "deduct",
    ]


def test_check_date_filter_uses_live_recon_primary_check_date():
    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        check_date=["2026-08-11"],
    )
    assert "primary_check_date" in sql
    assert "reconciliation_visit_agg" in sql
    assert params[-1] == [date(2026, 8, 11)]


def test_search_q_matches_compact_eft():
    sql, params = _build_filters(
        q="917-204-97",
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
    )
    assert "primary_check_number" in sql
    assert "third_check_number" in sql
    assert "91720497" in params
    sql2, params2 = _build_filters(
        q="90004573535.0",
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
    )
    assert "90004573535" in params2
    assert params2.count("90004573535") >= 4


def test_search_q_digits_uses_emr_equality():
    sql, params = _build_filters(
        q="51115440",
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
    )
    assert "wi.emr_patient_id = %s" in sql
    assert "wi.emr_patient_id LIKE %s" in sql
    assert "51115440" in params
    assert "51115440%" in params
    assert "patient_name ILIKE" not in sql
    assert "notes ILIKE" not in sql
    assert "primary_check_number" not in sql
    assert "revflow_patient_id" in sql
    assert "%51115440%" in params


def test_search_q_name_still_uses_ilike():
    sql, params = _build_filters(
        q="TORRES",
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
    )
    assert "patient_name ILIKE %s" in sql
    assert "%TORRES%" in params
    assert "wi.emr_patient_id = %s" not in sql


def test_denied_and_skipped_sql_use_indexed_exists():
    from cashflow_db.repository.eligibility import DENIED_VISIT_SQL, SKIPPED_VISIT_SQL

    assert "EXISTS (" in DENIED_VISIT_SQL
    assert "manual_overrides->>'source_visit_status'" in DENIED_VISIT_SQL
    assert "total_remit_amount" in DENIED_VISIT_SQL
    assert "NULLIF(btrim(el.carcs), '')" in DENIED_VISIT_SQL
    assert "sf.emr_id = wi.emr_patient_id" not in DENIED_VISIT_SQL
    assert "IN ('paid', 'deduct')" in DENIED_VISIT_SQL
    assert "IN (SELECT sf.emr_id" not in DENIED_VISIT_SQL
    assert "EXISTS (" in SKIPPED_VISIT_SQL
    assert "elig_skip_visit" in SKIPPED_VISIT_SQL
    assert "sk.emr_id = wi.emr_patient_id" in SKIPPED_VISIT_SQL
    assert "schedule_appointment" not in SKIPPED_VISIT_SQL
    assert "IN (SELECT sf.emr_id" not in SKIPPED_VISIT_SQL
    assert "IN (SELECT p.webpt_patient_id, v.service_date" not in SKIPPED_VISIT_SQL


def test_filter_options_scopes_distinct_to_sheet_dos_window():
    import inspect

    from cashflow_db.repository.eligibility import filter_options

    src = inspect.getsource(filter_options)
    assert "ELIGIBILITY_MIN_DOS_SQL" in src
    assert "ELIGIBILITY_MAX_DOS_SQL" in src
    assert "dos_window" in src


def test_attach_sheet_fields_uses_recon_check_amounts():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 100,
            "recon_check_number": "111",
            "recon_check_amount": 80,
            "recon_secondary_check_number": "222",
            "recon_secondary_check_amount": 20,
            "recon_third_check_number": "333",
            "recon_third_check_amount": 5,
            "recon_missing_tracker_checks": ["222"],
        }
    )
    assert row["insurance_payment"] == 80
    assert row["coinsurance_payment"] == 20
    assert row["updated_payment"] == 5
    assert row["insurance_check_amount"] == 80
    assert row["secondary_check_amount"] == 20
    assert row["updated_check_amount"] == 5
    assert row["updated_check_number"] == "333"
    assert row.get("secondary_check_tracker_status") is None


def test_attach_sheet_splits_sheepshead_payments():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 96.43,
            "recon_check_number": "816847922",
            "recon_check_date": "2026-07-22",
            "recon_check_amount": 88.78,
            "recon_secondary_check_number": "1257307716",
            "recon_secondary_check_date": "2026-07-30",
            "recon_secondary_check_amount": 7.65,
            "sf_insurance_payment": 96.43,
        }
    )
    assert row["insurance_payment"] == 88.78
    assert row["coinsurance_payment"] == 7.65
    assert row["insurance_check_number"] == "816847922"
    assert row["secondary_check_number"] == "1257307716"
    assert row["total_amount"] == 96.43


def test_attach_sheet_oa23_splits_same_check():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 93.04,
            "recon_check_number": "111",
            "recon_check_amount": 93.04,
            "recon_oa23_amount": 19.18,
            "recon_oa23_check": "111",
        }
    )
    assert row["insurance_payment"] == 73.86
    assert row["paid_amount"] == 73.86
    assert row["coinsurance_payment"] == 19.18
    assert row["total_amount"] == 93.04
    assert not row.get("secondary_check_number")


def test_attach_sheet_rtm_is_not_added_to_total():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 100,
            "recon_check_amount": 100,
            "recon_rtm_amount": 25,
        }
    )
    assert row["rtm"] == 25
    assert row["insurance_payment"] == 100
    assert row["total_amount"] == 100


def test_overlay_rtm_sql_sums_paid_for_rtm_cpts():
    import inspect

    from cashflow_db.repository.eligibility import overlay_rtm_amounts

    src = inspect.getsource(overlay_rtm_amounts)
    assert "paid_amount" in src
    for code in ("98975", "98977", "98979", "98980", "98985"):
        assert code in src


def test_attach_sheet_oa23_skips_separate_secondary_check():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 96.43,
            "recon_check_number": "816847922",
            "recon_check_amount": 88.78,
            "recon_secondary_check_number": "1257307716",
            "recon_secondary_check_amount": 7.65,
            "recon_oa23_amount": 19.18,
            "recon_oa23_check": "816847922",
        }
    )
    assert row["insurance_payment"] == 88.78
    assert row["coinsurance_payment"] == 7.65
    assert row["total_amount"] == 96.43


def test_attach_sheet_oa23_skips_manual_payment_override():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 93.04,
            "recon_check_number": "111",
            "recon_check_amount": 93.04,
            "recon_oa23_amount": 19.18,
            "manual_overrides": {"insurance_payment": 93.04},
        }
    )
    assert row["insurance_payment"] == 93.04
    assert not row.get("coinsurance_payment")


def test_attach_sheet_swaps_when_coins_greater_than_primary():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_total_paid": 96.43,
            "recon_check_number": "SMALL",
            "recon_check_date": "2026-07-22",
            "recon_check_amount": 7.65,
            "recon_secondary_check_number": "LARGE",
            "recon_secondary_check_date": "2026-07-30",
            "recon_secondary_check_amount": 88.78,
        }
    )
    assert row["insurance_payment"] == 88.78
    assert row["paid_amount"] == 88.78
    assert row["coinsurance_payment"] == 7.65
    assert row["insurance_check_number"] == "LARGE"
    assert row["insurance_check_date"] == "2026-07-30"
    assert row["insurance_check_amount"] == 88.78
    assert row["secondary_check_number"] == "SMALL"
    assert row["secondary_check_date"] == "2026-07-22"
    assert row["secondary_check_amount"] == 7.65
    assert row["check_number"] == "LARGE"
    assert row["total_amount"] == 96.43


def test_attach_sheet_places_hidden_missing_eft():
    row = attach_sheet_fields(
        {
            "source_visit_status": "pending",
            "recon_visit_status": "paid",
            "recon_pending_reason": "pending_tracker",
            "recon_check_number": "817000833",
            "recon_check_amount": 66.70,
            "sf_insurance_check_number": "817000833",
            "recon_missing_tracker_checks": ["369506761"],
        }
    )
    assert row["source_visit_status"] == "paid"
    assert row["pending_reason"] == "pending_tracker"
    assert row["insurance_check_number"] == "817000833"
    assert row["secondary_check_number"] == "369506761"
    assert row.get("secondary_check_tracker_status") is None


def test_attach_sheet_details_does_not_tag_missing_eft():
    row = attach_sheet_fields(
        {
            "source_visit_status": "paid",
            "recon_visit_status": "paid",
            "recon_pending_reason": "pending_tracker",
            "recon_check_number": "111",
            "recon_secondary_check_number": "222",
            "recon_third_check_number": "333",
            "recon_fourth_check_number": "444",
            "recon_missing_tracker_checks": ["555"],
        }
    )
    details = row.get("details") or ""
    assert "Paid (Pending Tracker)" not in details
    assert "555" not in details


def test_overlay_eft_totals_uses_compact_remit_sum(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetchall(_conn, sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {"compact": "90004573535", "eft_total": 120.5},
            {"compact": "91720497", "eft_total": 40},
        ]

    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchall", fake_fetchall)
    rows = [
        {
            "insurance_check_number": "90004573535.0",
            "insurance_check_amount": 10,
            "secondary_check_number": "917-204-97",
            "secondary_check_amount": 5,
        }
    ]
    overlay_eft_totals(None, rows)  # type: ignore[arg-type]
    assert rows[0]["insurance_check_amount"] == 120.5
    assert rows[0]["secondary_check_amount"] == 40
    assert "90004573535" in captured["params"][0]
    assert "91720497" in captured["params"][0]
    assert "waystar_claim" in str(captured["sql"])
    assert "transaction_tracker_row" in str(captured["sql"])


def test_check_date_filter_invalid_matches_nothing():
    sql, params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        check_date=["not-a-date"],
    )
    assert "FALSE" in sql
    assert params == []


def test_sheet_sortable_includes_visible_columns():
    from cashflow_db.repository.eligibility import _SORTABLE

    for key in (
        "patient_name",
        "emr_patient_id",
        "dos",
        "insurance_name",
        "paid_amount",
        "check_number",
        "check_date",
        "tracker_date",
        "source_visit_status",
        "notes",
        "facility_name",
        "assigned_to",
    ):
        assert key in _SORTABLE
    assert "COALESCE(f.name, wi.facility_name)" in _SORTABLE["facility_name"]
    assert "collector_code" in _SORTABLE["assigned_to"]
    assert "insurance_payment" in _SORTABLE


def test_portal_sheet_includes_claim_money_columns():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2]
        / "rcm_portal"
        / "src"
        / "pages"
        / "EligibilityQueue.tsx"
    ).read_text(encoding="utf-8")
    for needle in (
        "Updated Payment",
        "Reduction",
        "Co-Insurance",
        "Add adjustment",
        "Search patient, EMR, EFT, notes",
        "Paid (Pending Tracker)",
        "added_amount",
        "/adjustments",
        "SHEET_COLUMNS",
        "COLLECTION_COLUMNS",
        "queue",
    ):
        assert needle in text
    assert "Client Payment" not in text
    assert "label: 'Charged'" not in text
    assert "label: 'Adjusted'" not in text
    assert "setQueue" not in text
    layout = (
        Path(__file__).resolve().parents[2]
        / "rcm_portal"
        / "src"
        / "components"
        / "Layout.tsx"
    ).read_text(encoding="utf-8")
    app = (
        Path(__file__).resolve().parents[2] / "rcm_portal" / "src" / "App.tsx"
    ).read_text(encoding="utf-8")
    assert "to: '/collection'" in layout
    assert "<CollectionPage />" in app
    assert 'path="/collection"' in app


def test_sheet_unknown_sort_falls_back_to_dos(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetchone(conn, sql, params=None):
        return {"n": 0}

    def fake_fetchall(conn, sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchone", fake_fetchone)
    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchall", fake_fetchall)
    from cashflow_db.repository import eligibility

    eligibility.list_work_items(None, sort_by="not_a_column", sort_dir="asc")  # type: ignore[arg-type]
    sql = str(captured["sql"])
    assert "ORDER BY wi.dos ASC NULLS LAST" in sql
    order_by = sql.split("ORDER BY", 1)[1]
    assert "reconciliation_visit_agg" not in order_by


def test_sheet_paid_sort_joins_recon(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetchone(conn, sql, params=None):
        return {"n": 0}

    def fake_fetchall(conn, sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchone", fake_fetchone)
    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchall", fake_fetchall)
    from cashflow_db.repository import eligibility

    eligibility.list_work_items(None, sort_by="paid_amount", sort_dir="desc")  # type: ignore[arg-type]
    sql = str(captured["sql"])
    assert "reconciliation_visit_agg" in sql
    assert "manual_overrides->>'paid_amount'" in sql
    assert "trk.txn_date" not in sql


def test_sheet_tracker_sort_joins_tracker(monkeypatch):
    captured: dict[str, object] = {}

    def fake_fetchone(conn, sql, params=None):
        return {"n": 0}

    def fake_fetchall(conn, sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchone", fake_fetchone)
    monkeypatch.setattr("cashflow_db.repository.eligibility.client.fetchall", fake_fetchall)
    from cashflow_db.repository import eligibility

    eligibility.list_work_items(None, sort_by="tracker_date")  # type: ignore[arg-type]
    sql = str(captured["sql"])
    assert "transaction_tracker_row" in sql
    assert "reconciliation_visit_agg" in sql
    assert "ORDER BY trk.txn_date DESC NULLS LAST" in sql


def test_export_keyset_pages_do_not_overlap_or_recount(monkeypatch):
    from cashflow_db.repository import eligibility

    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    third = "33333333-3333-4333-8333-333333333333"
    pages = {
        None: [_export_row(first), _export_row(second)],
        second: [_export_row(third)],
    }
    queries: list[tuple[str, object]] = []

    def fake_fetchone(conn, sql, params=None):
        raise AssertionError(f"export must not count: {sql}")

    def fake_fetchall(conn, sql, params=None):
        text = str(sql)
        if "ORDER BY wi.work_item_id" not in text:
            return []
        assert "OFFSET" not in text
        assert "count(" not in text.lower()
        after = None if params is None else params[-2]
        queries.append((text, after))
        return list(pages[after])

    monkeypatch.setattr(eligibility.client, "fetchone", fake_fetchone)
    monkeypatch.setattr(eligibility.client, "fetchall", fake_fetchall)
    for name in (
        "overlay_live_sf",
        "overlay_live_recon",
        "overlay_pr1_reductions",
        "overlay_rtm_amounts",
        "overlay_oa23_amounts",
        "overlay_denial_reasons",
        "overlay_tracker_dates",
        "overlay_eft_totals",
        "overlay_ledger_totals",
    ):
        monkeypatch.setattr(eligibility, name, lambda *_a, **_k: None)

    rows = list(
        eligibility.iter_export_work_items(None, queue="sheet", batch_size=2)  # type: ignore[arg-type]
    )
    ids = [row["work_item_id"] for row in rows]
    assert ids == [first, second, third]
    assert len(ids) == len(set(ids))
    assert len(queries) == 2
    assert queries[0][1] is None
    assert queries[1][1] == second
    assert set(ids[:2]).isdisjoint({ids[2]})


def test_export_scans_tracker_and_waystar_once(monkeypatch):
    from cashflow_db.repository import eligibility

    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    third = "33333333-3333-4333-8333-333333333333"

    def _checked(work_item_id: str) -> dict:
        row = _export_row(work_item_id)
        row["context"] = {"primary_check_number": "EFT100"}
        return row

    pages = {
        None: [_checked(first), _checked(second)],
        second: [_checked(third)],
    }
    tracker_sql = 0
    waystar_sql = 0

    def fake_fetchall(conn, sql, params=None):
        nonlocal tracker_sql, waystar_sql
        text = str(sql)
        if "ORDER BY wi.work_item_id" in text:
            after = None if params is None else params[-2]
            return list(pages[after])
        if "transaction_tracker_row" in text:
            tracker_sql += 1
        if "waystar_claim" in text:
            waystar_sql += 1
        return []

    monkeypatch.setattr(eligibility.client, "fetchone", lambda *_a, **_k: {"n": 0})
    monkeypatch.setattr(eligibility.client, "fetchall", fake_fetchall)
    for name in (
        "overlay_live_sf",
        "overlay_live_recon",
        "overlay_pr1_reductions",
        "overlay_rtm_amounts",
        "overlay_oa23_amounts",
        "overlay_denial_reasons",
        "overlay_ledger_totals",
    ):
        monkeypatch.setattr(eligibility, name, lambda *_a, **_k: None)

    rows = list(
        eligibility.iter_export_work_items(None, queue="sheet", batch_size=2)  # type: ignore[arg-type]
    )
    assert [row["work_item_id"] for row in rows] == [first, second, third]
    assert tracker_sql == 2
    assert waystar_sql == 1


def test_export_sf_overlay_projects_payload_keys(monkeypatch):
    from cashflow_db.repository import eligibility

    captured: dict[str, str] = {}

    def fake_fetchall(conn, sql, params=None):
        captured["sql"] = str(sql)
        return [
            {
                "emr_id": "1",
                "date_of_service": date(2026, 1, 2),
                "p3": "42.5",
            }
        ]

    monkeypatch.setattr(eligibility.client, "fetchall", fake_fetchall)
    rows = [{"emr_patient_id": "1", "dos": date(2026, 1, 2)}]
    eligibility.overlay_live_sf(None, rows, full_payload=False)  # type: ignore[arg-type]
    sql = captured["sql"]
    assert "payload->>'UPDATED_PAYMENT'" in sql
    assert "sf.payload," not in sql
    assert "sf.payload\n" not in sql
    assert rows[0]["sf_updated_payment"] == 42.5

    eligibility.overlay_live_sf(None, rows)  # type: ignore[arg-type]
    assert "sf.payload" in captured["sql"]
    assert "payload->>'" not in captured["sql"]


def _export_row(work_item_id: str) -> dict:
    return {
        "work_item_id": work_item_id,
        "emr_patient_id": "100",
        "dos": date(2026, 1, 2),
        "context": {},
        "manual_overrides": {},
        "source_visit_status": "pending",
    }


def test_sheet_queue_keeps_denied_and_pr3():
    from cashflow_db.repository.eligibility import (
        COLLECTION_VISIT_SQL,
        DENIED_VISIT_SQL,
        PR3_UNPAID_SQL,
        SKIPPED_VISIT_SQL,
    )
    from cashflow_db.repository.visits import (
        ELIGIBILITY_MAX_DOS_SQL,
        ELIGIBILITY_MIN_DOS_SQL,
        KEEP_WORK_ITEM_SQL,
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
    assert f"NOT {DENIED_VISIT_SQL}" not in sql
    assert f"NOT {COLLECTION_VISIT_SQL}" not in sql
    assert f"NOT {PR3_UNPAID_SQL}" not in sql
    assert PR3_UNPAID_SQL not in sql
    assert f"NOT {SKIPPED_VISIT_SQL}" in sql
    assert "manual_overrides->>'source_visit_status'" not in sql
    assert ELIGIBILITY_MIN_DOS_SQL in sql
    assert ELIGIBILITY_MAX_DOS_SQL in sql
    assert KEEP_WORK_ITEM_SQL in sql
    assert "elig_keep_visit" in sql
    assert "elig_skip_visit" in sql
    assert "cancelled" in sql
    assert "no_show" in sql
    assert "visit_service_line" not in sql


def test_sheet_queue_requires_billed_note_or_cpt():
    from cashflow_db.repository.visits import KEEP_WORK_ITEM_SQL

    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="sheet",
    )
    assert KEEP_WORK_ITEM_SQL in sql
    assert "elig_keep_visit" in sql
    assert "visit_service_line" not in sql
    assert "charged_amount" not in sql


def test_collection_queue_requires_billed_membership():
    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
    )
    assert "analytics.collection_queue_member" in sql
    assert "charged_amount" not in sql
    refresh = (
        Path(__file__).resolve().parents[1] / "repository" / "eligibility.py"
    ).read_text(encoding="utf-8")
    assert "KEEP_WORK_ITEM_SQL" in refresh
    assert "analytics.collection_queue_member" in refresh


def test_collection_queue_keeps_skipped_visits_filter_off():
    from cashflow_db.repository.eligibility import SKIPPED_VISIT_SQL

    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="collection",
    )
    assert f"NOT {SKIPPED_VISIT_SQL}" not in sql


def test_collection_queue_is_denied_only():
    from cashflow_db.repository.eligibility import (
        COLLECTION_QUEUE_MEMBER_SQL,
        COLLECTION_VISIT_SQL,
        DENIED_VISIT_SQL,
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
    )
    predicate = collection_bucket_predicate(None)
    assert COLLECTION_QUEUE_MEMBER_SQL in sql
    assert params == ["denied"]
    assert DENIED_VISIT_SQL in predicate
    assert COLLECTION_VISIT_SQL not in predicate


def test_collection_bucket_is_collection_status():
    from cashflow_db.repository.eligibility import (
        COLLECTION_QUEUE_MEMBER_SQL,
        DENIED_VISIT_SQL,
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
    assert DENIED_VISIT_SQL not in predicate


def test_sheet_queue_does_not_hide_pr3_unpaid():
    from cashflow_db.repository.eligibility import (
        PR1_SQL_PATTERN,
        PR3_SQL_PATTERN,
        PR3_UNPAID_SQL,
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
    assert f"NOT {PR3_UNPAID_SQL}" not in sql
    assert PR3_UNPAID_SQL not in sql
    assert PR3_SQL_PATTERN not in sql
    assert PR1_SQL_PATTERN not in sql
    assert "source_visit_status" in PR3_UNPAID_SQL
    assert "NOT IN ('paid', 'partial')" in PR3_UNPAID_SQL
    assert "= 'patient_responsibility'" not in PR3_UNPAID_SQL


def test_pr3_queue_lists_unpaid_pr3():
    from cashflow_db.repository.eligibility import (
        DENIED_VISIT_SQL,
        PR1_SQL_PATTERN,
        PR3_SQL_PATTERN,
        PR3_UNPAID_SQL,
        SKIPPED_VISIT_SQL,
    )

    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="patient_responsibility",
    )
    assert PR3_UNPAID_SQL in sql
    assert f"NOT {PR3_UNPAID_SQL}" not in sql
    assert f"NOT {DENIED_VISIT_SQL}" not in sql
    assert f"NOT {SKIPPED_VISIT_SQL}" in sql
    assert PR3_SQL_PATTERN in sql
    assert "PR-?3" in sql
    assert PR1_SQL_PATTERN not in sql
    assert "waystar_webpt_map" in sql
    assert "pr_oa_codes" in sql
    assert "NOT IN ('paid', 'partial')" in sql
    assert "= 'patient_responsibility'" not in PR3_UNPAID_SQL


def test_pr3_alias_queue_matches_patient_responsibility():
    from cashflow_db.repository.eligibility import PR3_UNPAID_SQL

    sql, _params = _build_filters(
        q=None,
        facility=None,
        month=None,
        insurance=None,
        status=None,
        assigned_to=None,
        queue="pr3",
    )
    assert PR3_UNPAID_SQL in sql
    assert f"NOT {PR3_UNPAID_SQL}" not in sql
