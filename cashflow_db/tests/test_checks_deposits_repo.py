"""Checks & Deposits repository diff and versioning (no live DB)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cashflow_db.repository import checks_deposits as cd
from cashflow_db.repository.portal_activity import checks_deposits_row_label, entity_href


def _row(**overrides):
    base = {
        "row_id": "11111111-1111-1111-1111-111111111111",
        "sheet_key": "1|2026-01-05|90.00|0",
        "check_date": date(2026, 1, 2),
        "payer": "Geico",
        "amount": Decimal("90.00"),
        "check_number": "1",
        "deposit_date": date(2026, 1, 5),
        "deposit_month": date(2026, 1, 1),
        "link": None,
        "notes": None,
        "version": 1,
        "deleted_at": None,
    }
    base.update(overrides)
    return base


def _incoming(**overrides):
    base = {
        "sheet_key": "1|2026-01-05|90.00|0",
        "check_date": "2026-01-02",
        "payer": "Geico",
        "amount": "90.00",
        "check_number": "1",
        "deposit_date": "2026-01-05",
        "deposit_month": "2026-01-01",
        "link": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def test_repository_exports():
    from cashflow_db import repository

    assert repository.checks_deposits is cd
    assert callable(cd.build_upload_diff)
    assert callable(cd.import_parsed_rows)
    assert callable(cd.available_months)


def test_build_upload_diff_add_update_unchanged_delete(monkeypatch):
    same = _row()
    changed = _row(
        row_id="22222222-2222-2222-2222-222222222222",
        sheet_key="2|2026-01-05|10.00|0",
        check_number="2",
        amount=Decimal("10.00"),
        payer="Old",
        version=4,
    )
    missing = _row(
        row_id="33333333-3333-3333-3333-333333333333",
        sheet_key="3|2026-01-05|5.00|0",
        check_number="3",
        amount=Decimal("5.00"),
        version=2,
    )
    monkeypatch.setattr(cd.client, "fetchall", lambda *args, **kwargs: [same, changed, missing])

    diff = cd.build_upload_diff(
        None,
        [
            _incoming(),
            _incoming(
                sheet_key="2|2026-01-05|10.00|0",
                check_number="2",
                amount="10.00",
                payer="New",
            ),
            _incoming(
                sheet_key="9|2026-02-01|1.00|0",
                check_number="9",
                amount="1.00",
                deposit_date="2026-02-01",
                deposit_month="2026-02-01",
                payer="New payer",
            ),
        ],
    )
    assert diff["counts"] == {"adds": 1, "updates": 1, "unchanged": 1, "soft_deletes": 1}
    assert diff["updates"][0]["row_id"] == changed["row_id"]
    assert diff["updates"][0]["version"] == 4
    assert diff["soft_deletes"][0]["sheet_key"] == missing["sheet_key"]
    assert diff["month_bounds"][0]["from"] == "2026-01-01"


def test_update_row_version_conflict_does_not_write(monkeypatch):
    current = _row(version=2)
    writes: list[str] = []
    monkeypatch.setattr(cd.client, "fetchone", lambda *args, **kwargs: current)
    monkeypatch.setattr(cd.client, "execute", lambda *args, **kwargs: writes.append("audit"))

    result = cd.update_row(None, current["row_id"], {"payer": "X"}, version=1, actor_user_id="u")
    assert result is not None
    assert result["__conflict__"] is True
    assert result["current"]["version"] == 2
    assert writes == []


def test_soft_delete_then_restore_clash(monkeypatch):
    state = _row()

    def fetchone(conn, sql, params=None):
        if "deleted_at = now()" in sql:
            state["deleted_at"] = "2026-01-06T00:00:00Z"
            state["version"] = 2
            return dict(state)
        if "sheet_key = %s" in sql:
            return {"row_id": "other"}
        return dict(state)

    monkeypatch.setattr(cd.client, "fetchone", fetchone)
    monkeypatch.setattr(cd.client, "execute", lambda *args, **kwargs: None)

    deleted = cd.soft_delete_row(None, state["row_id"], version=1, actor_user_id="u")
    assert deleted is not None
    assert deleted["deleted_at"]
    with pytest.raises(ValueError, match="already active"):
        cd.restore_row(None, state["row_id"], version=2, actor_user_id="u")


def test_apply_upload_payload_retries_conflict(monkeypatch):
    monkeypatch.setattr(cd, "create_row", lambda *args, **kwargs: {"row_id": "a"})

    def update_row(conn, row_id, data, *, version, actor_user_id, upload_batch_id=None, action="update"):
        if version == 1:
            return {
                "__conflict__": True,
                "current": {"row_id": row_id, "version": 4, "deleted_at": None},
            }
        assert version == 4
        return {"row_id": row_id}

    monkeypatch.setattr(cd, "update_row", update_row)
    monkeypatch.setattr(cd, "soft_delete_row", lambda *args, **kwargs: {"row_id": "c"})

    counts = cd.apply_upload_payload(
        None,
        {
            "adds": [_incoming(sheet_key="new|2026-01-05|1.00|0", amount="1.00")],
            "updates": [
                {
                    "row_id": "22222222-2222-2222-2222-222222222222",
                    "version": 1,
                    "after": _incoming(payer="New"),
                }
            ],
            "soft_deletes": [{"row_id": "33333333-3333-3333-3333-333333333333", "version": 1}],
        },
        actor_user_id="user-1",
    )
    assert counts["adds"] == 1
    assert counts["updates"] == 1
    assert counts["soft_deletes"] == 1
    assert counts["upload_batch_id"]


def test_activity_label_and_href():
    assert entity_href("checks_deposits_row", "x", "checks_deposits") == "/checks-deposits"
    assert entity_href("checks_deposits_grant", "x", "checks_deposits") == "/checks-deposits"
    label = checks_deposits_row_label(
        {"check_number": "69017024", "deposit_date": date(2026, 1, 5)}
    )
    assert "69017024" in label
    assert "Checks & Deposits" in label
