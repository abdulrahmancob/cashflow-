"""DB-backed reconcile: read facts via repository, write operational spine."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from cashflow_db.repository import connection
from cashflow_db.repository import insurance as ins_repo
from cashflow_db.repository import payments as pay_repo
from cashflow_db.repository import reconciliation as recon_repo
from cashflow_db.repository import visits as visit_repo

from .insurance_map import load_insurance_rules
from .load_webpt import WebptLine
from .matcher import MatchedLine, match_lines
from .normalize import format_date, format_money, name_key_from_revflow, name_key_from_webpt, parse_date
from .parse_revflow_eob import PaymentLine



def _line_rows(matched_lines: list[MatchedLine]) -> list[dict]:
    rows: list[dict] = []
    for item in matched_lines:
        webpt = item.webpt
        payment = item.payment
        rows.append(
            {
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
                "facility_id": webpt.facility_id,
                "facility_name": webpt.facility_name,
                "case_id": webpt.case_id,
                "ins_name": webpt.ins_name,
                "insurance_note": webpt.insurance_note,
                "insurance_revflow": payment.payor if payment else "",
                "date_of_service": webpt.date_of_service,
                "cpt_code": webpt.cpt_code,
                "modifier": webpt.modifier,
                "status": item.status,
                "paid_amount": format_money(payment.paid_amount if payment else 0.0),
                "allowed_amount": format_money(payment.allowed_amount if payment else 0.0),
                "adjustment_amount": format_money(
                    payment.adjustment_amount if payment else 0.0
                ),
                "deductible_amount": format_money(
                    payment.deductible_amount if payment else 0.0
                ),
                "eob_date": payment.eob_date if payment else "",
                "check_eft_num": payment.check_eft_num if payment else "",
                "carcs": payment.carcs if payment else "",
                "expected_copay": webpt.expected_copay,
                "expected_deductible": webpt.expected_deductible,
                "match_level": item.match_level,
                "confidence": f"{item.confidence:.2f}",
                "insurance_mismatch": item.insurance_mismatch,
                "daily_note_id": webpt.daily_note_id,
                "note_file": webpt.note_file,
            }
        )
    return rows

log = logging.getLogger(__name__)


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def load_webpt_lines_from_db(
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    database_url: str | None = None,
) -> list[WebptLine]:
    with connection(database_url) as conn:
        rows = visit_repo.get_service_lines_for_reconcile(
            conn, service_from=service_from, service_to=service_to
        )
    lines: list[WebptLine] = []
    for r in rows:
        pid = _as_str(r.get("webpt_patient_id"))
        pname = _as_str(r.get("patient_name"))
        dos = format_date(r.get("date_of_service")) or _as_str(r.get("date_of_service"))
        lines.append(
            WebptLine(
                patient_id=pid,
                daily_note_id=_as_str(r.get("daily_note_id")),
                patient_name=pname,
                name_key=name_key_from_webpt(pname),
                date_of_service=dos,
                cpt_code=_as_str(r.get("cpt_code")),
                modifier=_as_str(r.get("modifier")),
                units=_as_str(r.get("units")),
                description="",
                insurance_note=_as_str(r.get("insurance_note")),
                dob=format_date(r.get("dob")) or _as_str(r.get("dob")),
                case_id=_as_str(r.get("case_id")),
                facility_id=_as_str(r.get("facility_id")),
                facility_name=_as_str(r.get("facility_name")),
                ins_name=_as_str(r.get("ins_name")),
                expected_copay=_as_str(r.get("expected_copay")),
                expected_deductible=_as_str(r.get("expected_deductible")),
                note_file=_as_str(r.get("note_file")),
            )
        )
    return lines


def load_payments_from_db(
    *,
    database_url: str | None = None,
) -> tuple[list[PaymentLine], dict[str, date | None]]:
    with connection(database_url) as conn:
        rows = pay_repo.get_eob_payments_unified(conn)
        tracked = pay_repo.get_tracked_eft_refs(conn)
    payments: list[PaymentLine] = []
    for r in rows:
        pname = _as_str(r.get("first_name"))
        # patient_name may be "Last, First"
        first, last = pname, ""
        if "," in pname:
            last, first = [p.strip() for p in pname.split(",", 1)]
        payments.append(
            PaymentLine(
                revflow_patient_id=_as_str(r.get("revflow_patient_id")),
                first_name=first,
                last_name=last,
                name_key=_as_str(r.get("name_key"))
                or name_key_from_revflow(last, first),
                date_of_service=format_date(r.get("date_of_service"))
                or _as_str(r.get("date_of_service")),
                cpt_code=_as_str(r.get("cpt_code")),
                modifier=_as_str(r.get("modifier")),
                units=int(r.get("units") or 0),
                billed_amount=float(r.get("billed_amount") or 0),
                allowed_amount=float(r.get("allowed_amount") or 0),
                paid_amount=float(r.get("paid_amount") or 0),
                adjustment_amount=float(r.get("adjustment_amount") or 0),
                deductible_amount=float(r.get("deductible_amount") or 0),
                carcs=_as_str(r.get("carcs")),
                payor=_as_str(r.get("payor")),
                check_eft_num=_as_str(r.get("check_eft_num")),
                eob_date=format_date(r.get("eob_date")) or _as_str(r.get("eob_date")),
                report_from="",
                report_to="",
                source_file=_as_str(r.get("source_file")),
                eob_key=_as_str(r.get("eob_key")),
                company_id=_as_str(r.get("company_id")),
            )
        )
    return payments, tracked


def _partition_by_tracker(
    payments: list[PaymentLine],
    tracked_refs: dict[str, date | None],
) -> tuple[list[PaymentLine], list[PaymentLine]]:
    if not tracked_refs:
        return payments, []
    in_tracker: list[PaymentLine] = []
    out: list[PaymentLine] = []
    for p in payments:
        ref = (p.check_eft_num or "").strip()
        if ref and ref in tracked_refs:
            in_tracker.append(p)
        else:
            # also try last4
            if ref and len(ref) >= 4 and ref[-4:] in tracked_refs:
                in_tracker.append(p)
            else:
                out.append(p)
    return in_tracker, out


def run_reconciliation_from_db(
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    insurance_map: Path | None = None,
    emit_csv: bool = False,
    output_dir: Path | None = None,
    database_url: str | None = None,
    rules_version: str | None = None,
) -> dict[str, Any]:
    rules = load_insurance_rules(insurance_map)
    webpt_lines = load_webpt_lines_from_db(
        service_from=service_from,
        service_to=service_to,
        database_url=database_url,
    )
    all_payments, tracked = load_payments_from_db(database_url=database_url)
    payments, not_tracked = _partition_by_tracker(all_payments, tracked)
    log.info(
        "DB reconcile: %s webpt lines, %s payments in tracker, %s excluded",
        len(webpt_lines),
        len(payments),
        len(not_tracked),
    )
    result = match_lines(webpt_lines, payments, rules)
    line_rows = _line_rows(result.lines)
    # normalize money/confidence for DB numerics
    for row in line_rows:
        for key in (
            "paid_amount",
            "allowed_amount",
            "adjustment_amount",
            "deductible_amount",
            "expected_copay",
            "expected_deductible",
            "confidence",
        ):
            raw = row.get(key)
            if raw in ("", None):
                row[key] = None
            else:
                try:
                    row[key] = float(str(raw).replace(",", ""))
                except ValueError:
                    row[key] = None
        if row.get("date_of_service"):
            row["date_of_service"] = parse_date(row["date_of_service"]) or row["date_of_service"]
        if row.get("eob_date"):
            row["eob_date"] = parse_date(row["eob_date"]) or row["eob_date"]
        if row.get("dob"):
            row["dob"] = parse_date(row["dob"]) or row["dob"]
        mm = str(row.get("insurance_mismatch") or "").lower()
        row["insurance_mismatch"] = mm in {"1", "true", "yes", "y"}

    from .reconcile import aggregate_patients, aggregate_visits, _write_csv

    deposit_dates = {k: v for k, v in tracked.items() if v is not None}
    visit_rows = aggregate_visits(
        result.lines,
        result.orphan_payments,
        deposit_dates=deposit_dates or None,
    )
    for v in visit_rows:
        for key in (
            "total_paid",
            "matched_paid",
            "bonus_paid",
            "unmatched_paid",
            "visit_paid_total",
            "primary_check_amount",
            "secondary_check_amount",
        ):
            if key in v and v[key] not in ("", None):
                try:
                    v[key] = float(str(v[key]).replace(",", ""))
                except ValueError:
                    pass
        if v.get("date_of_service"):
            v["date_of_service"] = parse_date(v["date_of_service"]) or v["date_of_service"]

    with connection(database_url) as conn:
        etl_ids = recon_repo.latest_etl_run_ids(
            conn, ["webpt", "revflow", "tracker", "schedule"]
        )
        run_id = recon_repo.create_reconciliation_run(
            conn,
            source_etl_run_ids=etl_ids,
            rules_version=rules_version or "insurance_map",
            status="running",
        )
        try:
            n_lines = recon_repo.replace_reconciliation_lines(conn, run_id, line_rows)
            n_visits = recon_repo.replace_visit_aggs(conn, run_id, visit_rows)
            recon_repo.finish_reconciliation_run(
                conn, run_id, status="success", row_count=n_lines
            )
        except Exception as exc:
            recon_repo.finish_reconciliation_run(
                conn, run_id, status="failed", notes=str(exc)[:2000]
            )
            raise

    if emit_csv and output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(output_dir / "reconciliation_lines.csv", _line_rows(result.lines), list(line_rows[0].keys()) if line_rows else [])
        _write_csv(
            output_dir / "reconciliation_visits.csv",
            visit_rows,
            list(visit_rows[0].keys()) if visit_rows else [],
        )
        _write_csv(
            output_dir / "reconciliation_patients.csv",
            aggregate_patients(result.lines),
            [],
        )

    matched_count = sum(1 for item in result.lines if item.payment is not None)
    return {
        "reconciliation_run_id": run_id,
        "webpt_lines": len(webpt_lines),
        "payment_lines": len(payments),
        "matched_lines": matched_count,
        "pending_lines": sum(1 for item in result.lines if item.status == "pending"),
        "orphan_payments": len(result.orphan_payments),
        "lines_written": len(line_rows),
        "visits_written": n_visits,
    }


def persist_insurance_behavior_from_frames(
    *,
    reconciliation_run_id: str | None,
    summary_rows: list[dict[str, Any]],
    timeline_rows: list[dict[str, Any]],
    database_url: str | None = None,
) -> dict[str, int]:
    with connection(database_url) as conn:
        n_sum = ins_repo.replace_payor_behavior_summary(
            conn, reconciliation_run_id=reconciliation_run_id, rows=summary_rows
        )
        n_tl = ins_repo.replace_checks_timeline(
            conn, reconciliation_run_id=reconciliation_run_id, rows=timeline_rows
        )
    return {"payor_behavior_summary": n_sum, "checks_timeline": n_tl}
