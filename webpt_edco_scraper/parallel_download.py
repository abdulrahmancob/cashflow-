"""Parallel patient PDF download from export CSV (Phase 2)."""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from auth import (
    ClinicInfo,
    bootstrap_shared_session,
    create_context,
    ensure_authenticated,
    refresh_csrf,
    switch_clinic,
)
from config import SCHEDULER_INDEX_URL, WebPTConfig
from export_utils import (
    EDOC_MANIFEST_FIELDNAMES,
    PATIENT_EXPORT_FIELDNAMES,
    build_patient_export_row,
)
from logging_config import get_logger
from pdf_throttle import set_pdf_semaphore
from scheduler_api import SchedulerPatient

log = get_logger("parallel_download")

CHART_FIELD_KEYS = (
    "auth_ins_visits",
    "cancel_no_show",
    "visits_in_case",
    "assigned_therapist",
    "diagnosis",
    "deductible",
    "copay",
    "limit_per_year",
    "referral_required",
    "additional_info_raw",
)


@dataclass
class ParallelPatientJob:
    patient: SchedulerPatient
    facility_name: str
    chart_fields: dict[str, str]
    diagnosis: str


def _patient_key(facility_id: str | int, patient_id: int) -> str:
    return f"{facility_id}:{patient_id}"


def _load_download_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data.get("processed_patient_ids") or [])


def _save_download_checkpoint(path: Path, processed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"processed_patient_ids": sorted(processed)}, fh, indent=2)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _append_manifest_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EDOC_MANIFEST_FIELDNAMES, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _parse_case_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_int(raw: str, default: int = 0) -> int:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_jobs_from_csv(path: Path) -> list[ParallelPatientJob]:
    jobs: list[ParallelPatientJob] = []
    for row in _read_csv_rows(path):
        pid_raw = row.get("patient_id") or row.get("PatientID")
        fid_raw = row.get("facility_id") or row.get("FacilityID")
        if not pid_raw or not fid_raw:
            log.warning("Skipping row missing patient_id or facility_id: %s", row)
            continue
        chart_fields = {k: row.get(k, "") or "" for k in CHART_FIELD_KEYS}
        patient = SchedulerPatient(
            patient_id=int(pid_raw),
            facility_id=int(fid_raw),
            case_id=_parse_case_id(row.get("case_id") or ""),
            patient_name=row.get("patient_name") or "",
            dob=row.get("dob") or "",
            case_label=row.get("case_label") or "",
            ins_name=row.get("ins_name") or "",
            appointment_count=_parse_int(row.get("appointment_count") or ""),
            appointment_dates=[
                d.strip()
                for d in (row.get("appointment_dates") or "").split(";")
                if d.strip()
            ],
            appointments_past_count=_parse_int(row.get("appointments_past_count") or ""),
            appointments_past_dates=[
                d.strip()
                for d in (row.get("appointments_past_dates") or "").split(";")
                if d.strip()
            ],
            appointments_upcoming_count=_parse_int(
                row.get("appointments_upcoming_count") or ""
            ),
            appointments_upcoming_dates=[
                d.strip()
                for d in (row.get("appointments_upcoming_dates") or "").split(";")
                if d.strip()
            ],
        )
        jobs.append(
            ParallelPatientJob(
                patient=patient,
                facility_name=row.get("facility_name") or "",
                chart_fields=chart_fields,
                diagnosis=chart_fields.get("diagnosis", ""),
            )
        )
    return jobs


class ParallelDownloadState:
    def __init__(
        self,
        *,
        checkpoint_path: Path,
        manifest_path: Path,
        checkpoint_every: int,
        existing_export: Path | None = None,
    ) -> None:
        self.checkpoint_path = checkpoint_path
        self.manifest_path = manifest_path
        self.checkpoint_every = checkpoint_every
        self.lock = asyncio.Lock()
        self.processed: set[str] = _load_download_checkpoint(checkpoint_path)
        self.export_rows: dict[str, dict[str, Any]] = {}
        if existing_export and existing_export.exists():
            for row in _read_csv_rows(existing_export):
                pid = row.get("patient_id")
                fid = row.get("facility_id")
                if pid and fid:
                    self.export_rows[f"{fid}:{pid}"] = row
        self.since_flush = 0
        self.total_done = 0

    async def record_success(
        self,
        job: ParallelPatientJob,
        *,
        manifest_rows: list[dict[str, Any]],
        edoc_summary: dict[str, Any],
        chart_notes_summary: dict[str, Any],
    ) -> None:
        key = _patient_key(job.patient.facility_id, job.patient.patient_id)
        export_row = build_patient_export_row(
            clinic_name=job.facility_name,
            patient=job.patient,
            chart_fields=job.chart_fields,
            edoc_summary=edoc_summary,
            chart_notes_summary=chart_notes_summary,
            ocr_summary={
                "edoc_ocr_name": "",
                "edoc_ocr_name_match": "",
                "edoc_ocr_patient_id": "",
                "edoc_ocr_id_match": "",
                "edoc_ocr_diagnosis": "",
                "edoc_ocr_diagnosis_match": "",
                "edoc_ocr_source_files": "",
                "edoc_ocr_file_hints": "",
                "edoc_ocr_errors": "",
            },
        )
        async with self.lock:
            self.processed.add(key)
            self.export_rows[key] = export_row
            if manifest_rows:
                _append_manifest_rows(self.manifest_path, manifest_rows)
            self.since_flush += 1
            self.total_done += 1
            if self.checkpoint_every > 0 and self.since_flush >= self.checkpoint_every:
                _save_download_checkpoint(self.checkpoint_path, self.processed)
                self.since_flush = 0
                log.info("Parallel checkpoint saved (%d patients done)", self.total_done)

    async def finalize(self, export_path: Path, all_jobs: list[ParallelPatientJob]) -> None:
        async with self.lock:
            _save_download_checkpoint(self.checkpoint_path, self.processed)
            rows: list[dict[str, Any]] = []
            for job in all_jobs:
                key = _patient_key(job.patient.facility_id, job.patient.patient_id)
                if key in self.export_rows:
                    rows.append(self.export_rows[key])
                else:
                    rows.append(
                        build_patient_export_row(
                            clinic_name=job.facility_name,
                            patient=job.patient,
                            chart_fields=job.chart_fields,
                        )
                    )
            if rows:
                _write_csv_rows(export_path, rows, PATIENT_EXPORT_FIELDNAMES)


