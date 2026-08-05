"""Parallel patient PDF download from export CSV (Phase 2, single browser)."""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from auth import (
    ClinicInfo,
    ClinicSwitchError,
    SessionState,
    VEGA_JWT_MIN_TTL_SEC,
    create_context,
    ensure_authenticated,
    ensure_page_authenticated,
    ensure_session_fresh,
    safe_close_context,
    save_storage_state,
    switch_clinic_and_settle,
    vega_jwt_seconds_remaining,
)
from config import SCHEDULER_INDEX_URL, WebPTConfig
from export_utils import (
    EDOC_MANIFEST_FIELDNAMES,
    PATIENT_EXPORT_FIELDNAMES,
    build_patient_export_row,
    merge_pass_summary_fields,
    patients_export_filename_from_input,
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


@dataclass
class SharedBrowserPool:
    """One browser context + page shared by all worker coroutines."""

    context: BrowserContext
    page: Page
    session: SessionState
    config: WebPTConfig
    current_facility: str | None = None
    facility_refcount: int = 0
    facility_cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Optional SpeedController (clinic/jwt/csrf cache) — set by case drain worker.
    speed_controller: Any = None


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


def _load_edoc_pass_checkpoint(path: Path) -> set[str]:
    return _load_download_checkpoint(path)


def _save_edoc_pass_checkpoint(path: Path, done: set[str]) -> None:
    _save_download_checkpoint(path, done)


def _dedupe_jobs_by_patient(jobs: list[ParallelPatientJob]) -> list[ParallelPatientJob]:
    """Keep first CSV row per (facility_id, patient_id)."""
    seen: set[str] = set()
    out: list[ParallelPatientJob] = []
    for job in jobs:
        key = _patient_key(job.patient.facility_id, job.patient.patient_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def _load_export_processed_patient_ids(output_dir: Path) -> set[str]:
    """Reuse serial-export checkpoint so parallel-download skips already-done patients."""
    path = output_dir / "checkpoint.json"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data.get("processed_patient_ids") or [])


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
        edoc_pass_path: Path | None = None,
    ) -> None:
        self.checkpoint_path = checkpoint_path
        self.edoc_pass_path = edoc_pass_path
        self.manifest_path = manifest_path
        self.checkpoint_every = checkpoint_every
        self.lock = asyncio.Lock()
        self.processed: set[str] = _load_download_checkpoint(checkpoint_path)
        self.edoc_pass_done: set[str] = (
            _load_edoc_pass_checkpoint(edoc_pass_path) if edoc_pass_path else set()
        )
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
        mark_done: bool = True,
        mark_edoc_pass: bool = False,
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
            prior = self.export_rows.get(key)
            export_row = merge_pass_summary_fields(export_row, prior)
            # Prefer non-blank chart fields from prior when this pass did not refetch.
            if prior:
                for k, v in prior.items():
                    if k in job.chart_fields and not (export_row.get(k) or "") and v:
                        export_row[k] = v
            if mark_done:
                self.processed.add(key)
            if mark_edoc_pass:
                self.edoc_pass_done.add(key)
            self.export_rows[key] = export_row
            if manifest_rows:
                _append_manifest_rows(self.manifest_path, manifest_rows)
            self.since_flush += 1
            self.total_done += 1
            if self.checkpoint_every > 0 and self.since_flush >= self.checkpoint_every:
                _save_download_checkpoint(self.checkpoint_path, self.processed)
                if self.edoc_pass_path is not None:
                    _save_edoc_pass_checkpoint(
                        self.edoc_pass_path, self.edoc_pass_done
                    )
                self.since_flush = 0
                log.info("Parallel checkpoint saved (%d patients done)", self.total_done)

    async def finalize(self, export_path: Path, all_jobs: list[ParallelPatientJob]) -> None:
        async with self.lock:
            _save_download_checkpoint(self.checkpoint_path, self.processed)
            if self.edoc_pass_path is not None:
                _save_edoc_pass_checkpoint(self.edoc_pass_path, self.edoc_pass_done)
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


async def _facility_cache_put_async(
    pool: SharedBrowserPool, facility_id: str
) -> None:
    ctrl = pool.speed_controller
    if ctrl is None:
        return
    try:
        cookies = await pool.context.cookies()
        remaining = vega_jwt_seconds_remaining(cookies)
        ctrl.put_facility_cache(
            str(facility_id),
            jwt_remaining_sec=remaining,
            csrf=str(getattr(pool.session, "csrf_token", None) or ""),
            permissions="",
            ttl_sec=max(60.0, float(remaining or 0) - 30.0)
            if remaining is not None
            else 25 * 60,
        )
    except Exception:
        pass


