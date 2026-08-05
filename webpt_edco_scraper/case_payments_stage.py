"""Independent payments stage — JSON-first ledger + summary (not in PDF wave)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from case_artifact_contract import (
    save_raw_json_with_meta,
    update_audit,
    write_case_sources,
    write_json,
)
from case_paths import case_root, ensure_case_layout
from config import BASE_URL
from logging_config import get_logger
from case_raw_capture import extract_payments_json_from_html
from patient_payments_api import (
    PaymentTxn,
    parse_patient_payments_html,
    patient_payments_url,
)

log = get_logger("case_payments_stage")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def payments_dir(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    ensure_case_layout(base_dir, facility_id, case_id)
    root = case_root(base_dir, facility_id, case_id) / "payments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def classify_txn_bucket(txn: PaymentTxn | dict[str, Any]) -> str:
    if isinstance(txn, PaymentTxn):
        ptype = (txn.payment_type or "").lower()
        desc = (txn.description or "").lower()
        due = float(txn.amount_due or 0)
        paid = float(txn.amount_paid or 0)
    else:
        ptype = str(txn.get("type") or txn.get("payment_type") or "").lower()
        desc = str(txn.get("description") or "").lower()
        due = float(txn.get("amountDue") or txn.get("amount_due") or 0)
        paid = float(txn.get("amountPaid") or txn.get("amount_paid") or 0)
    blob = f"{ptype} {desc}"
    if "adjust" in blob or "write" in blob or "wo " in blob:
        return "adjustment"
    if paid and not due:
        return "payment"
    if due and not paid:
        return "charge"
    if "copay" in blob or "patient" in blob:
        return "patient"
    if "insur" in blob or "eob" in blob:
        return "insurance"
    if paid:
        return "payment"
    if due:
        return "charge"
    return "other"


def build_payments_summary(
    txns: list[PaymentTxn] | list[dict[str, Any]],
    totals: dict[str, float] | None = None,
) -> dict[str, Any]:
    charges = 0.0
    payments = 0.0
    adjustments = 0.0
    insurance = 0.0
    patient = 0.0
    for t in txns:
        bucket = classify_txn_bucket(t)
        if isinstance(t, PaymentTxn):
            due = float(t.amount_due or 0)
            paid = float(t.amount_paid or 0)
        else:
            due = float(t.get("amountDue") or t.get("amount_due") or 0)
            paid = float(t.get("amountPaid") or t.get("amount_paid") or 0)
        if bucket == "charge":
            charges += due
        elif bucket == "payment":
            payments += paid
        elif bucket == "adjustment":
            adjustments += abs(due) + abs(paid)
        elif bucket == "insurance":
            insurance += paid or due
        elif bucket == "patient":
            patient += paid or due
        else:
            charges += due
            payments += paid
    totals = totals or {}
    total_charge = float(totals.get("total_charge") or charges)
    total_paid = float(totals.get("total_paid") or payments)
    balance = float(
        totals.get("balance")
        if totals.get("balance") is not None
        else (total_charge - total_paid)
    )
    return {
        "Charges": round(total_charge, 2),
        "Payments": round(total_paid, 2),
        "Adjustments": round(adjustments, 2),
        "Balance": round(balance, 2),
        "Insurance": round(insurance, 2),
        "Patient": round(patient, 2),
        "txn_count": len(txns),
        "updated_at": _utc(),
    }


def store_payments_from_json(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    transactions: list[dict[str, Any]],
    totals: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Write payments/ from raw JSON array (preferred source of truth)."""
    pdir = payments_dir(base_dir, facility_id, case_id)
    case_dir = case_root(base_dir, facility_id, case_id)
    save_raw_json_with_meta(
        pdir / "payments.json",
        transactions,
        facility_id=facility_id,
        case_id=case_id,
        endpoint="/patient/transaction/chart#transactions",
    )
    # Normalize via HTML parser path when possible for consistent PaymentTxn fields
    txns: list[PaymentTxn] = []
    parsed_totals = dict(totals or {})
    # Build synthetic HTML blob? Better: map dicts directly
    fieldnames = [
        "transaction_id",
        "date_of_service",
        "date_of_transaction",
        "payment_type",
        "description",
        "amount_due",
        "amount_paid",
        "paid_method",
        "credit_type",
        "auth_check",
        "status",
        "case_id",
        "facility_id",
        "patient_id",
        "payment_date",
        "bucket",
        "extras_json",
    ]
    rows: list[dict[str, Any]] = []
    for item in transactions:
        if not isinstance(item, dict):
            continue
        txn = PaymentTxn(
            date_of_service=str(item.get("dateOfService") or ""),
            date_of_transaction=str(item.get("dateOfTransaction") or ""),
            payment_type=str(item.get("type") or ""),
            description=str(item.get("description") or ""),
            amount_due=float(item.get("amountDue") or 0),
            amount_paid=float(item.get("amountPaid") or 0),
            paid_method=str(item.get("paidMethodType") or ""),
            credit_type=str(item.get("creditType") or ""),
            auth_check=str(item.get("checkAuthorizationNumber") or ""),
            transaction_id=str(item.get("transactionId") or ""),
            status=str(item.get("status") or ""),
            case_id=str(item.get("caseId") or case_id),
            facility_id=str(item.get("facilityId") or facility_id),
            patient_id=str(item.get("patientId") or ""),
            payment_date=str(item.get("paymentDate") or ""),
        )
        txns.append(txn)
        known = {
            "dateOfService",
            "dateOfTransaction",
            "type",
            "description",
            "amountDue",
            "amountPaid",
            "paidMethodType",
            "creditType",
            "checkAuthorizationNumber",
            "transactionId",
            "status",
            "caseId",
            "facilityId",
            "patientId",
            "paymentDate",
        }
        extras = {k: item[k] for k in item if k not in known}
        rows.append(
            {
                "transaction_id": txn.transaction_id,
                "date_of_service": txn.date_of_service,
                "date_of_transaction": txn.date_of_transaction,
                "payment_type": txn.payment_type,
                "description": txn.description,
                "amount_due": txn.amount_due,
                "amount_paid": txn.amount_paid,
                "paid_method": txn.paid_method,
                "credit_type": txn.credit_type,
                "auth_check": txn.auth_check,
                "status": txn.status,
                "case_id": txn.case_id,
                "facility_id": txn.facility_id,
                "patient_id": txn.patient_id,
                "payment_date": txn.payment_date,
                "bucket": classify_txn_bucket(txn),
                "extras_json": json.dumps(extras, default=str),
            }
        )

    if not parsed_totals:
        parsed_totals = {
            "total_charge": sum(t.amount_due for t in txns),
            "total_paid": sum(t.amount_paid for t in txns),
            "balance": 0.0,
        }
        parsed_totals["balance"] = (
            parsed_totals["total_charge"] - parsed_totals["total_paid"]
        )

    csv_path = pdir / "transactions.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    summary = build_payments_summary(txns, parsed_totals)
    save_raw_json_with_meta(
        pdir / "summary.json",
        summary,
        facility_id=facility_id,
        case_id=case_id,
        endpoint="/patient/transaction/chart#summary",
    )

    update_audit(case_dir, flag="payments_complete", value=True)
    write_case_sources(case_dir)
    return {
        "txn_count": len(rows),
        "summary": summary,
        "payments_dir": str(pdir),
    }


