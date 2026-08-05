"""Compare Snowflake ALL_BILLING_DATA (reference) to reconciliation_visits.csv."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

# Allow running as python -m snowflake_pull.compare_visits from repo root
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import (  # noqa: E402
    format_money,
    name_key_from_webpt,
    normalize_name_key,
    parse_date,
    parse_money,
)

AMOUNT_TOLERANCE = 0.01


@dataclass
class VisitRow:
    name_key: str
    date_of_service: str
    patient_name: str
    facility: str
    status: str
    total_paid: float
    source: str  # snowflake | ours
    raw: dict[str, str] = field(default_factory=dict)


def name_key_from_snowflake_patient(patient: str) -> str:
    """SF PATIENT is typically 'First Last' (optional middle)."""
    parts = (patient or "").strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return normalize_name_key(parts[0], "")
    first, last = parts[0], parts[-1]
    return normalize_name_key(last, first)


def normalize_status(value: str, *, source: str) -> str:
    text = (value or "").strip().lower()
    if not text or text in {"blank", ""}:
        return "pending"
    if text in {"paid"}:
        return "paid"
    if text in {"partial"}:
        return "partial"
    if text in {"pending"}:
        return "pending"
    if text in {"denied"}:
        return "denied"
    if text in {"deduct"}:
        return "deduct"
    if text in {"collection"}:
        return "collection"
    # SF sometimes title-cases; already lowercased
    if source == "snowflake" and text == "blank":
        return "pending"
    return text


def _in_range(dos: str, start: date | None, end: date | None) -> bool:
    d = parse_date(dos)
    if d is None:
        return False
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _status_rank(status: str) -> int:
    order = {
        "paid": 50,
        "partial": 40,
        "deduct": 30,
        "collection": 25,
        "denied": 20,
        "pending": 10,
    }
    return order.get(status, 0)


_SF_MONEY_DETAIL_FIELDS = (
    "CLIENT_PAYMENT",
    "INSURANCE_PAYMENT",
    "UPDATED_PAYMENT",
    "CO_INSURANCE_PAYMENT",
    "REDUCTIONS",
)

_SF_TEXT_DETAIL_FIELDS = (
    "STATUS",
    "DETAILS",
    "PRIMARY_CHECK_NUMBER",
    "PRIMARY_CHECK_DATE",
    "SECONDARY_CHECK_NUMBER",
    "SECONDARY_CHECK_DATE",
)

STATUS_MISMATCH_SF_DETAIL_FIELDS = (
    "CLIENT_PAYMENT",
    "INSURANCE_PAYMENT",
    "STATUS",
    "UPDATED_PAYMENT",
    "CO_INSURANCE_PAYMENT",
    "REDUCTIONS",
    "DETAILS",
    "PRIMARY_CHECK_NUMBER",
    "PRIMARY_CHECK_DATE",
    "PRIMARY_CHECK_AMOUNT",
    "SECONDARY_CHECK_NUMBER",
    "SECONDARY_CHECK_DATE",
    "SECONDARY_CHECK_AMOUNT",
)


def _join_unique(values: Iterable[str]) -> str:
    seen: list[str] = []
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return ";".join(seen)


def load_snowflake(
    path: Path,
    *,
    start: date | None,
    end: date | None,
) -> dict[tuple[str, str], VisitRow]:
    """Aggregate SF rows by (name_key, DOS); sum payments; best status."""
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("DATE_OF_SERVICE") or "").strip()
            if not _in_range(dos, start, end):
                continue
            key = name_key_from_snowflake_patient(row.get("PATIENT") or "")
            if not key:
                continue
            buckets[(key, dos)].append(row)

    out: dict[tuple[str, str], VisitRow] = {}
    for (key, dos), rows in buckets.items():
        paid = 0.0
        best_status = "pending"
        clinics: list[str] = []
        patients: list[str] = []
        money_sums = {col: 0.0 for col in _SF_MONEY_DETAIL_FIELDS}
        for row in rows:
            paid += (
                parse_money(row.get("INSURANCE_PAYMENT"))
                + parse_money(row.get("CO_INSURANCE_PAYMENT"))
                + parse_money(row.get("CLIENT_PAYMENT"))
            )
            for col in _SF_MONEY_DETAIL_FIELDS:
                money_sums[col] += parse_money(row.get(col))
            st = normalize_status(row.get("STATUS") or "", source="snowflake")
            if _status_rank(st) > _status_rank(best_status):
                best_status = st
            clinic = (row.get("CLINIC") or "").strip()
            if clinic and clinic not in clinics:
                clinics.append(clinic)
            patient = (row.get("PATIENT") or "").strip()
            if patient and patient not in patients:
                patients.append(patient)

        winning_rows = [
            row
            for row in rows
            if normalize_status(row.get("STATUS") or "", source="snowflake") == best_status
        ]
        text_detail = {
            col: _join_unique(r.get(col) or "" for r in winning_rows)
            for col in _SF_TEXT_DETAIL_FIELDS
        }
        # PRIMARY/SECONDARY_CHECK_AMOUNT: join from winning rows (not summed with totals)
        check_amount_detail = {
            col: _join_unique(
                format_money(parse_money(r.get(col))) if (r.get(col) or "").strip() else ""
                for r in winning_rows
            )
            for col in ("PRIMARY_CHECK_AMOUNT", "SECONDARY_CHECK_AMOUNT")
        }

        raw: dict[str, str] = {
            "sf_row_count": str(len(rows)),
            "sf_ids": ";".join((r.get("ID") or "") for r in rows),
            "sf_status_raw": ";".join(
                sorted({(r.get("STATUS") or "").strip() for r in rows})
            ),
            "sf_insurance": ";".join(
                sorted(
                    {
                        (r.get("INSURANCE") or "").strip()
                        for r in rows
                        if (r.get("INSURANCE") or "").strip()
                    }
                )
            ),
            "CLIENT_PAYMENT": format_money(money_sums["CLIENT_PAYMENT"]),
            "INSURANCE_PAYMENT": format_money(money_sums["INSURANCE_PAYMENT"]),
            "UPDATED_PAYMENT": format_money(money_sums["UPDATED_PAYMENT"]),
            "CO_INSURANCE_PAYMENT": format_money(money_sums["CO_INSURANCE_PAYMENT"]),
            "REDUCTIONS": format_money(money_sums["REDUCTIONS"]),
            **text_detail,
            **check_amount_detail,
        }
        out[(key, dos)] = VisitRow(
            name_key=key,
            date_of_service=dos,
            patient_name="; ".join(patients),
            facility="; ".join(clinics),
            status=best_status,
            total_paid=paid,
            source="snowflake",
            raw=raw,
        )
    return out


def load_visits(
    path: Path,
    *,
    start: date | None,
    end: date | None,
) -> dict[tuple[str, str], VisitRow]:
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("date_of_service") or "").strip()
            if not _in_range(dos, start, end):
                continue
            key = name_key_from_webpt(row.get("patient_name") or "")
            if not key:
                continue
            buckets[(key, dos)].append(row)

    out: dict[tuple[str, str], VisitRow] = {}
    for (key, dos), rows in buckets.items():
        paid = 0.0
        for r in rows:
            if (r.get("visit_paid_total") or "").strip() != "":
                paid += parse_money(r.get("visit_paid_total"))
            else:
                paid += parse_money(r.get("total_paid"))
        best_status = "pending"
        facilities: list[str] = []
        patients: list[str] = []
        for row in rows:
            st = normalize_status(row.get("visit_status") or "", source="ours")
            if _status_rank(st) > _status_rank(best_status):
                best_status = st
            fac = (row.get("facility_name") or "").strip()
            if fac and fac not in facilities:
                facilities.append(fac)
            name = (row.get("patient_name") or "").strip()
            if name and name not in patients:
                patients.append(name)
        out[(key, dos)] = VisitRow(
            name_key=key,
            date_of_service=dos,
            patient_name="; ".join(patients),
            facility="; ".join(facilities),
            status=best_status,
            total_paid=paid,
            source="ours",
            raw={
                "ours_row_count": str(len(rows)),
                "webpt_patient_id": ";".join(
                    (r.get("webpt_patient_id") or "") for r in rows
                ),
                "ours_amount_column": (
                    "visit_paid_total"
                    if any((r.get("visit_paid_total") or "").strip() for r in rows)
                    else "total_paid"
                ),
            },
        )
    return out


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: Sequence[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def load_snowflake_by_emr(
    path: Path,
    *,
    start: date | None,
    end: date | None,
) -> dict[tuple[str, str], VisitRow]:
    """Aggregate SF by (EMR_ID, DOS). Rows without EMR_ID are omitted."""
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("DATE_OF_SERVICE") or "").strip()
            if not _in_range(dos, start, end):
                continue
            emr = (row.get("EMR_ID") or "").strip()
            if not emr:
                continue
            buckets[(emr, dos)].append(row)

    # Reuse money/status aggregation via temporary name_key load pattern
    out: dict[tuple[str, str], VisitRow] = {}
    for (emr, dos), rows in buckets.items():
        # Delegate to same aggregation as load_snowflake by synthesizing PATIENT key path
        paid = 0.0
        best_status = "pending"
        clinics: list[str] = []
        patients: list[str] = []
        for row in rows:
            paid += (
                parse_money(row.get("INSURANCE_PAYMENT"))
                + parse_money(row.get("CO_INSURANCE_PAYMENT"))
                + parse_money(row.get("CLIENT_PAYMENT"))
            )
            st = normalize_status(row.get("STATUS") or "", source="snowflake")
            if _status_rank(st) > _status_rank(best_status):
                best_status = st
            clinic = (row.get("CLINIC") or "").strip()
            if clinic and clinic not in clinics:
                clinics.append(clinic)
            patient = (row.get("PATIENT") or "").strip()
            if patient and patient not in patients:
                patients.append(patient)
        out[(emr, dos)] = VisitRow(
            name_key=emr,
            date_of_service=dos,
            patient_name="; ".join(patients),
            facility="; ".join(clinics),
            status=best_status,
            total_paid=paid,
            source="snowflake",
            raw={
                "sf_row_count": str(len(rows)),
                "sf_ids": ";".join((r.get("ID") or "") for r in rows),
                "emr_id": emr,
                "join_key": "emr_id",
            },
        )
    return out


def load_visits_by_emr(
    path: Path,
    *,
    start: date | None,
    end: date | None,
) -> dict[tuple[str, str], VisitRow]:
    """Aggregate REC by (webpt_patient_id, DOS)."""
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("date_of_service") or "").strip()
            if not _in_range(dos, start, end):
                continue
            emr = (row.get("webpt_patient_id") or "").strip()
            if not emr:
                continue
            buckets[(emr, dos)].append(row)

    out: dict[tuple[str, str], VisitRow] = {}
    for (emr, dos), rows in buckets.items():
        paid = 0.0
        for r in rows:
            if (r.get("visit_paid_total") or "").strip() != "":
                paid += parse_money(r.get("visit_paid_total"))
            else:
                paid += parse_money(r.get("total_paid"))
        best_status = "pending"
        facilities: list[str] = []
        patients: list[str] = []
        for row in rows:
            st = normalize_status(row.get("visit_status") or "", source="ours")
            if _status_rank(st) > _status_rank(best_status):
                best_status = st
            fac = (row.get("facility_name") or "").strip()
            if fac and fac not in facilities:
                facilities.append(fac)
            name = (row.get("patient_name") or "").strip()
            if name and name not in patients:
                patients.append(name)
        out[(emr, dos)] = VisitRow(
            name_key=emr,
            date_of_service=dos,
            patient_name="; ".join(patients),
            facility="; ".join(facilities),
            status=best_status,
            total_paid=paid,
            source="ours",
            raw={
                "ours_row_count": str(len(rows)),
                "webpt_patient_id": emr,
                "join_key": "emr_id",
            },
        )
    return out


def coverage_summary_dual(
    *,
    name_key_results: dict[str, list[dict[str, Any]]],
    emr_results: dict[str, list[dict[str, Any]]],
    fallback_namekey_count: int = 0,
) -> dict[str, Any]:
    def meta(results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        m = results.get("_meta", [{}])[0]
        return {
            "sf_keys": m.get("sf_keys", 0),
            "ours_keys": m.get("ours_keys", 0),
            "matched": m.get("matched", 0),
            "missing_in_ours": m.get("missing_in_ours", 0),
            "extra_in_ours": m.get("extra_in_ours", 0),
            "status_mismatch": m.get("status_mismatch", 0),
            "amount_mismatch": m.get("amount_mismatch", 0),
        }

    return {
        "name_key": meta(name_key_results),
        "emr_id": meta(emr_results),
        "measurement_correction": {
            "label": "emr_key_vs_name_key",
            "missing_delta": meta(name_key_results)["missing_in_ours"]
            - meta(emr_results)["missing_in_ours"],
            "fallback_namekey_count": fallback_namekey_count,
            "kpi_credit": "none_measurement_only",
        },
    }


def compare(
    sf: dict[tuple[str, str], VisitRow],
    ours: dict[tuple[str, str], VisitRow],
) -> dict[str, list[dict[str, Any]]]:
    sf_keys = set(sf)
    ours_keys = set(ours)
    missing = sorted(sf_keys - ours_keys)
    extra = sorted(ours_keys - sf_keys)
    matched = sorted(sf_keys & ours_keys)

    missing_rows: list[dict[str, Any]] = []
    for k in missing:
        row = sf[k]
        missing_rows.append(
            {
                "name_key": row.name_key,
                "date_of_service": row.date_of_service,
                "sf_patient": row.patient_name,
                "sf_clinic": row.facility,
                "sf_status": row.status,
                "sf_total_paid": format_money(row.total_paid),
                "sf_row_count": row.raw.get("sf_row_count", ""),
                "sf_ids": row.raw.get("sf_ids", ""),
                "sf_insurance": row.raw.get("sf_insurance", ""),
                "reason": "in_snowflake_not_in_reconciliation_visits",
            }
        )

    extra_rows: list[dict[str, Any]] = []
    for k in extra:
        row = ours[k]
        extra_rows.append(
            {
                "name_key": row.name_key,
                "date_of_service": row.date_of_service,
                "ours_patient": row.patient_name,
                "ours_facility": row.facility,
                "ours_status": row.status,
                "ours_total_paid": format_money(row.total_paid),
                "webpt_patient_id": row.raw.get("webpt_patient_id", ""),
                "reason": "in_reconciliation_visits_not_in_snowflake",
            }
        )

    status_rows: list[dict[str, Any]] = []
    amount_rows: list[dict[str, Any]] = []
    for k in matched:
        a, b = sf[k], ours[k]
        if a.status != b.status:
            status_row: dict[str, Any] = {
                "name_key": a.name_key,
                "date_of_service": a.date_of_service,
                "sf_patient": a.patient_name,
                "ours_patient": b.patient_name,
                "sf_clinic": a.facility,
                "ours_facility": b.facility,
                "sf_status": a.status,
                "ours_status": b.status,
                "sf_total_paid": format_money(a.total_paid),
                "ours_total_paid": format_money(b.total_paid),
                "webpt_patient_id": b.raw.get("webpt_patient_id", ""),
            }
            for col in STATUS_MISMATCH_SF_DETAIL_FIELDS:
                status_row[col] = a.raw.get(col, "") if a.status == "paid" else ""
            status_rows.append(status_row)
        delta = abs(a.total_paid - b.total_paid)
        if delta > AMOUNT_TOLERANCE:
            amount_rows.append(
                {
                    "name_key": a.name_key,
                    "date_of_service": a.date_of_service,
                    "sf_patient": a.patient_name,
                    "ours_patient": b.patient_name,
                    "sf_clinic": a.facility,
                    "ours_facility": b.facility,
                    "sf_status": a.status,
                    "ours_status": b.status,
                    "sf_total_paid": format_money(a.total_paid),
                    "ours_total_paid": format_money(b.total_paid),
                    "delta": format_money(a.total_paid - b.total_paid),
                }
            )

    return {
        "missing_in_ours": missing_rows,
        "extra_in_ours": extra_rows,
        "status_mismatch": status_rows,
        "amount_mismatch": amount_rows,
        "_meta": [
            {
                "sf_keys": len(sf),
                "ours_keys": len(ours),
                "matched": len(matched),
                "missing_in_ours": len(missing_rows),
                "extra_in_ours": len(extra_rows),
                "status_mismatch": len(status_rows),
                "amount_mismatch": len(amount_rows),
            }
        ],
    }


AMOUNT_XLSX_FIELDS = [
    "patient_ours",
    "patient_sf",
    "date_of_service",
    "facility_ours",
    "clinic_sf",
    "ours_amount",
    "ours_source_file",
    "ours_source_column",
    "sf_amount",
    "sf_source_file",
    "sf_source_column",
    "delta",
    "webpt_patient_id",
    "sf_ids",
]


def build_amount_diff_rows(
    sf: dict[tuple[str, str], VisitRow],
    ours: dict[tuple[str, str], VisitRow],
    *,
    visits_path: Path,
    snowflake_path: Path,
) -> list[dict[str, Any]]:
    """Matched visits where |sf − ours| > tolerance, with source provenance."""
    ours_src = str(visits_path).replace("\\", "/")
    sf_src = str(snowflake_path).replace("\\", "/")
    rows: list[dict[str, Any]] = []
    for key in sorted(set(sf) & set(ours)):
        a, b = sf[key], ours[key]
        delta = a.total_paid - b.total_paid
        if abs(delta) <= AMOUNT_TOLERANCE:
            continue
        rows.append(
            {
                "patient_ours": b.patient_name,
                "patient_sf": a.patient_name,
                "date_of_service": a.date_of_service,
                "facility_ours": b.facility,
                "clinic_sf": a.facility,
                "ours_amount": round(b.total_paid, 2),
                "ours_source_file": ours_src,
                "ours_source_column": b.raw.get("ours_amount_column", "visit_paid_total"),
                "sf_amount": round(a.total_paid, 2),
                "sf_source_file": sf_src,
                "sf_source_column": "INSURANCE_PAYMENT+CO_INSURANCE_PAYMENT+CLIENT_PAYMENT",
                "delta": round(delta, 2),
                "webpt_patient_id": b.raw.get("webpt_patient_id", ""),
                "sf_ids": a.raw.get("sf_ids", ""),
            }
        )
    return rows


def write_amount_xlsx(path: Path, rows: list[dict[str, Any]]) -> int:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "amount_diff"
    ws.append(list(AMOUNT_XLSX_FIELDS))
    for row in rows:
        ws.append([row.get(col, "") for col in AMOUNT_XLSX_FIELDS])
    wb.save(path)
    return len(rows)


def write_reports(output_dir: Path, results: dict[str, list[dict[str, Any]]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = {
        "missing_in_ours.csv": (
            "missing_in_ours",
            [
                "name_key",
                "date_of_service",
                "sf_patient",
                "sf_clinic",
                "sf_status",
                "sf_total_paid",
                "sf_row_count",
                "sf_ids",
                "sf_insurance",
                "reason",
            ],
        ),
        "extra_in_ours.csv": (
            "extra_in_ours",
            [
                "name_key",
                "date_of_service",
                "ours_patient",
                "ours_facility",
                "ours_status",
                "ours_total_paid",
                "webpt_patient_id",
                "reason",
            ],
        ),
        "status_mismatch.csv": (
            "status_mismatch",
            [
                "name_key",
                "date_of_service",
                "sf_patient",
                "ours_patient",
                "sf_clinic",
                "ours_facility",
                "sf_status",
                "ours_status",
                "sf_total_paid",
                "ours_total_paid",
                "webpt_patient_id",
                *STATUS_MISMATCH_SF_DETAIL_FIELDS,
            ],
        ),
        "amount_mismatch.csv": (
            "amount_mismatch",
            [
                "name_key",
                "date_of_service",
                "sf_patient",
                "ours_patient",
                "sf_clinic",
                "ours_facility",
                "sf_status",
                "ours_status",
                "sf_total_paid",
                "ours_total_paid",
                "delta",
            ],
        ),
    }
    meta = results["_meta"][0]
    lines = [
        "Snowflake ALL_BILLING_DATA = reference (visit ledger)",
        "reconciliation_visits.csv = our pipeline visit rollup",
        "",
        f"sf_keys={meta['sf_keys']}",
        f"ours_keys={meta['ours_keys']}",
        f"matched={meta['matched']}",
        f"missing_in_ours={meta['missing_in_ours']}",
        f"extra_in_ours={meta['extra_in_ours']}",
        f"status_mismatch={meta['status_mismatch']}",
        f"amount_mismatch={meta['amount_mismatch']}",
        "",
        "Files:",
    ]
    for filename, (key, fields) in specs.items():
        n = _write_csv(output_dir / filename, results[key], fields)
        lines.append(f"  {filename}: {n} rows")

    summary_path = output_dir / "summary.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compare Snowflake billing visits to reconciliation_visits.csv",
    )
    p.add_argument(
        "--snowflake",
        type=Path,
        default=Path("snowflake_pull/output/all_billing_data.csv"),
    )
    p.add_argument(
        "--visits",
        type=Path,
        default=Path(
            "webpt_edco_scraper/output/jun_jul_2026/reconciliation/reconciliation_visits.csv"
        ),
    )
    p.add_argument("--from", dest="date_from", default="2026-06-01")
    p.add_argument("--to", dest="date_to", default="2026-07-31")
    p.add_argument(
        "--output",
        type=Path,
        default=Path(
            "webpt_edco_scraper/output/jun_jul_2026/reconciliation/sf_compare"
        ),
    )
    p.add_argument(
        "--amount-xlsx-only",
        action="store_true",
        help="Only write amount_diff.xlsx (skip status/missing/extra CSVs).",
    )
    p.add_argument(
        "--dual-key",
        action="store_true",
        help="Also compare on (EMR_ID/webpt_patient_id, DOS) and write coverage_summary.json",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    import json
    from datetime import datetime, timezone

    args = build_parser().parse_args(argv)
    start = parse_date(args.date_from)
    end = parse_date(args.date_to)
    if not args.snowflake.is_file():
        raise SystemExit(f"Snowflake CSV not found: {args.snowflake}")
    if not args.visits.is_file():
        raise SystemExit(f"Visits CSV not found: {args.visits}")

    print(f"Loading Snowflake {args.snowflake} ({args.date_from}..{args.date_to}) ...")
    sf = load_snowflake(args.snowflake, start=start, end=end)
    print(f"  sf keys={len(sf)}")
    print(f"Loading visits {args.visits} ...")
    ours = load_visits(args.visits, start=start, end=end)
    print(f"  ours keys={len(ours)}")

    if args.amount_xlsx_only:
        amount_rows = build_amount_diff_rows(
            sf,
            ours,
            visits_path=args.visits,
            snowflake_path=args.snowflake,
        )
        xlsx_path = args.output / "amount_diff.xlsx"
        n = write_amount_xlsx(xlsx_path, amount_rows)
        print(f"amount_diff rows={n} -> {xlsx_path}")
        return 0

    results = compare(sf, ours)
    summary = write_reports(args.output, results)
    amount_rows = build_amount_diff_rows(
        sf,
        ours,
        visits_path=args.visits,
        snowflake_path=args.snowflake,
    )
    xlsx_path = args.output / "amount_diff.xlsx"
    write_amount_xlsx(xlsx_path, amount_rows)

    if args.dual_key or True:
        # Dual scoreboard is mandatory for coverage recovery KPIs.
        print("Loading EMR-key aggregates ...")
        sf_emr = load_snowflake_by_emr(args.snowflake, start=start, end=end)
        ours_emr = load_visits_by_emr(args.visits, start=start, end=end)
        print(f"  sf emr keys={len(sf_emr)} ours emr keys={len(ours_emr)}")
        emr_results = compare(sf_emr, ours_emr)
        _write_csv(
            args.output / "missing_in_ours_emr_key.csv",
            emr_results["missing_in_ours"],
            [
                "name_key",
                "date_of_service",
                "sf_patient",
                "sf_clinic",
                "sf_status",
                "sf_total_paid",
                "sf_row_count",
                "sf_ids",
                "sf_insurance",
                "reason",
            ],
        )
        cov = coverage_summary_dual(
            name_key_results=results,
            emr_results=emr_results,
            fallback_namekey_count=0,
        )
        cov["generated_at"] = datetime.now(timezone.utc).isoformat()
        cov["window"] = {"from": args.date_from, "to": args.date_to}
        cov_path = args.output / "coverage_summary.json"
        cov_path.write_text(json.dumps(cov, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {cov_path}")
        print(
            "EMR-key missing_in_ours=",
            cov["emr_id"]["missing_in_ours"],
            "measurement_correction_delta=",
            cov["measurement_correction"]["missing_delta"],
        )

    print(summary.read_text(encoding="utf-8"))
    print(f"Also wrote {xlsx_path} ({len(amount_rows)} amount diffs)")
    print(f"Wrote reports -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
