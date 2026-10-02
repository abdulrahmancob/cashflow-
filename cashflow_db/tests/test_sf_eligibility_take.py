"""Pending Snowflake copy and the difference rows that remain after it."""

from cashflow_db.services.sf_eligibility_take import (
    remaining_differences,
    sheet_values_from_sf,
    should_copy_visit,
)


def test_copy_only_when_sheet_is_pending_and_snowflake_is_not():
    assert should_copy_visit("pending", "paid")
    assert should_copy_visit("pending", "Denied")
    assert should_copy_visit("pending", "patient_responsibility")
    assert should_copy_visit(None, "collection")
    assert not should_copy_visit("pending", "pending")
    assert not should_copy_visit("pending", "")
    assert not should_copy_visit("pending", "blank")
    assert not should_copy_visit("paid", "denied")
    assert not should_copy_visit("patient_responsibility", "paid")


def test_sheet_values_keep_snowflake_status_and_payment():
    values = sheet_values_from_sf(
        {
            "sf_status": "Paid",
            "sf_insurance_payment": "120.5",
            "sf_insurance_check_number": " 9981 ",
            "sf_details": "posted",
            "sf_client_payment": None,
        }
    )
    assert values["source_visit_status"] == "paid"
    assert values["insurance_payment"] == 120.5
    assert values["paid_amount"] == 120.5
    assert values["insurance_check_number"] == "9981"
    assert values["check_number"] == "9981"
    assert values["details"] == "posted"
    assert "client_payment" not in values


def test_matching_visit_is_not_a_difference():
    assert (
        remaining_differences(
            emr="E1",
            dos="2026-02-01",
            local_status="paid",
            local_amount=10,
            sf_status="Paid",
            sf_amount=10,
            on_sheet=True,
            on_snowflake=True,
        )
        == []
    )


def test_status_amount_and_presence_differences():
    assert remaining_differences(
        emr="E1",
        dos="2026-02-01",
        local_status="paid",
        local_amount=10,
        sf_status="denied",
        sf_amount=0,
        on_sheet=True,
        on_snowflake=True,
    ) == [
        ("E1", "2026-02-01", "status", "paid", "denied"),
        ("E1", "2026-02-01", "amount", "10.00", "0.00"),
    ]
    assert remaining_differences(
        emr="E2",
        dos="2026-03-01",
        local_status="pending",
        local_amount=0,
        sf_status=None,
        sf_amount=None,
        on_sheet=True,
        on_snowflake=False,
    ) == [("E2", "2026-03-01", "only-on-sheet", "pending", "")]
    assert remaining_differences(
        emr="E3",
        dos="2026-04-01",
        local_status=None,
        local_amount=None,
        sf_status="collection",
        sf_amount=5,
        on_sheet=False,
        on_snowflake=True,
    ) == [("E3", "2026-04-01", "only-on-snowflake", "", "collection")]