async def acquire_facility(pool: SharedBrowserPool, facility_id: str) -> SessionState:
    """Lease shared session for a facility; parallel workers may share the same facility.

    Facility Cache: when already on clinic and cache (jwt/csrf/expires) is valid,
    skip refresh entirely.
    """
    async with pool.facility_cond:
        while (
            pool.facility_refcount > 0
            and pool.current_facility is not None
            and pool.current_facility != facility_id
        ):
            await pool.facility_cond.wait()

        if pool.current_facility != facility_id:
            async with pool.session_lock:
                async with pool.page_lock:
                    # Refresh JWT if needed; clinic switch + settle below.
                    pool.session = await ensure_session_fresh(
                        pool.page,
                        pool.context,
                        pool.config,
                        allow_oust=True,
                    )
                    try:
                        pool.session = await switch_clinic_and_settle(
                            pool.page,
                            pool.context,
                            pool.config,
                            company_id=str(pool.config.company_id),
                            facility_id=str(facility_id),
                            allow_oust=True,
                        )
                    except (ClinicSwitchError, TimeoutError) as switch_exc:
                        # Session often bounced to Auth0 mid-switch; re-login once.
                        log.warning(
                            "Clinic switch failed (%s); re-auth and retry",
                            switch_exc,
                        )
                        await ensure_authenticated(
                            pool.page,
                            pool.context,
                            pool.config,
                            allow_oust=True,
                            fresh_login=True,
                        )
                        pool.session = await switch_clinic_and_settle(
                            pool.page,
                            pool.context,
                            pool.config,
                            company_id=str(pool.config.company_id),
                            facility_id=str(facility_id),
                            allow_oust=True,
                        )
            pool.current_facility = facility_id
            await _facility_cache_put_async(pool, facility_id)
            log.info("Shared browser switched to facility %s", facility_id)
        else:
            # Same facility: Facility Cache short-circuit when still valid.
            cached = None
            ctrl = pool.speed_controller
            if ctrl is not None:
                try:
                    cached = ctrl.get_facility_cache(str(facility_id))
                except Exception:
                    cached = None
            if cached is not None and cached.is_valid():
                # Keep CSRF from cache if session missing it
                if not getattr(pool.session, "csrf_token", None) and cached.csrf:
                    try:
                        pool.session.csrf_token = cached.csrf
                    except Exception:
                        pass
            else:
                # Same facility: refresh JWT only when near expiry.
                cookies = await pool.context.cookies()
                remaining = vega_jwt_seconds_remaining(cookies)
                if remaining is None or remaining <= VEGA_JWT_MIN_TTL_SEC:
                    async with pool.session_lock:
                        async with pool.page_lock:
                            pool.session = await ensure_session_fresh(
                                pool.page,
                                pool.context,
                                pool.config,
                                facility_id=facility_id,
                                company_id=pool.config.company_id,
                                allow_oust=True,
                            )
                    await _facility_cache_put_async(pool, facility_id)
                else:
                    await _facility_cache_put_async(pool, facility_id)

        pool.facility_refcount += 1
        return pool.session


async def release_facility(pool: SharedBrowserPool, facility_id: str) -> None:
    async with pool.facility_cond:
        if pool.facility_refcount > 0:
            pool.facility_refcount -= 1
        pool.facility_cond.notify_all()


