"""CLI for WebPT ↔ RevFlow payment reconciliation."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

from .insurance_map import (
    collect_revflow_payor_counts,
    collect_webpt_insurance_counts,
    load_insurance_rules,
    suggest_mappings,
    write_insurance_mapping_report,
)
from .load_webpt import load_webpt_lines
from .matcher import aggregate_patients, aggregate_visits, match_lines
from .normalize import format_money, parse_date
from .parse_revflow_eob import load_all_payments

log = logging.getLogger("cashflow_reconcile")


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _payment_rows(payments) -> list[dict]:
    rows: list[dict] = []
    for payment in payments:
        rows.append(
            {
                "revflow_patient_id": payment.revflow_patient_id,
                "first_name": payment.first_name,
                "last_name": payment.last_name,
                "name_key": payment.name_key,
                "date_of_service": payment.date_of_service,
                "cpt_code": payment.cpt_code,
                "modifier": payment.modifier,
                "units": payment.units,
                "billed_amount": format_money(payment.billed_amount),
                "allowed_amount": format_money(payment.allowed_amount),
                "paid_amount": format_money(payment.paid_amount),
                "adjustment_amount": format_money(payment.adjustment_amount),
                "deductible_amount": format_money(payment.deductible_amount),
                "carcs": payment.carcs,
                "payor": payment.payor,
                "check_eft_num": payment.check_eft_num,
                "eob_date": payment.eob_date,
                "source_file": payment.source_file,
                "eob_key": payment.eob_key,
                "company_id": payment.company_id,
            }
        )
    return rows


def _line_rows(matched_lines) -> list[dict]:
    rows: list[dict] = []
    for item in matched_lines:
        webpt = item.webpt
        payment = item.payment
        rows.append(
            {
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
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
                "adjustment_amount": format_money(payment.adjustment_amount if payment else 0.0),
                "deductible_amount": format_money(payment.deductible_amount if payment else 0.0),
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


def _unmatched_webpt_rows(matched_lines) -> list[dict]:
    rows: list[dict] = []
    for item in matched_lines:
        if item.status != "pending" and item.insurance_mismatch != "yes":
            continue
        webpt = item.webpt
        reason = item.unmatched_reason or (
            "insurance_mismatch" if item.insurance_mismatch == "yes" else "no_payment_in_window"
        )
        rows.append(
            {
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
                "ins_name": webpt.ins_name,
                "insurance_note": webpt.insurance_note,
                "date_of_service": webpt.date_of_service,
                "cpt_code": webpt.cpt_code,
                "modifier": webpt.modifier,
                "reason": reason,
            }
        )
    return rows


def _unmatched_payment_rows(orphan_payments) -> list[dict]:
    rows: list[dict] = []
    for payment in orphan_payments:
        rows.append(
            {
                "revflow_patient_id": payment.revflow_patient_id,
                "first_name": payment.first_name,
                "last_name": payment.last_name,
                "date_of_service": payment.date_of_service,
                "cpt_code": payment.cpt_code,
                "modifier": payment.modifier,
                "paid_amount": format_money(payment.paid_amount),
                "payor": payment.payor,
                "check_eft_num": payment.check_eft_num,
                "eob_date": payment.eob_date,
                "source_file": payment.source_file,
                "reason": "no_matching_webpt_documentation",
            }
        )
    return rows


def run_reconciliation(
    *,
    webpt_dir: Path,
    patients_export: Path | None,
    revflow_dir: Path,
    manifest: Path | None,
    output_dir: Path,
    insurance_map: Path | None,
    service_from: date | None,
    service_to: date | None,
) -> dict[str, int]:
    rules = load_insurance_rules(insurance_map)
    webpt_lines = load_webpt_lines(
        webpt_dir,
        patients_export_path=patients_export,
        service_from=service_from,
        service_to=service_to,
    )
    payments = load_all_payments(revflow_dir, manifest_path=manifest)
    result = match_lines(webpt_lines, payments, rules)

    payment_fieldnames = [
        "revflow_patient_id",
        "first_name",
        "last_name",
        "name_key",
        "date_of_service",
        "cpt_code",
        "modifier",
        "units",
        "billed_amount",
        "allowed_amount",
        "paid_amount",
        "adjustment_amount",
        "deductible_amount",
        "carcs",
        "payor",
        "check_eft_num",
        "eob_date",
        "source_file",
        "eob_key",
        "company_id",
    ]
    line_fieldnames = [
        "webpt_patient_id",
        "patient_name",
        "dob",
        "facility_name",
        "case_id",
        "ins_name",
        "insurance_note",
        "insurance_revflow",
        "date_of_service",
        "cpt_code",
        "modifier",
        "status",
        "paid_amount",
        "allowed_amount",
        "adjustment_amount",
        "deductible_amount",
        "eob_date",
        "check_eft_num",
        "carcs",
        "expected_copay",
        "expected_deductible",
        "match_level",
        "confidence",
        "insurance_mismatch",
        "daily_note_id",
        "note_file",
    ]
    visit_fieldnames = [
        "webpt_patient_id",
        "patient_name",
        "dob",
        "facility_name",
        "date_of_service",
        "total_billed_cpts",
        "total_paid",
        "paid_lines",
        "pending_lines",
        "visit_status",
    ]
    patient_fieldnames = [
        "webpt_patient_id",
        "patient_name",
        "dob",
        "facility_name",
        "case_id",
        "ins_name",
        "assigned_therapist",
        "auth_ins_visits",
        "visits_total",
        "visits_paid",
        "visits_pending",
        "total_paid",
        "primary_payor",
    ]
    unmatched_webpt_fieldnames = [
        "webpt_patient_id",
        "patient_name",
        "dob",
        "ins_name",
        "insurance_note",
        "date_of_service",
        "cpt_code",
        "modifier",
        "reason",
    ]
    unmatched_payment_fieldnames = [
        "revflow_patient_id",
        "first_name",
        "last_name",
        "date_of_service",
        "cpt_code",
        "modifier",
        "paid_amount",
        "payor",
        "check_eft_num",
        "eob_date",
        "source_file",
        "reason",
    ]

    _write_csv(output_dir / "payments_unified.csv", _payment_rows(payments), payment_fieldnames)
    _write_csv(
        output_dir / "reconciliation_lines.csv",
        _line_rows(result.lines),
        line_fieldnames,
    )
    _write_csv(
        output_dir / "reconciliation_visits.csv",
        aggregate_visits(result.lines),
        visit_fieldnames,
    )
    _write_csv(
        output_dir / "reconciliation_patients.csv",
        aggregate_patients(result.lines),
        patient_fieldnames,
    )
    _write_csv(
        output_dir / "unmatched_webpt.csv",
        _unmatched_webpt_rows(result.lines),
        unmatched_webpt_fieldnames,
    )
    _write_csv(
        output_dir / "unmatched_payments.csv",
        _unmatched_payment_rows(result.orphan_payments),
        unmatched_payment_fieldnames,
    )

    mapping_rows = suggest_mappings(
        collect_webpt_insurance_counts(webpt_lines),
        collect_revflow_payor_counts(payments),
        rules,
    )
    write_insurance_mapping_report(
        mapping_rows,
        output_dir / "insurance_mapping_report.csv",
    )

    matched_count = sum(1 for item in result.lines if item.payment is not None)
    pending_count = sum(1 for item in result.lines if item.status == "pending")
    return {
        "webpt_lines": len(webpt_lines),
        "payment_lines": len(payments),
        "matched_lines": matched_count,
        "pending_lines": pending_count,
        "orphan_payments": len(result.orphan_payments),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile WebPT extracted billing lines with RevFlow EOB payments"
    )
    parser.add_argument(
        "--webpt-dir",
        type=Path,
        required=True,
        help="Directory containing extracted/cpt_codes.csv and daily_notes.csv",
    )
    parser.add_argument(
        "--patients-export",
        type=Path,
        default=None,
        help="Path to patients_export_61d.csv (defaults to sibling of --webpt-dir)",
    )
    parser.add_argument(
        "--revflow-dir",
        type=Path,
        required=True,
        help="Directory containing RevFlow EOB CSV exports",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional manifest.json from revflow_scraper export run",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for reconciliation CSV outputs",
    )
    parser.add_argument(
        "--insurance-map",
        type=Path,
        default=None,
        help="Optional insurance_map.yaml override",
    )
    parser.add_argument(
        "--service-from",
        type=str,
        default="2026-06-01",
        help="Include WebPT service dates on/after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--service-to",
        type=str,
        default="2026-07-02",
        help="Include WebPT service dates on/before this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    patients_export = args.patients_export
    if patients_export is None:
        parent = args.webpt_dir.parent
        candidates = sorted(parent.glob("patients_export*.csv"))
        patients_export = candidates[0] if candidates else None

    service_from = parse_date(args.service_from)
    service_to = parse_date(args.service_to)
    if service_from is None or service_to is None:
        parser.error("Invalid --service-from/--service-to date (use YYYY-MM-DD)")

    summary = run_reconciliation(
        webpt_dir=args.webpt_dir,
        patients_export=patients_export,
        revflow_dir=args.revflow_dir,
        manifest=args.manifest,
        output_dir=args.output_dir,
        insurance_map=args.insurance_map,
        service_from=service_from,
        service_to=service_to,
    )

    log.info("Reconciliation complete:")
    for key, value in summary.items():
        log.info("  %s: %s", key, value)
    log.info("Outputs written to %s", args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
