"""Transaction Tracker (DB) + Mail Checks → bank_deposit, allocations, mail_work_item."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from cashflow_db.config import MAIL_CHECKS_CSV
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.util import safe_str

_EFT_LAST4 = re.compile(r"(\d{4})\s*$")


def _channel_from_type(tx_type: str | None) -> str:
    text = (tx_type or "").strip().lower()
    if "ach" in text or "eft" in text:
        return "eft"
    if "check" in text:
        return "mail_check"
    if "card" in text or "v-card" in text or "vcard" in text:
        return "v_card"
    if "deposit" in text:
        return "direct_deposit"
    return "other"


def load_tracker(
    *,
    path: Path | None = None,
    database_url: str | None = None,
) -> dict[str, int]:
    """Load bank_deposit from active billing.transaction_tracker_row rows.

    ``path`` is ignored (kept for call-site compatibility). Postgres is SoT.
    """
    del path  # file is no longer the runtime source
    counts = {"deposits": 0, "allocations": 0}

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "tracker", "billing.transaction_tracker_row")
        try:
            from cashflow_db.repository import tracker as tracker_repo

            rows = tracker_repo.list_active_for_etl(conn)
            for raw in rows:
                payment_id = safe_str(raw.get("payment_id"))
                if not payment_id:
                    continue

                eft1 = safe_str(raw.get("eft_1"))
                eft2 = safe_str(raw.get("eft_2"))
                if eft1 and eft1.upper() == "#N/A":
                    eft1 = None
                if eft2 and eft2.upper() == "#N/A":
                    eft2 = None
                last4 = None
                for candidate in (eft1, eft2):
                    if candidate:
                        m = _EFT_LAST4.search(candidate)
                        if m:
                            last4 = m.group(1)
                            break

                txn_date = raw.get("txn_date")
                deposit = conn.execute(
                    """
                    INSERT INTO billing.bank_deposit (
                        payment_id_external, channel, check_date_recognized,
                        bank_posting_date, amount, transaction_type, bank_name,
                        description, billing_status, collector, posted, notes,
                        eft_1, eft_2, eft_last4, source_system, source_natural_key,
                        etl_run_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'tracker', %s, %s::uuid
                    )
                    ON CONFLICT (payment_id_external) DO UPDATE
                    SET amount = EXCLUDED.amount,
                        bank_posting_date = COALESCE(EXCLUDED.bank_posting_date, billing.bank_deposit.bank_posting_date),
                        eft_1 = COALESCE(EXCLUDED.eft_1, billing.bank_deposit.eft_1),
                        eft_2 = COALESCE(EXCLUDED.eft_2, billing.bank_deposit.eft_2),
                        etl_run_id = EXCLUDED.etl_run_id
                    RETURNING deposit_id
                    """,
                    (
                        payment_id,
                        _channel_from_type(safe_str(raw.get("transaction_type"))),
                        txn_date,
                        txn_date,
                        raw.get("amount"),
                        safe_str(raw.get("transaction_type")),
                        safe_str(raw.get("bank_name")),
                        safe_str(raw.get("description")),
                        safe_str(raw.get("billing_status")),
                        safe_str(raw.get("collector")),
                        raw.get("posted"),
                        safe_str(raw.get("notes")),
                        eft1,
                        eft2,
                        last4,
                        payment_id,
                        etl_id,
                    ),
                ).fetchone()
                counts["deposits"] += 1
                deposit_id = str(deposit["deposit_id"])

                for method, eft in (("eft1", eft1), ("eft2", eft2)):
                    if not eft:
                        continue
                    matches = conn.execute(
                        """
                        SELECT eob_check_id, paid_amount_sum
                        FROM billing.eob_check
                        WHERE check_eft_num = %s
                        """,
                        (eft,),
                    ).fetchall()
                    for mrow in matches:
                        conn.execute(
                            """
                            INSERT INTO billing.deposit_check_allocation (
                                deposit_id, eob_check_id, allocated_amount,
                                match_method, confidence, etl_run_id
                            )
                            VALUES (%s::uuid, %s::uuid, %s, %s, 0.95, %s::uuid)
                            ON CONFLICT (deposit_id, eob_check_id) DO NOTHING
                            """,
                            (
                                deposit_id,
                                str(mrow["eob_check_id"]),
                                mrow["paid_amount_sum"],
                                method,
                                etl_id,
                            ),
                        )
                        counts["allocations"] += 1

            finish_etl_run(conn, etl_id, status="success", row_count=sum(counts.values()))
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts


def load_mail(
    *,
    path: Path | None = None,
    database_url: str | None = None,
) -> dict[str, int]:
    path = path or MAIL_CHECKS_CSV
    counts = {"mail_items": 0}

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "mail", str(path))
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                del header
                for i, row in enumerate(reader):
                    if not row:
                        continue
                    payer = safe_str(row[0] if len(row) > 0 else None)
                    prev = safe_str(row[2] if len(row) > 2 else None)
                    collector = safe_str(row[3] if len(row) > 3 else None)
                    item_type = safe_str(row[4] if len(row) > 4 else None)
                    notes = safe_str(row[5] if len(row) > 5 else None)
                    if not any([payer, collector, item_type, notes]):
                        continue
                    previously = bool(prev)
                    conn.execute(
                        """
                        INSERT INTO ops.mail_work_item (
                            payer_label, collector, item_type, notes,
                            previously_posted_flag, status, source_system,
                            source_natural_key, etl_run_id
                        )
                        VALUES (%s, %s, %s, %s, %s, 'open', 'mail', %s, %s::uuid)
                        """,
                        (
                            payer,
                            collector,
                            item_type,
                            notes,
                            previously,
                            f"mail:{i}:{payer}:{collector}",
                            etl_id,
                        ),
                    )
                    counts["mail_items"] += 1
            finish_etl_run(conn, etl_id, status="success", row_count=counts["mail_items"])
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
