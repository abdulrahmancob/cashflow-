"""Raw WebPT schedule_visits → schedule_appointment + clinical visit (case+DOS)."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from cashflow_db.config import SCHEDULE_VISITS_CSV
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.loaders.base import (
    AppointmentCandidate,
    appointment_from_schedule_row,
    ensure_patient_history,
    select_clinical_appointment,
    upsert_case,
    upsert_clinical_visit,
    upsert_coverage,
    upsert_facility,
    upsert_patient,
    upsert_schedule_appointment,
)
from cashflow_db.util import safe_str


def _resolve_schedule_csv(path: Path | None) -> Path:
    if path and path.exists():
        return path
    if SCHEDULE_VISITS_CSV.exists():
        return SCHEDULE_VISITS_CSV
    # Fallback: newest schedule_visits_*.csv under WEBPT_OUTPUT parent of default
    parent = SCHEDULE_VISITS_CSV.parent
    matches = sorted(parent.glob("schedule_visits_*.csv"), key=lambda p: p.stat().st_mtime)
    if matches:
        return matches[-1]
    raise FileNotFoundError(f"No schedule_visits CSV (tried {SCHEDULE_VISITS_CSV})")


def load_schedule(
    *,
    path: Path | None = None,
    database_url: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    csv_path = _resolve_schedule_csv(path)
    counts = {
        "rows_read": 0,
        "rejected_blank_case": 0,
        "appointments": 0,
        "visits": 0,
        "patients": 0,
        "cases": 0,
    }
    rejected: list[dict[str, str]] = []

    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if limit:
        rows = rows[:limit]
    counts["rows_read"] = len(rows)

    # Group by (facility, case, patient, service_date) using parsed candidates
    groups: dict[tuple[str, str, str, str], list[AppointmentCandidate]] = defaultdict(list)
    for row in rows:
        if not safe_str(row.get("case_id")):
            counts["rejected_blank_case"] += 1
            rejected.append(
                {
                    "facility_id": safe_str(row.get("facility_id")) or "",
                    "patient_id": safe_str(row.get("patient_id")) or "",
                    "appointment_at": safe_str(row.get("appointment_at")) or "",
                    "visit_status": safe_str(row.get("visit_status")) or "",
                    "reject_reason": "CaseMissingOnSchedule",
                }
            )
            continue
        cand = appointment_from_schedule_row(row)
        if not cand or not cand.case_webpt_id or not cand.patient_webpt_id:
            counts["rejected_blank_case"] += 1
            continue
        key = (
            cand.facility_webpt_id or "",
            cand.case_webpt_id,
            cand.patient_webpt_id,
            cand.service_date.isoformat(),
        )
        groups[key].append(cand)

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "webpt", str(csv_path), notes="load_schedule")
        conn.commit()
        try:
            processed = 0
            for _key, cands in groups.items():
                winner = select_clinical_appointment(cands)
                if not winner:
                    continue
                pid = upsert_patient(
                    conn,
                    webpt_patient_id=winner.patient_webpt_id,
                    patient_name=winner.patient_name,
                    etl_run_id=etl_id,
                )
                if not pid:
                    continue
                counts["patients"] += 1
                ensure_patient_history(
                    conn, pid, patient_name=winner.patient_name
                )
                facility_id = upsert_facility(
                    conn,
                    webpt_facility_id=winner.facility_webpt_id,
                    name=winner.facility_name,
                )
                case_pk = upsert_case(
                    conn,
                    webpt_case_id=winner.case_webpt_id,
                    patient_id=pid,
                    facility_id=facility_id,
                    etl_run_id=etl_id,
                )
                if not case_pk:
                    continue
                counts["cases"] += 1

                coverage_id = None
                if winner.insurance_name_raw:
                    coverage_id = upsert_coverage(
                        conn,
                        patient_id=pid,
                        case_pk=case_pk,
                        raw_insurance_name=winner.insurance_name_raw,
                        etl_run_id=etl_id,
                    )

                visit_id = upsert_clinical_visit(
                    conn,
                    case_pk=case_pk,
                    patient_id=pid,
                    facility_id=facility_id,
                    service_date=winner.service_date,
                    appointment_at=winner.appointment_at,
                    status=winner.status,
                    check_in_at=winner.check_in_at,
                    check_out_at=winner.check_out_at,
                    coverage_id=coverage_id,
                    insurance_name_raw=winner.insurance_name_raw,
                    webpt_appointment_id=winner.webpt_appointment_id,
                    etl_run_id=etl_id,
                )
                counts["visits"] += 1

                for cand in cands:
                    upsert_schedule_appointment(
                        conn,
                        case_pk=case_pk,
                        patient_id=pid,
                        facility_id=facility_id,
                        visit_id=visit_id,
                        candidate=cand,
                        is_selected_clinical=(cand.appointment_at == winner.appointment_at),
                        etl_run_id=etl_id,
                    )
                    counts["appointments"] += 1

                processed += 1
                if processed % 200 == 0:
                    conn.commit()

            notes = None
            if rejected:
                notes = f"rejected_blank_case={len(rejected)}"
            finish_etl_run(
                conn,
                etl_id,
                status="success",
                row_count=counts["appointments"] + counts["visits"],
                notes=notes,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            conn.commit()
            raise
    counts["reject_samples"] = len(rejected)
    return counts
