"""Expand case-aware clinical extracts by joining legacy notes/CPT to schedule.

Legacy jun_jul extracts lack case_id. When schedule has exactly one case for
(patient_id, facility_id, service_date), we attach that case_id and write
enriched CSVs under CASE_PIPELINE_DIR/extracted for load_webpt.

Ambiguous (multi-case same day) rows are skipped — never guess.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from cashflow_db.config import CASE_PIPELINE_DIR, SCHEDULE_VISITS_CSV, WEBPT_LEGACY_OUTPUT
from cashflow_db.util import parse_datetime, safe_str


def _load_schedule_case_index(schedule_csv: Path) -> dict[tuple[str, str, str], set[str]]:
    """(patient_id, facility_id, service_date_iso) → set of case_id."""
    index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    with schedule_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            case_id = safe_str(row.get("case_id"))
            patient_id = safe_str(row.get("patient_id"))
            facility_id = safe_str(row.get("facility_id"))
            appt = parse_datetime(row.get("appointment_at"))
            if not case_id or not patient_id or not facility_id or not appt:
                continue
            key = (patient_id, facility_id, appt.date().isoformat())
            index[key].add(case_id)
    return index


def _resolve_case(
    index: dict[tuple[str, str, str], set[str]],
    *,
    patient_id: str | None,
    facility_id: str | None,
    service_date: str | None,
) -> str | None:
    if not patient_id or not service_date:
        return None
    # Prefer facility-scoped unique case
    if facility_id:
        cases = index.get((patient_id, facility_id, service_date), set())
        if len(cases) == 1:
            return next(iter(cases))
        if len(cases) > 1:
            return None
    # Fall back: unique across all facilities for that patient+DOS
    union: set[str] = set()
    for (pid, _fid, dos), cases in index.items():
        if pid == patient_id and dos == service_date:
            union |= cases
    if len(union) == 1:
        return next(iter(union))
    return None


def enrich_file(
    src: Path,
    dest: Path,
    index: dict[tuple[str, str, str], set[str]],
    *,
    date_field: str,
) -> dict[str, int]:
    counts = {"in": 0, "out": 0, "already_cased": 0, "resolved": 0, "skipped_ambiguous": 0}
    if not src.exists():
        return counts
    with src.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        if "case_id" not in fieldnames:
            fieldnames.append("case_id")
        if "facility_id" not in fieldnames:
            fieldnames.append("facility_id")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8", newline="") as out_fh:
            writer = csv.DictWriter(out_fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in reader:
                counts["in"] += 1
                if safe_str(row.get("case_id")) and safe_str(row.get("facility_id")):
                    counts["already_cased"] += 1
                    writer.writerow(row)
                    counts["out"] += 1
                    continue
                dos_raw = safe_str(row.get(date_field))
                dos = dos_raw[:10] if dos_raw else None
                case_id = _resolve_case(
                    index,
                    patient_id=safe_str(row.get("patient_id")),
                    facility_id=safe_str(row.get("facility_id")),
                    service_date=dos,
                )
                if not case_id:
                    counts["skipped_ambiguous"] += 1
                    continue
                row = dict(row)
                row["case_id"] = case_id
                # facility from schedule if missing — pick any matching key
                if not safe_str(row.get("facility_id")):
                    for (pid, fid, d), cases in index.items():
                        if pid == row.get("patient_id") and d == dos and case_id in cases:
                            row["facility_id"] = fid
                            break
                if not safe_str(row.get("facility_id")):
                    counts["skipped_ambiguous"] += 1
                    continue
                counts["resolved"] += 1
                writer.writerow(row)
                counts["out"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enrich legacy extracts with case_id")
    parser.add_argument(
        "--schedule",
        type=Path,
        default=SCHEDULE_VISITS_CSV,
        help="schedule_visits CSV",
    )
    parser.add_argument(
        "--legacy-extracted",
        type=Path,
        default=WEBPT_LEGACY_OUTPUT / "extracted",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=CASE_PIPELINE_DIR / "extracted",
    )
    args = parser.parse_args(argv)

    if not args.schedule.exists():
        print(f"Missing schedule: {args.schedule}", file=sys.stderr)
        return 1

    index = _load_schedule_case_index(args.schedule)
    print(f"Schedule index keys: {len(index)}")

    notes = enrich_file(
        args.legacy_extracted / "daily_notes.csv",
        args.out_dir / "daily_notes.csv",
        index,
        date_field="date_of_daily_note",
    )
    cpt = enrich_file(
        args.legacy_extracted / "cpt_codes.csv",
        args.out_dir / "cpt_codes.csv",
        index,
        date_field="date_of_service",
    )
    print({"daily_notes": notes, "cpt_codes": cpt})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
