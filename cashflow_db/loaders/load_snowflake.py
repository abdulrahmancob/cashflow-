"""Snowflake billing CSV → analytics.snowflake_visit_kpi (EMR+DOS only)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from cashflow_db.config import SNOWFLAKE_BILLING_CSV
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.util import parse_date, parse_money, safe_str


def load_snowflake_kpi(
    *,
    path: Path | None = None,
    database_url: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    csv_path = path or SNOWFLAKE_BILLING_CSV
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    counts = {"rows": 0, "upserted": 0, "skipped": 0}
    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if limit:
        rows = rows[:limit]

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "snowflake", str(csv_path), notes="load_snowflake_kpi")
        try:
            for row in rows:
                counts["rows"] += 1
                emr = safe_str(row.get("EMR_ID"))
                dos = parse_date(row.get("DATE_OF_SERVICE"))
                if not emr or not dos:
                    counts["skipped"] += 1
                    continue

                patient_id = None
                prow = conn.execute(
                    "SELECT patient_id FROM core.patient WHERE webpt_patient_id = %s",
                    (emr,),
                ).fetchone()
                if prow:
                    patient_id = str(prow["patient_id"])

                natural = f"{emr}:{dos.isoformat()}"
                payload = {k: row.get(k) for k in row}
                existing = conn.execute(
                    """
                    SELECT snowflake_visit_kpi_id FROM analytics.snowflake_visit_kpi
                    WHERE emr_id = %s AND date_of_service = %s
                    """,
                    (emr, dos),
                ).fetchone()
                values = (
                    emr,
                    dos,
                    patient_id,
                    safe_str(row.get("PATIENT")),
                    safe_str(row.get("INSURANCE")),
                    safe_str(row.get("CLINIC")),
                    safe_str(row.get("STATUS")),
                    parse_money(row.get("CHARGED_AMOUNT")),
                    parse_money(row.get("INSURANCE_PAYMENT")),
                    parse_money(row.get("CLIENT_PAYMENT")),
                    parse_money(row.get("CO_INSURANCE_PAYMENT")),
                    parse_money(row.get("REDUCTIONS")),
                    parse_money(row.get("ADJUSTED")),
                    safe_str(row.get("VISIT_ID")),
                    safe_str(row.get("ID")),
                    safe_str(row.get("PRIMARY_CHECK_NUMBER")),
                    parse_date(row.get("PRIMARY_CHECK_DATE")),
                    parse_money(row.get("PRIMARY_CHECK_AMOUNT")),
                    safe_str(row.get("SECONDARY_CHECK_NUMBER")),
                    parse_date(row.get("SECONDARY_CHECK_DATE")),
                    parse_money(row.get("SECONDARY_CHECK_AMOUNT")),
                    parse_date(row.get("BILLED_DATE")),
                    json.dumps(payload),
                    natural,
                    etl_id,
                )
                if existing:
                    conn.execute(
                        """
                        UPDATE analytics.snowflake_visit_kpi SET
                            patient_id = COALESCE(%s::uuid, patient_id),
                            patient_name = %s,
                            insurance = %s,
                            clinic = %s,
                            status = %s,
                            charged_amount = %s,
                            insurance_payment = %s,
                            client_payment = %s,
                            co_insurance_payment = %s,
                            reductions = %s,
                            adjusted = %s,
                            sf_visit_id = %s,
                            sf_billing_id = %s,
                            primary_check_number = %s,
                            primary_check_date = %s,
                            primary_check_amount = %s,
                            secondary_check_number = %s,
                            secondary_check_date = %s,
                            secondary_check_amount = %s,
                            billed_date = %s,
                            payload = %s::jsonb,
                            source_natural_key = %s,
                            etl_run_id = %s::uuid
                        WHERE snowflake_visit_kpi_id = %s::uuid
                        """,
                        values[2:] + (str(existing["snowflake_visit_kpi_id"]),),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO analytics.snowflake_visit_kpi (
                            emr_id, date_of_service, patient_id, patient_name,
                            insurance, clinic, status, charged_amount,
                            insurance_payment, client_payment, co_insurance_payment,
                            reductions, adjusted, sf_visit_id, sf_billing_id,
                            primary_check_number, primary_check_date, primary_check_amount,
                            secondary_check_number, secondary_check_date, secondary_check_amount,
                            billed_date, payload, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s, %s, %s::uuid, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s::jsonb, %s, %s::uuid
                        )
                        """,
                        values,
                    )
                counts["upserted"] += 1

            finish_etl_run(conn, etl_id, status="success", row_count=counts["upserted"])
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