async def _download_worker(
    worker_id: int,
    *,
    queue: asyncio.Queue[ParallelPatientJob | None],
    pool: SharedBrowserPool,
    output_dir: Path,
    state: ParallelDownloadState,
    skip_existing: bool,
    skip_edocs: bool,
    skip_chart_notes: bool,
) -> None:
    from scraper import _process_patient_edocs

    while True:
        job = await queue.get()
        try:
            if job is None:
                break

            key = _patient_key(job.patient.facility_id, job.patient.patient_id)
            if key in state.processed or (
                skip_chart_notes and key in state.edoc_pass_done
            ):
                log.debug("Worker %d skip done %s", worker_id, key)
                continue

            fid = str(job.patient.facility_id)
            session = await acquire_facility(pool, fid)
            try:
                clinic = ClinicInfo(
                    company_id=pool.config.company_id,
                    facility_id=fid,
                    name=job.facility_name,
                )
                edocs_dir = output_dir / "edocs"
                manifest_rows, edoc_summary, chart_notes_summary, _ = (
                    await _process_patient_edocs(
                        pool.context,
                        clinic=clinic,
                        patient=job.patient,
                        config=pool.config,
                        session=session,
                        edocs_dir=edocs_dir,
                        skip_existing=skip_existing,
                        skip_edocs=skip_edocs,
                        skip_chart_notes=skip_chart_notes,
                        skip_ocr=True,
                        expected_diagnosis=job.diagnosis,
                        # Keep page for clinic switch + HTTP→page chart-notes fallback.
                        page=pool.page,
                        parallel_pdfs=True,
                        page_lock=pool.page_lock,
                        session_lock=pool.session_lock,
                        chart_notes_debug_dir=output_dir / "debug",
                        prefer_http_chart_notes=True,
                    )
                )
                edoc_ok = edoc_summary.get("edoc_status") in (
                    "complete",
                    "no_docs",
                )
                await state.record_success(
                    job,
                    manifest_rows=manifest_rows,
                    edoc_summary=edoc_summary,
                    chart_notes_summary=chart_notes_summary,
                    # Full done only when chart notes are included in this pass.
                    mark_done=not skip_chart_notes,
                    # Edocs-only pass: persist complete/no_docs so restarts skip
                    # them; leave partial/failed for retry. Chart-notes pass still
                    # sees them via download_checkpoint staying unmarked.
                    mark_edoc_pass=skip_chart_notes and not skip_edocs and edoc_ok,
                )
                log.info(
                    "Worker %d done %s (%s) edoc=%s chart_notes=%s",
                    worker_id,
                    job.patient.patient_id,
                    job.patient.patient_name,
                    edoc_summary.get("edoc_status"),
                    chart_notes_summary.get("chart_notes_status"),
                )
            finally:
                await release_facility(pool, fid)
        except Exception as exc:
            log.error(
                "Worker %d failed patient %s: %r",
                worker_id,
                job.patient.patient_id if job else "?",
                exc,
            )
        finally:
            queue.task_done()


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
    # WebPT single-session: one shared browser/tab. Multiple patient workers race
    # Auth0 and click "oust", killing the live session. Keep PDF parallelism only.
    if worker_count > 1:
        log.warning(
            "WebPT single-session requires serial patients; clamping --workers "
            "%d -> 1 (speed via WEBPT_MAX_CONCURRENT_PDFS=%d, not patient workers)",
            worker_count,
            config.max_concurrent_pdfs,
        )
        worker_count = 1

    jobs = load_jobs_from_csv(input_csv)
    if not jobs:
        raise RuntimeError(f"No patients in {input_csv}")
    jobs_raw_count = len(jobs)
    jobs = _dedupe_jobs_by_patient(jobs)
    if len(jobs) < jobs_raw_count:
        log.info(
            "Deduped CSV patients: %d rows -> %d unique facility:patient keys",
            jobs_raw_count,
            len(jobs),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    export_name = patients_export_filename_from_input(input_csv)
    export_path = output_dir / export_name
    # Prefer stem-matched export; fall back to legacy patients_export_10d.csv.
    existing_export = export_path if export_path.exists() else None
    legacy_export = output_dir / "patients_export_10d.csv"
    if existing_export is None and legacy_export.exists():
        existing_export = legacy_export
    checkpoint_path = output_dir / "download_checkpoint.json"
    edoc_pass_path = output_dir / "edoc_pass_checkpoint.json"
    state = ParallelDownloadState(
        checkpoint_path=checkpoint_path,
        edoc_pass_path=edoc_pass_path,
        manifest_path=output_dir
        / f"edocs_manifest_parallel_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv",
        checkpoint_every=checkpoint_every,
        existing_export=existing_export,
    )

    export_done = _load_export_processed_patient_ids(output_dir)
    if export_done:
        before = len(state.processed)
        state.processed |= export_done
        log.info(
            "Merged %d keys from checkpoint.json into parallel skip set (%d -> %d)",
            len(export_done),
            before,
            len(state.processed),
        )

    skip_keys = set(state.processed)
    if skip_chart_notes and not skip_edocs:
        skip_keys |= state.edoc_pass_done
        if state.edoc_pass_done:
            log.info(
                "Skipping %d patients already done in edocs-only pass",
                len(state.edoc_pass_done),
            )

    pending = [
        j
        for j in jobs
        if _patient_key(j.patient.facility_id, j.patient.patient_id) not in skip_keys
    ]
    if max_patients is not None:
        pending = pending[:max_patients]
    pending.sort(key=lambda j: (j.patient.facility_id, j.patient.patient_id))

    log.info(
        "Parallel download (single browser): %d pending / %d unique (%d csv rows), "
        "%d workers, pdf_sem=%d",
        len(pending),
        len(jobs),
        jobs_raw_count,
        worker_count,
        config.max_concurrent_pdfs,
    )

    set_pdf_semaphore(asyncio.Semaphore(config.max_concurrent_pdfs))
    queue: asyncio.Queue[ParallelPatientJob | None] = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)
    for _ in range(worker_count):
        queue.put_nowait(None)

    try:
        async with async_playwright() as playwright:
            context = await create_context(playwright, config)
            page = await context.new_page()
            try:
                # Single patient worker: allow "Yes, oust them!" so expired
                # sessions can be reclaimed mid-run.
                session = await ensure_authenticated(
                    page, context, config, allow_oust=True
                )
                pool = SharedBrowserPool(
                    context=context,
                    page=page,
                    session=session,
                    config=config,
                )

                tasks = [
                    asyncio.create_task(
                        _download_worker(
                            i + 1,
                            queue=queue,
                            pool=pool,
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

                await state.finalize(export_path, jobs)
                log.info("Wrote patient export: %s", export_path)
            finally:
                await save_storage_state(context)
                await safe_close_context(context)
    finally:
        set_pdf_semaphore(None)
    log.info(
        "Parallel download complete: %d patients, manifest=%s",
        state.total_done,
        state.manifest_path,
    )
