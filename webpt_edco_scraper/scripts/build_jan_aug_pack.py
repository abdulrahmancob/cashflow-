"""Build Jan–Aug 2026 chart cohort, merge OCR, and pack multi-sheet Excel."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from export_utils import (  # noqa: E402
    PATIENT_EXPORT_FIELDNAMES,
    SCHEDULE_EXPORT_FIELDNAMES,
    empty_ocr_summary,
)
from payments_scrape import PAYMENT_FIELDNAMES  # noqa: E402
from scheduler_api import reclassify_appointment_dates  # noqa: E402

OCR_KEYS = (
    "edoc_ocr_name",
    "edoc_ocr_name_match",
    "edoc_ocr_patient_id",
    "edoc_ocr_id_match",
    "edoc_ocr_diagnosis",
    "edoc_ocr_diagnosis_match",
    "edoc_ocr_source_files",
    "edoc_ocr_file_hints",
    "edoc_ocr_errors",
)


def _parse_dates(cell: str) -> list[str]:
    out: list[str] = []
    for part in (cell or "").split(";"):
        part = part.strip()
        if part:
            out.append(part)
    return out


def row_intersects_window(row: dict[str, str], start: date, end: date) -> bool:
    for raw in _parse_dates(row.get("appointment_dates") or ""):
        day = raw[:10]
        if len(day) != 10:
            continue
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        if start <= d <= end:
            return True
    return False


def filter_chart_cohort(
    rows: list[dict[str, str]],
    *,
    start: date,
    end: date,
    as_of: date,
) -> list[dict[str, str]]:
    """Keep patients with ≥1 appointment in window; trim date lists to window."""
    out: list[dict[str, str]] = []
    for row in rows:
        if not row_intersects_window(row, start, end):
            continue
        kept: list[str] = []
        for raw in _parse_dates(row.get("appointment_dates") or ""):
            day = raw[:10]
            if len(day) != 10:
                continue
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            if start <= d <= end:
                kept.append(raw)
        past, upcoming, past_n, up_n = reclassify_appointment_dates(
            kept, reference_date=as_of
        )
        new_row = dict(row)
        new_row["appointment_dates"] = "; ".join(kept)
        new_row["appointment_count"] = str(len(kept))
        new_row["appointments_past_dates"] = "; ".join(past)
        new_row["appointments_past_count"] = str(past_n)
        new_row["appointments_upcoming_dates"] = "; ".join(upcoming)
        new_row["appointments_upcoming_count"] = str(up_n)
        out.append(new_row)
    return out


def merge_ocr_from_report(
    chart_rows: list[dict[str, str]],
    ocr_report: Path,
) -> int:
    """Merge edoc_ocr_* columns from ocr-batch-test report by patient_id."""
    if not ocr_report.exists():
        return 0
    by_pid: dict[str, dict[str, str]] = {}
    with ocr_report.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            pid = str(r.get("patient_id") or "").strip()
            if pid:
                by_pid[pid] = r
    filled = 0
    for row in chart_rows:
        pid = str(row.get("patient_id") or "").strip()
        src = by_pid.get(pid)
        if not src:
            continue
        if any((row.get(k) or "").strip() for k in OCR_KEYS if k != "edoc_ocr_errors"):
            continue
        for k in OCR_KEYS:
            if k in src:
                row[k] = src.get(k) or ""
        filled += 1
    return filled


def merge_ocr_from_edocs(
    chart_rows: list[dict[str, str]],
    edocs_dir: Path,
    *,
    max_patients: int | None = None,
) -> int:
    """Fill OCR columns from patient .ocr_cache.txt (fast path, no per-file re-OCR)."""
    from edoc_ocr import (
        OCR_CACHE_FILENAME,
        _cache_valid,
        collect_patient_pdf_paths,
        extract_patient_fields,
        validate_ocr_fields,
    )

    if not edocs_dir.exists():
        return 0
    filled = 0
    skipped_no_cache = 0
    for row in chart_rows:
        if max_patients is not None and filled >= max_patients:
            break
        if any((row.get(k) or "").strip() for k in OCR_KEYS if k != "edoc_ocr_errors"):
            continue
        pid = str(row.get("patient_id") or "").strip()
        if not pid:
            continue
        patient_dir = edocs_dir / pid
        if not patient_dir.is_dir():
            continue
        pdfs = collect_patient_pdf_paths(patient_dir)
        if not pdfs:
            row.update(empty_ocr_summary(error="no PDF files"))
            continue
        # Cache-only: do not run fresh Tesseract during pack (too slow for 20k+).
        cache_file = patient_dir / OCR_CACHE_FILENAME
        if not _cache_valid(cache_file, pdfs):
            row.update(empty_ocr_summary(error="ocr_cache_missing"))
            skipped_no_cache += 1
            continue
        try:
            ocr_text = cache_file.read_text(encoding="utf-8")
            used_files = [p.name for p in pdfs]
            ocr_errors: list[str] = []
        except Exception as exc:  # noqa: BLE001
            row.update(empty_ocr_summary(error=str(exc)))
            filled += 1
            continue
        if not (ocr_text or "").strip():
            err = " | ".join(ocr_errors) if ocr_errors else "OCR produced no text"
            row.update(empty_ocr_summary(error=err))
            filled += 1
            continue
        expected_name = row.get("patient_name") or ""
        expected_diagnosis = row.get("diagnosis") or ""
        extracted = extract_patient_fields(
            ocr_text,
            expected_name=expected_name,
            expected_id=pid,
        )
        extracted["_ocr_text"] = ocr_text
        matches = validate_ocr_fields(
            extracted,
            expected_name=expected_name,
            expected_id=pid,
            expected_diagnosis=expected_diagnosis,
        )
        if not expected_diagnosis.strip():
            matches["edoc_ocr_diagnosis_match"] = ""
            ocr_errors = list(ocr_errors or [])
            ocr_errors.append("chart diagnosis unavailable")
        row["edoc_ocr_name"] = str(extracted.get("edoc_ocr_name") or "")
        row["edoc_ocr_patient_id"] = str(extracted.get("edoc_ocr_patient_id") or "")
        row["edoc_ocr_diagnosis"] = str(extracted.get("edoc_ocr_diagnosis") or "")
        row["edoc_ocr_name_match"] = str(matches.get("edoc_ocr_name_match") or "")
        row["edoc_ocr_id_match"] = str(matches.get("edoc_ocr_id_match") or "")
        row["edoc_ocr_diagnosis_match"] = str(
            matches.get("edoc_ocr_diagnosis_match") or ""
        )
        row["edoc_ocr_source_files"] = "; ".join(used_files)
        row["edoc_ocr_file_hints"] = ""
        row["edoc_ocr_errors"] = " | ".join(list(ocr_errors or [])[:3])
        filled += 1
        if filled % 500 == 0:
            print(f"OCR merged {filled} patients...", flush=True)
    print(
        f"OCR cache-only done: filled={filled} skipped_no_cache={skipped_no_cache}",
        flush=True,
    )
    return filled


def write_gap_report(chart_rows: list[dict[str, str]], path: Path) -> dict[str, int]:
    gaps: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for row in chart_rows:
        reasons: list[str] = []
        if not (row.get("diagnosis") or "").strip():
            reasons.append("empty_diagnosis")
        if (row.get("edoc_status") or "") == "failed":
            reasons.append("edoc_failed")
        if (row.get("chart_notes_status") or "") in ("failed", "pending", "partial"):
            reasons.append(f"chart_notes_{(row.get('chart_notes_status') or '')}")
        if not (row.get("edoc_ocr_name") or "").strip() and not (
            row.get("edoc_ocr_errors") or ""
        ).strip():
            reasons.append("ocr_empty")
        if not reasons:
            continue
        for r in reasons:
            counts[r] += 1
        gaps.append(
            {
                "facility_id": row.get("facility_id") or "",
                "patient_id": row.get("patient_id") or "",
                "patient_name": row.get("patient_name") or "",
                "case_id": row.get("case_id") or "",
                "edoc_status": row.get("edoc_status") or "",
                "chart_notes_status": row.get("chart_notes_status") or "",
                "reasons": "; ".join(reasons),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fields = [
            "facility_id",
            "patient_id",
            "patient_name",
            "case_id",
            "edoc_status",
            "chart_notes_status",
            "reasons",
        ]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(gaps)
    return dict(counts)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def join_schedule_chart_fields(
    schedule_rows: list[dict[str, str]],
    chart_csv: Path,
) -> int:
    """Fill auth/copay/deductible/ins_name on schedule rows from chart export."""
    lookup = {}
    if chart_csv.exists():
        with chart_csv.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                key = (
                    str(r.get("facility_id") or "").strip(),
                    str(r.get("patient_id") or "").strip(),
                    str(r.get("case_id") or "").strip(),
                )
                lookup[key] = r
    n = 0
    for row in schedule_rows:
        key = (
            str(row.get("facility_id") or "").strip(),
            str(row.get("patient_id") or "").strip(),
            str(row.get("case_id") or "").strip(),
        )
        src = lookup.get(key)
        if not src:
            continue
        for k in ("auth_ins_visits", "copay", "deductible"):
            if not (row.get(k) or "").strip() and (src.get(k) or "").strip():
                row[k] = src[k]
        if not (row.get("ins_name") or "").strip() and (src.get("ins_name") or "").strip():
            row["ins_name"] = src["ins_name"]
        n += 1
    return n


def pack_xlsx(
    *,
    chart_csv: Path,
    schedule_csv: Path,
    payments_csv: Path,
    xlsx_path: Path,
) -> None:
    from openpyxl import Workbook

    if schedule_csv.exists() and chart_csv.exists():
        sched = _read_csv(schedule_csv)
        joined = join_schedule_chart_fields(sched, chart_csv)
        if joined:
            _write_csv(schedule_csv, sched, SCHEDULE_EXPORT_FIELDNAMES)
            print(f"Joined chart fields onto {joined} schedule row(s)")

    sheets = [
        ("chart", chart_csv, PATIENT_EXPORT_FIELDNAMES),
        ("schedule", schedule_csv, SCHEDULE_EXPORT_FIELDNAMES),
        ("payments", payments_csv, PAYMENT_FIELDNAMES),
    ]
    wb = Workbook()
    wb.remove(wb.active)
    for title, csv_path, fields in sheets:
        ws = wb.create_sheet(title)
        rows = _read_csv(csv_path)
        use_fields = list(fields)
        if rows:
            # Preserve any extra columns present in the CSV.
            for k in rows[0].keys():
                if k not in use_fields:
                    use_fields.append(k)
        ws.append(use_fields)
        for r in rows:
            ws.append([r.get(c, "") for c in use_fields])
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input-export",
        type=Path,
        default=ROOT / "output/jun_jul_2026/patients_export_10d.csv",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output/jan_aug_2026",
    )
    p.add_argument("--start-date", type=str, default="2026-01-01")
    p.add_argument("--end-date", type=str, default="2026-08-30")
    p.add_argument("--as-of", type=str, default=None)
    p.add_argument(
        "--edocs-dir",
        type=Path,
        default=ROOT / "output/jun_jul_2026/edocs",
    )
    p.add_argument(
        "--ocr-report",
        type=Path,
        default=None,
        help="Optional ocr-batch-test report to merge",
    )
    p.add_argument(
        "--merge-ocr-from-edocs",
        action="store_true",
        help="Fill OCR via local PDF caches (slow for full cohort)",
    )
    p.add_argument("--max-ocr-patients", type=int, default=None)
    p.add_argument(
        "--schedule-csv",
        type=Path,
        default=None,
        help="Default: output_dir/schedule_visits_{start}_{end}.csv",
    )
    p.add_argument(
        "--payments-csv",
        type=Path,
        default=None,
        help="Default: output_dir/patient_payments_202601_202608.csv",
    )
    p.add_argument(
        "--pack-only",
        action="store_true",
        help="Skip cohort filter/OCR; only build xlsx from existing CSVs",
    )
    args = p.parse_args()

    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    as_of = date.fromisoformat(args.as_of) if args.as_of else end
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    chart_path = out / "patients_export_jan_aug_2026.csv"
    schedule_csv = args.schedule_csv or (
        out / f"schedule_visits_{start.isoformat()}_{end.isoformat()}.csv"
    )
    payments_csv = args.payments_csv or (
        out / "patient_payments_202601_202608.csv"
    )
    xlsx_path = out / "webpt_jan_aug_2026.xlsx"
    gap_path = out / "gap_report.csv"

    if not args.pack_only:
        rows = _read_csv(args.input_export)
        print(f"Loaded {len(rows)} rows from {args.input_export}")
        cohort = filter_chart_cohort(rows, start=start, end=end, as_of=as_of)
        print(f"Jan–Aug cohort: {len(cohort)} patients")

        ocr_report = args.ocr_report or (out / "ocr_batch_report.csv")
        n = merge_ocr_from_report(cohort, ocr_report)
        print(f"OCR merged from report: {n}")
        if args.merge_ocr_from_edocs:
            n2 = merge_ocr_from_edocs(
                cohort,
                args.edocs_dir,
                max_patients=args.max_ocr_patients,
            )
            print(f"OCR merged from edocs caches: {n2}")

        _write_csv(chart_path, cohort, PATIENT_EXPORT_FIELDNAMES)
        gap_counts = write_gap_report(cohort, gap_path)
        print(f"Wrote {chart_path} ({len(cohort)} rows)")
        print(f"Gap report {gap_path}: {gap_counts}")
    else:
        print(f"Pack-only using chart={chart_path}")

    pack_xlsx(
        chart_csv=chart_path,
        schedule_csv=schedule_csv,
        payments_csv=payments_csv,
        xlsx_path=xlsx_path,
    )
    print(f"Wrote {xlsx_path}")


if __name__ == "__main__":
    main()
