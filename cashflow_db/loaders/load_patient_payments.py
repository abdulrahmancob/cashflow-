"""WebPT patient_payments CSV → billing.patient_payment."""

from __future__ import annotations

import csv
from pathlib import Path

from cashflow_db.config import PATIENT_PAYMENTS_CSV, WEBPT_OUTPUT
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.loaders.base import (
    normalize_payment_category,
    upsert_case,
    upsert_facility,
    upsert_patient,
)
from cashflow_db.util import parse_date, parse_money, safe_str


def _resolve_payments_csv(path: Path | None) -> Path:
    if path and path.exists():
        return path
    if PATIENT_PAYMENTS_CSV.exists():
        return PATIENT_PAYMENTS_CSV
    for name in (
        "patient_payments_202601_202608.csv",
        "patient_payments_jan_may_2026.csv",
    ):
        cand = WEBPT_OUTPUT / name
        if cand.exists():
            return cand
    raise FileNotFoundError(f"No patient_payments CSV under {WEBPT_OUTPUT}")


def load_patient_payments(
    *,
    path: Path | None = None,
    database_url: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    csv_path = _resolve_payments_csv(path)
    counts = {"rows": 0, "inserted": 0, "skipped": 0}

    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if limit:
        rows = rows[:limit]

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "webpt", str(csv_path), notes="load_patient_payments")
        try:
            for row in rows:
                counts["rows"] += 1
                category = normalize_payment_category(row.get("payment_type"))
                webpt_pid = safe_str(row.get("patient_id"))
                case_id = safe_str(row.get("case_id"))
                if not category or not webpt_pid or not case_id:
                    counts["skipped"] += 1
                    continue

                pid = upsert_patient(
                    conn,
                    webpt_patient_id=webpt_pid,
                    patient_name=row.get("patient_name"),
                    etl_run_id=etl_id,
                )
                if not pid:
                    counts["skipped"] += 1
                    continue
                facility_id = upsert_facility(
                    conn,
                    webpt_facility_id=row.get("facility_id"),
                    name=row.get("facility_name"),
                )
                case_pk = upsert_case(
                    conn,
                    webpt_case_id=case_id,
                    patient_id=pid,
                    facility_id=facility_id,
                    etl_run_id=etl_id,
                )
                service_date = parse_date(
                    row.get("date_of_service_iso") or row.get("date_of_service")
                )
                txn_date = parse_date(row.get("date_of_transaction"))
                visit_id = None
                if case_pk and service_date:
                    v = conn.execute(
                        """
                        SELECT visit_id FROM core.visit
                        WHERE case_pk = %s::uuid AND service_date = %s
                        """,
                        (case_pk, service_date),
                    ).fetchone()
                    if v:
                        visit_id = str(v["visit_id"])

                natural = "|".join(
                    [
                        webpt_pid,
                        case_id,
                        service_date.isoformat() if service_date else "",
                        txn_date.isoformat() if txn_date else "",
                        category,
                        safe_str(row.get("amount_paid")) or "",
                        safe_str(row.get("description")) or "",
                    ]
                )
                existing = conn.execute(
                    """
                    SELECT patient_payment_id FROM billing.patient_payment
                    WHERE source_system = 'webpt' AND source_natural_key = %s
                    """,
                    (natural,),
                ).fetchone()
                params = (
                    pid,
                    case_pk,
                    visit_id,
                    facility_id,
                    service_date,
                    txn_date,
                    category,
                    safe_str(row.get("payment_type")),
                    safe_str(row.get("description")),
                    parse_money(row.get("amount_due")),
                    parse_money(row.get("amount_paid")),
                    safe_str(row.get("paid_method")),
                    safe_str(row.get("credit_type")),
                    safe_str(row.get("auth_check")),
                    parse_money(row.get("total_charge")),
                    parse_money(row.get("total_paid")),
                    parse_money(row.get("balance")),
                    natural,
                    etl_id,
                )
                if existing:
                    conn.execute(
                        """
                        UPDATE billing.patient_payment SET
                            amount_due = %s, amount_paid = %s,
                            visit_id = COALESCE(%s::uuid, visit_id),
                            etl_run_id = %s::uuid
                        WHERE patient_payment_id = %s::uuid
                        """,
                        (
                            parse_money(row.get("amount_due")),
                            parse_money(row.get("amount_paid")),
                            visit_id,
                            etl_id,
                            str(existing["patient_payment_id"]),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO billing.patient_payment (
                            patient_id, case_pk, visit_id, facility_id,
                            service_date, transaction_date, payment_category, payment_type,
                            description, amount_due, amount_paid, paid_method, credit_type,
                            auth_check, total_charge, total_paid, balance,
                            source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            'webpt', %s, %s::uuid
                        )
                        """,
                        params,
                    )
                counts["inserted"] += 1

            finish_etl_run(conn, etl_id, status="success", row_count=counts["inserted"])
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
