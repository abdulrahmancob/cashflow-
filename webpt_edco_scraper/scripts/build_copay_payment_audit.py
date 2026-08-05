"""Audit Checked Out visits vs Patient Payments for cases with real copay/deductible."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SUMMARY_FIELDS = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "case_id",
    "case_label",
    "ins_name",
    "copay",
    "deductible",
    "checked_out_visits",
    "visits_with_payment",
    "visits_without_payment",
    "payment_coverage_pct",
    "payment_txn_count",
    "amount_due_total",
    "amount_paid_total",
]

DETAIL_FIELDS = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "case_id",
    "case_label",
    "ins_name",
    "copay",
    "deductible",
    "appointment_at",
    "service_date",
    "visit_status",
    "checkin_time",
    "checkout_time",
    "has_payment",
    "payment_txn_count",
    "payment_types",
    "amount_due",
    "amount_paid",
]

# Values that mean "no obligation" (case-insensitive, punctuation stripped lightly).
_NO_OBLIGATION = frozenset(
    {
        "",
        "no",
        "n",
        "none",
        "na",
        "n/a",
        "nil",
        "0",
        "0.00",
        "$0",
        "0$",
        "no copay",
        "nocopay",
        "no deductible",
        "nodeductible",
        "waived",
        "-",
        "--",
    }
)


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def is_owed(value: str) -> bool:
    """True when copay/deductible cell indicates a real patient obligation."""
    n = _norm(value)
    if n in _NO_OBLIGATION:
        return False
    # "no $15" style is rare; treat bare no* without digits as negative.
    if n.startswith("no ") and not re.search(r"\d", n):
        return False
    return bool(n)


def _service_date(appointment_at: str) -> str:
    raw = (appointment_at or "").strip()
    return raw[:10] if len(raw) >= 10 else ""


def _f(raw: Any) -> float:
    try:
        return float(str(raw or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_money_amount(value: str) -> float | None:
    """Extract first dollar/number from a chart cell, or None if unparsable."""
    n = _norm(value)
    if not n or n in _NO_OBLIGATION:
        return None
    if n.startswith("no ") and not re.search(r"\d", n):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", n.replace(",", ""))
    if not match:
        return None
    amount = float(match.group(1))
    return amount if amount > 0 else None


def paid_equals_deductible(*, amount_paid_total: float, deductible: str) -> bool:
    """True when paid total matches the numeric deductible within $0.01."""
    ded = parse_money_amount(deductible)
    if ded is None:
        return False
    return abs(amount_paid_total - ded) < 0.01


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_xlsx(
    path: Path,
    *,
    summary: list[dict[str, Any]],
    detail: list[dict[str, Any]],
) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws_s = wb.active
    ws_s.title = "case_summary"
    ws_s.append(SUMMARY_FIELDS)
    for r in summary:
        ws_s.append([r.get(c, "") for c in SUMMARY_FIELDS])

    ws_d = wb.create_sheet("visit_detail")
    ws_d.append(DETAIL_FIELDS)
    for r in detail:
        ws_d.append([r.get(c, "") for c in DETAIL_FIELDS])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def build_audit(
    *,
    schedule_csv: Path,
    payments_csv: Path,
    chart_csv: Path,
    output_dir: Path,
    service_date_from: str | None = None,
    service_date_to: str | None = None,
) -> dict[str, Any]:
    schedule = _read_csv(schedule_csv)
    payments = _read_csv(payments_csv)
    chart = _read_csv(chart_csv) if chart_csv.exists() else []

    date_from = (service_date_from or "").strip() or None
    date_to = (service_date_to or "").strip() or None

    chart_lookup: dict[tuple[str, str, str], dict[str, str]] = {}
    for r in chart:
        key = (
            str(r.get("facility_id") or "").strip(),
            str(r.get("patient_id") or "").strip(),
            str(r.get("case_id") or "").strip(),
        )
        if key[1] and key[2]:
            chart_lookup[key] = r

    # Index payments: (facility, patient, case, dos) -> list of txns
    pay_index: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in payments:
        dos = str(r.get("date_of_service_iso") or "").strip()
        if not dos:
            continue
        key = (
            str(r.get("facility_id") or "").strip(),
            str(r.get("patient_id") or "").strip(),
            str(r.get("case_id") or "").strip(),
            dos,
        )
        pay_index[key].append(r)

    # Checked Out visits only (optional service_date window)
    checked_out: list[dict[str, str]] = []
    for r in schedule:
        if (r.get("visit_status") or "").strip() != "Checked Out":
            continue
        service_date = _service_date(r.get("appointment_at") or "")
        if not service_date:
            continue
        if date_from and service_date < date_from:
            continue
        if date_to and service_date > date_to:
            continue
        checked_out.append(r)

    # Enrich + filter to owed cohort (per visit, using case chart/schedule fields)
    detail: list[dict[str, Any]] = []
    case_meta: dict[tuple[str, str, str], dict[str, str]] = {}
    case_visits: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for v in checked_out:
        fid = str(v.get("facility_id") or "").strip()
        pid = str(v.get("patient_id") or "").strip()
        cid = str(v.get("case_id") or "").strip()
        if not pid or not cid:
            continue
        case_key = (fid, pid, cid)
        chart_row = chart_lookup.get(case_key) or {}
        copay = (chart_row.get("copay") or v.get("copay") or "").strip()
        deductible = (chart_row.get("deductible") or v.get("deductible") or "").strip()
        if not (is_owed(copay) or is_owed(deductible)):
            continue

        if case_key not in case_meta:
            case_meta[case_key] = {
                "facility_id": fid,
                "facility_name": str(
                    chart_row.get("facility_name") or v.get("facility_name") or ""
                ),
                "patient_id": pid,
                "patient_name": str(
                    chart_row.get("patient_name") or v.get("patient_name") or ""
                ),
                "case_id": cid,
                "case_label": str(v.get("case_label") or ""),
                "ins_name": str(chart_row.get("ins_name") or v.get("ins_name") or ""),
                "copay": copay,
                "deductible": deductible,
            }

        service_date = _service_date(v.get("appointment_at") or "")
        pay_key = (fid, pid, cid, service_date)
        txns = pay_index.get(pay_key) or []
        due = sum(_f(t.get("amount_due")) for t in txns)
        paid = sum(_f(t.get("amount_paid")) for t in txns)
        types = sorted({(t.get("payment_type") or "").strip() for t in txns if t.get("payment_type")})
        row = {
            **case_meta[case_key],
            "appointment_at": v.get("appointment_at") or "",
            "service_date": service_date,
            "visit_status": v.get("visit_status") or "",
            "checkin_time": v.get("checkin_time") or "",
            "checkout_time": v.get("checkout_time") or "",
            "has_payment": "yes" if txns else "no",
            "payment_txn_count": len(txns),
            "payment_types": "; ".join(types),
            "amount_due": f"{due:.2f}" if txns else "0.00",
            "amount_paid": f"{paid:.2f}" if txns else "0.00",
        }
        detail.append(row)
        case_visits[case_key].append(row)

    detail.sort(
        key=lambda r: (
            str(r.get("facility_id") or ""),
            str(r.get("patient_name") or ""),
            str(r.get("patient_id") or ""),
            str(r.get("case_id") or ""),
            str(r.get("service_date") or ""),
            str(r.get("appointment_at") or ""),
        )
    )

    summary: list[dict[str, Any]] = []
    excluded_paid_ded = 0
    kept_case_keys: set[tuple[str, str, str]] = set()
    for case_key, visits in case_visits.items():
        meta = case_meta[case_key]
        with_pay = sum(1 for x in visits if x["has_payment"] == "yes")
        without = len(visits) - with_pay
        pct = (100.0 * with_pay / len(visits)) if visits else 0.0
        txn_count = sum(int(x["payment_txn_count"]) for x in visits)
        due_total = sum(_f(x["amount_due"]) for x in visits)
        paid_total = sum(_f(x["amount_paid"]) for x in visits)
        if paid_equals_deductible(
            amount_paid_total=paid_total,
            deductible=str(meta.get("deductible") or ""),
        ):
            excluded_paid_ded += 1
            continue
        kept_case_keys.add(case_key)
        summary.append(
            {
                **meta,
                "checked_out_visits": len(visits),
                "visits_with_payment": with_pay,
                "visits_without_payment": without,
                "payment_coverage_pct": f"{pct:.1f}",
                "payment_txn_count": txn_count,
                "amount_due_total": f"{due_total:.2f}",
                "amount_paid_total": f"{paid_total:.2f}",
            }
        )

    detail = [
        r
        for r in detail
        if (
            str(r.get("facility_id") or "").strip(),
            str(r.get("patient_id") or "").strip(),
            str(r.get("case_id") or "").strip(),
        )
        in kept_case_keys
    ]

    summary.sort(
        key=lambda r: (
            -int(r["visits_without_payment"]),
            str(r.get("facility_name") or ""),
            str(r.get("patient_name") or ""),
            str(r.get("case_id") or ""),
        )
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "copay_deductible_case_summary.csv"
    detail_csv = output_dir / "copay_deductible_visit_detail.csv"
    xlsx_path = output_dir / "copay_deductible_visit_payment_audit.xlsx"

    _write_csv(summary_csv, summary, SUMMARY_FIELDS)
    _write_csv(detail_csv, detail, DETAIL_FIELDS)
    _write_xlsx(xlsx_path, summary=summary, detail=detail)

    stats = {
        "checked_out_total": len(checked_out),
        "cohort_cases": len(summary),
        "cohort_visits": len(detail),
        "excluded_paid_equals_deductible": excluded_paid_ded,
        "visits_with_payment": sum(1 for r in detail if r["has_payment"] == "yes"),
        "visits_without_payment": sum(1 for r in detail if r["has_payment"] == "no"),
        "summary_csv": str(summary_csv),
        "detail_csv": str(detail_csv),
        "xlsx": str(xlsx_path),
    }
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--schedule-csv",
        type=Path,
        default=ROOT
        / "output/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv",
    )
    p.add_argument(
        "--payments-csv",
        type=Path,
        default=ROOT / "output/jan_aug_2026/patient_payments_202601_202608.csv",
    )
    p.add_argument(
        "--chart-csv",
        type=Path,
        default=ROOT / "output/jan_aug_2026/patients_export_jan_aug_2026.csv",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output/jan_aug_2026",
    )
    p.add_argument(
        "--service-date-from",
        type=str,
        default=None,
        help="Inclusive YYYY-MM-DD lower bound on visit service_date",
    )
    p.add_argument(
        "--service-date-to",
        type=str,
        default=None,
        help="Inclusive YYYY-MM-DD upper bound on visit service_date",
    )
    args = p.parse_args()

    stats = build_audit(
        schedule_csv=args.schedule_csv,
        payments_csv=args.payments_csv,
        chart_csv=args.chart_csv,
        output_dir=args.output_dir,
        service_date_from=args.service_date_from,
        service_date_to=args.service_date_to,
    )
    print(
        f"Checked Out total={stats['checked_out_total']} | "
        f"cohort cases={stats['cohort_cases']} visits={stats['cohort_visits']} | "
        f"excluded paid==deductible={stats['excluded_paid_equals_deductible']} | "
        f"with_payment={stats['visits_with_payment']} "
        f"without_payment={stats['visits_without_payment']}"
    )
    print(f"Wrote {stats['xlsx']}")
    print(f"Wrote {stats['summary_csv']}")
    print(f"Wrote {stats['detail_csv']}")


if __name__ == "__main__":
    main()