def store_payments_from_html(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    html: str,
) -> dict[str, Any]:
    """Fallback: extract var transactions from HTML, then store as JSON-first."""
    raw_list = extract_payments_json_from_html(html)
    txns, totals = parse_patient_payments_html(html)
    if raw_list is None:
        # Rebuild dicts from parsed txns
        raw_list = [
            {
                "dateOfService": t.date_of_service,
                "dateOfTransaction": t.date_of_transaction,
                "type": t.payment_type,
                "description": t.description,
                "amountDue": t.amount_due,
                "amountPaid": t.amount_paid,
                "paidMethodType": t.paid_method,
                "creditType": t.credit_type,
                "checkAuthorizationNumber": t.auth_check,
                "transactionId": t.transaction_id,
                "status": t.status,
                "caseId": t.case_id or case_id,
                "facilityId": t.facility_id or facility_id,
                "patientId": t.patient_id,
                "paymentDate": t.payment_date,
            }
            for t in txns
        ]
        # Keep HTML only as probe_extra fallback
        probe = case_root(base_dir, facility_id, case_id) / "raw" / "probe_extra"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "payments.html").write_text(html or "", encoding="utf-8", errors="replace")
    return store_payments_from_json(
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        transactions=raw_list,
        totals=totals,
    )


async def fetch_and_store_payments(
    context: Any,
    *,
    base_dir: Path,
    facility_id: str | int,
    case_id: int,
    patient_id: int,
) -> dict[str, Any]:
    """Live deferred fetch — JSON from page; HTML only as extraction vehicle."""
    url = patient_payments_url(patient_id, case_id)
    resp = await context.request.get(
        url,
        headers={
            "Referer": f"{BASE_URL}/patientChart.php?ID={patient_id}&CaseID={case_id}"
        },
        timeout=90_000,
    )
    html = await resp.text()
    return store_payments_from_html(
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        html=html,
    )
