"""Build Case-centric schedule_cases.csv from WebPT schedule visit rows.

Window default: 2026-01-01 .. 2026-09-30.
Source of truth: visit-level schedule export (facility+case+patient+DOS).
Fail-closed: rows with blank case_id go to case_missing_rejects.csv — never guessed.
Never uses patient-collapsed patients CSV / first-row case selection.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_START = date(2026, 1, 1)
DEFAULT_END = date(2026, 9, 30)

SCHEDULE_CASE_FIELDNAMES = [
    "facility_id",
    "facility_name",
    "case_id",
    "patient_id",
    "dos",
    "patient_name",
    "dob",
    "ins_name",
    "visit_status",
    "appointment_at",
    "appointment_id",
    "unit_id",
    "reject_reason",
]


def _repo_root() -> Path:
    return ROOT


def make_unit_id(facility_id: str, case_id: str, patient_id: str, dos: str) -> str:
    from snowflake_pull.case_unit_state import make_case_unit_id

    return make_case_unit_id(facility_id, case_id, patient_id, dos)


def normalize_schedule_row(row: dict[str, str]) -> dict[str, str]:
    """Map schedule export columns → CaseScheduleUnit fields."""
    facility_id = (row.get("facility_id") or "").strip()
    case_id = (row.get("case_id") or "").strip()
    patient_id = (row.get("patient_id") or "").strip()
    dos = (row.get("service_date") or row.get("dos") or "").strip()[:10]
    if not dos:
        appt = (row.get("appointment_at") or "").strip()
        if len(appt) >= 10:
            dos = appt[:10]
    return {
        "facility_id": facility_id,
        "facility_name": (row.get("facility_name") or "").strip(),
        "case_id": case_id,
        "patient_id": patient_id,
        "dos": dos,
        "patient_name": (row.get("patient_name") or "").strip(),
        "dob": (row.get("dob") or "").strip(),
        "ins_name": (row.get("ins_name") or "").strip(),
        "visit_status": (row.get("visit_status") or "").strip(),
        "appointment_at": (row.get("appointment_at") or "").strip(),
        "appointment_id": (row.get("appointment_id") or "").strip(),
    }


def validate_case_schedule_row(row: dict[str, str]) -> str | None:
    """Return reject_reason or None if enqueueable (S0)."""
    if not row.get("facility_id"):
        return "facility_missing"
    if not row.get("patient_id"):
        return "patient_missing"
    if not row.get("dos") or len(row["dos"]) < 10:
        return "dos_missing"
    if not row.get("case_id"):
        return "CaseMissingOnSchedule"
    return None


def build_case_schedule_from_rows(
    rows: list[dict[str, str]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    """Deduplicate to (facility, case, patient, dos); separate rejects."""
    start = start or DEFAULT_START
    end = end or DEFAULT_END
    accepted: dict[tuple[str, str, str, str], dict[str, str]] = {}
    rejects: list[dict[str, str]] = []
    reject_counts: Counter[str] = Counter()
    out_of_window = 0

    for raw in rows:
        unit = normalize_schedule_row(raw)
        dos = unit["dos"]
        if dos:
            try:
                d = date.fromisoformat(dos)
            except ValueError:
                rejects.append({**unit, "unit_id": "", "reject_reason": "dos_invalid"})
                reject_counts["dos_invalid"] += 1
                continue
            if d < start or d > end:
                out_of_window += 1
                continue
        reason = validate_case_schedule_row(unit)
        if reason:
            rejects.append({**unit, "unit_id": "", "reject_reason": reason})
            reject_counts[reason] += 1
            continue
        key = (unit["facility_id"], unit["case_id"], unit["patient_id"], unit["dos"])
        unit_id = make_unit_id(*key)
        out = {**unit, "unit_id": unit_id, "reject_reason": ""}
        # Prefer Checked Out / richer status if duplicate slot
        prev = accepted.get(key)
        if prev is None:
            accepted[key] = out
        else:
            if (
                "checked out" in (out.get("visit_status") or "").lower()
                and "checked out" not in (prev.get("visit_status") or "").lower()
            ):
                accepted[key] = out

    accepted_list = sorted(
        accepted.values(),
        key=lambda r: (r["facility_id"], r["case_id"], r["patient_id"], r["dos"]),
    )
    summary = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "input_rows": len(rows),
        "accepted_units": len(accepted_list),
        "rejected_units": len(rejects),
        "out_of_window_skipped": out_of_window,
        "reject_counts": dict(reject_counts),
        "case_missing_count": int(reject_counts.get("CaseMissingOnSchedule", 0)),
    }
    return accepted_list, rejects, summary


def load_schedule_export_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_schedule_artifacts(
    out_dir: Path,
    accepted: list[dict[str, str]],
    rejects: list[dict[str, str]],
    summary: dict[str, Any],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    schedule_path = out_dir / "schedule_cases.csv"
    reject_path = out_dir / "case_missing_rejects.csv"
    summary_path = out_dir / "schedule_build_summary.json"

    with schedule_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEDULE_CASE_FIELDNAMES)
        w.writeheader()
        for row in accepted:
            w.writerow({k: row.get(k, "") for k in SCHEDULE_CASE_FIELDNAMES})

    with reject_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCHEDULE_CASE_FIELDNAMES)
        w.writeheader()
        for row in rejects:
            w.writerow({k: row.get(k, "") for k in SCHEDULE_CASE_FIELDNAMES})

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {
        "schedule_cases": schedule_path,
        "rejects": reject_path,
        "summary": summary_path,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--schedule-export",
        type=Path,
        required=True,
        help="Path to visit-level schedule CSV (export-schedule / SCHEDULE_EXPORT_FIELDNAMES).",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=_repo_root() / "snowflake_pull" / "artifacts" / "case_schedule",
    )
    ap.add_argument("--start", type=str, default=DEFAULT_START.isoformat())
    ap.add_argument("--end", type=str, default=DEFAULT_END.isoformat())
    args = ap.parse_args()

    t0 = time.perf_counter()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    rows = load_schedule_export_csv(args.schedule_export)
    accepted, rejects, summary = build_case_schedule_from_rows(
        rows, start=start, end=end
    )
    summary["elapsed_sec"] = round(time.perf_counter() - t0, 3)
    summary["unique_facility_case"] = len(
        {(r["facility_id"], r["case_id"]) for r in accepted}
    )
    summary["unique_patients"] = len({r["patient_id"] for r in accepted})
    paths = write_schedule_artifacts(args.out_dir, accepted, rejects, summary)
    print(json.dumps({"summary": summary, "paths": {k: str(v) for k, v in paths.items()}}, indent=2))
    if summary.get("case_missing_count", 0) and not accepted:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