_clinic_switch_lock = asyncio.Lock()


async def _download_worker(
    worker_id: int,
    *,
    queue: asyncio.Queue[ParallelPatientJob | None],
    config: WebPTConfig,
    output_dir: Path,
    state: ParallelDownloadState,
    skip_existing: bool,
    skip_edocs: bool,
    skip_chart_notes: bool,
) -> None:
    from scraper import _process_patient_edocs

    await asyncio.sleep(1.5 * worker_id)

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        current_facility: str | None = None
        session = None
        try:
            while True:
                job = await queue.get()
                try:
                    if job is None:
                        break
                    key = _patient_key(job.patient.facility_id, job.patient.patient_id)
                    if key in state.processed:
                        log.debug("Worker %d skip done %s", worker_id, key)
                        continue

                    fid = str(job.patient.facility_id)
                    if fid != current_facility:
                        async with _clinic_switch_lock:
                            session = await ensure_authenticated(page, context, config)
                            await switch_clinic(
                                page,
                                company_id=config.company_id,
                                facility_id=fid,
                            )
                            await page.goto(
                                SCHEDULER_INDEX_URL,
                                wait_until="domcontentloaded",
                                timeout=45000,
                            )
                        session = await refresh_csrf(context, page)
                        current_facility = fid

                    clinic = ClinicInfo(
                        company_id=config.company_id,
                        facility_id=fid,
                        name=job.facility_name,
                    )
                    edocs_dir = output_dir / "edocs"
                    manifest_rows, edoc_summary, chart_notes_summary, _ = (
                        await _process_patient_edocs(
                            context,
                            clinic=clinic,
                            patient=job.patient,
                            config=config,
                            session=session,
                            edocs_dir=edocs_dir,
                            skip_existing=skip_existing,
                            skip_edocs=skip_edocs,
                            skip_chart_notes=skip_chart_notes,
                            skip_ocr=True,
                            expected_diagnosis=job.diagnosis,
                            page=page,
                        )
                    )
                    await state.record_success(
                        job,
                        manifest_rows=manifest_rows,
                        edoc_summary=edoc_summary,
                        chart_notes_summary=chart_notes_summary,
                    )
                    log.info(
                        "Worker %d done %s (%s) edoc=%s chart_notes=%s",
                        worker_id,
                        job.patient.patient_id,
                        job.patient.patient_name,
                        edoc_summary.get("edoc_status"),
                        chart_notes_summary.get("chart_notes_status"),
                    )
                except Exception as exc:
                    log.error(
                        "Worker %d failed patient %s: %s",
                        worker_id,
                        job.patient.patient_id if job else "?",
                        exc,
                    )
                finally:
                    queue.task_done()
        finally:
            await context.browser.close()


async def run_parallel_download(
    config: WebPTConfig,
    *,
    input_csv: Path,
    output_dir: Path,
    workers: int | None = None,
    skip_existing: bool = True,
    skip_edocs: bool = False,
    skip_chart_notes: bool = False,
    checkpoint_every: int = 25,
    max_patients: int | None = None,
) -> None:
    worker_count = workers if workers is not None else config.parallel_workers
    if worker_count < 1:
        raise ValueError("--workers must be >= 1")

    jobs = load_jobs_from_csv(input_csv)
    if not jobs:
        raise RuntimeError(f"No patients in {input_csv}")

    checkpoint_path = output_dir / "download_checkpoint.json"
    state = ParallelDownloadState(
        checkpoint_path=checkpoint_path,
        manifest_path=output_dir
        / f"edocs_manifest_parallel_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
        checkpoint_every=checkpoint_every,
        existing_export=output_dir / "patients_export_10d.csv",
    )

    pending = [
        j
        for j in jobs
        if _patient_key(j.patient.facility_id, j.patient.patient_id) not in state.processed
    ]
    if max_patients is not None:
        pending = pending[:max_patients]

    log.info(
        "Parallel download: %d pending / %d total, %d workers, pdf_sem=%d",
        len(pending),
        len(jobs),
        worker_count,
        config.max_concurrent_pdfs,
    )

    await bootstrap_shared_session(config)

    set_pdf_semaphore(asyncio.Semaphore(config.max_concurrent_pdfs))
    queue: asyncio.Queue[ParallelPatientJob | None] = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)
    for _ in range(worker_count):
        queue.put_nowait(None)

    tasks = [
        asyncio.create_task(
            _download_worker(
                i + 1,
                queue=queue,
                config=config,
                output_dir=output_dir,
                state=state,
                skip_existing=skip_existing,
                skip_edocs=skip_edocs,
                skip_chart_notes=skip_chart_notes,
            )
        )
        for i in range(worker_count)
    ]
    await asyncio.gather(*tasks)

    export_path = output_dir / "patients_export_10d.csv"
    await state.finalize(export_path, jobs)
    set_pdf_semaphore(None)
    log.info(
        "Parallel download complete: %d patients, manifest=%s, export=%s",
        state.total_done,
        state.manifest_path,
        export_path,
    )
