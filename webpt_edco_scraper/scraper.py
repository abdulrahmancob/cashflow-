import argparse
import asyncio
import csv
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    list_clinics,
    parse_patient_ext_doc_url,
    refresh_csrf,
    save_storage_state,
    switch_clinic,
)
from chart_notes_api import fetch_patient_chart_notes
from chart_notes_download import download_patient_chart_notes
from config import EDOCS_DIR, SCHEDULER_INDEX_URL, STORAGE_STATE_PATH, WebPTConfig
from edoc_api import list_patient_edocs
from edoc_download import download_patient_edocs
from edoc_ocr import (
    analyze_patient_file_contributions,
    build_edoc_inventory_row,
    collect_patient_pdf_paths,
    run_ocr_all,
    run_patient_ocr_validation,
)
from chart_notes_parse import export_daily_notes, run_validate_extraction
from export_utils import (
    EDOC_MANIFEST_FIELDNAMES,
    PATIENT_EXPORT_FIELDNAMES,
    PATIENT_RECENT_FIELDNAMES,
    aggregate_edoc_summary_from_manifest,
    aggregate_chart_notes_summary_from_manifest,
    build_patient_export_row,
    chart_note_manifest_row,
    edoc_manifest_row,
    empty_ocr_summary,
    summarize_chart_notes_downloads,
    summarize_edoc_downloads,
    write_status_guide,
)
from logging_config import get_logger, setup_logging
from patient_api import _patient_display_name, iter_all_patients
from patient_chart_api import chart_to_dict, fetch_patient_chart
from scheduler_api import (
    SchedulerPatient,
    extract_patients_from_events,
    fetch_scheduler_events,
    resolve_date_range,
)

log = get_logger("scraper")

PATIENT_EXT_DOC_URL_PATTERN = re.compile(
    r"patientExtDoc\.php\?", re.IGNORECASE
)


def _manifest_path(output_dir: Path, name: str) -> Path:
    return output_dir / name


def _write_manifest_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _append_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _patient_key(facility_id: str, patient_id: int) -> str:
    return f"{facility_id}:{patient_id}"


def _load_checkpoint(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {"completed_facilities": [], "processed_patient_ids": []}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {
        "completed_facilities": list(data.get("completed_facilities") or []),
        "processed_patient_ids": list(data.get("processed_patient_ids") or []),
    }


def _save_checkpoint(path: Path, checkpoint: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(checkpoint, fh, indent=2)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _load_edoc_manifest_rows(output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("edocs_manifest_*.csv")):
        rows.extend(_read_csv_rows(path))
    return rows


async def _process_patient_edocs(
    context,
    *,
    clinic,
    patient: SchedulerPatient,
    config: WebPTConfig,
    session,
    edocs_dir: Path,
    skip_existing: bool,
    skip_edocs: bool,
    skip_chart_notes: bool = False,
    chart_notes_only: bool = False,
    skip_ocr: bool = False,
    ocr_only: bool = False,
    expected_diagnosis: str = "",
    force_ocr: bool = False,
    page=None,
    parallel_pdfs: bool = False,
    page_lock=None,
    chart_notes_debug_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if skip_edocs and skip_chart_notes and not ocr_only:
        return (
            [],
            summarize_edoc_downloads(docs_count=0, results=None, processed=False),
            summarize_chart_notes_downloads(notes_count=0, results=None, processed=False),
            empty_ocr_summary(),
        )

    patient_dir = edocs_dir / str(patient.patient_id)
    existing_pdfs = collect_patient_pdf_paths(patient_dir)

    if ocr_only:
        if not existing_pdfs:
            return (
                [],
                summarize_edoc_downloads(docs_count=0, results=None, processed=True),
                summarize_chart_notes_downloads(notes_count=0, results=None, processed=True),
                empty_ocr_summary(error="no PDF files on disk"),
            )
        ocr_summary = run_patient_ocr_validation(
            existing_pdfs,
            expected_name=patient.patient_name,
            expected_id=str(patient.patient_id),
            expected_diagnosis=expected_diagnosis,
            patient_dir=patient_dir,
            dpi=config.ocr_dpi,
            tesseract_cmd=config.tesseract_cmd or None,
            force=force_ocr,
        )
        edoc_summary = summarize_edoc_downloads(
            docs_count=len(existing_pdfs),
            results=[{"downloaded": True, "skipped": True, "error": None}] * len(existing_pdfs),
            processed=True,
        )
        return [], edoc_summary, summarize_chart_notes_downloads(
            notes_count=0, results=None, processed=True
        ), ocr_summary

    manifest_rows: list[dict[str, Any]] = []
    edoc_summary = summarize_edoc_downloads(docs_count=0, results=None, processed=True)
    chart_notes_summary = summarize_chart_notes_downloads(
        notes_count=0, results=None, processed=False
    )

    edoc_results: list[dict[str, Any]] = []
    chart_note_results: list[dict[str, Any]] = []
    notes: list = []

    need_edocs = not skip_edocs and not chart_notes_only
    need_chart_notes = not skip_chart_notes and patient.case_id is not None
    case_id = patient.case_id

    if parallel_pdfs and (need_edocs or need_chart_notes):
        parallel_tasks: list[Any] = []
        task_kinds: list[str] = []
        if need_edocs:
            parallel_tasks.append(
                list_patient_edocs(
                    context,
                    patient_id=patient.patient_id,
                    case_id=patient.case_id,
                    config=config,
                    session=session,
                    include_all_cases=True,
                )
            )
            task_kinds.append("edocs")
        if need_chart_notes:
            parallel_tasks.append(
                fetch_patient_chart_notes(
                    context,
                    patient_id=patient.patient_id,
                    case_id=case_id,
                    page=page,
                    config=config,
                    page_lock=page_lock,
                    debug_dir=chart_notes_debug_dir,
                    timeout_ms=int(config.chart_timeout_sec * 1000),
                )
            )
            task_kinds.append("notes")
        parallel_results = await asyncio.gather(*parallel_tasks)
        docs: list[dict[str, Any]] = []
        for kind, res in zip(task_kinds, parallel_results):
            if kind == "edocs":
                docs = res
            else:
                notes = res
    else:
        docs = []
        if need_edocs:
            docs = await list_patient_edocs(
                context,
                patient_id=patient.patient_id,
                case_id=patient.case_id,
                config=config,
                session=session,
                include_all_cases=True,
            )
        if need_chart_notes and not parallel_pdfs:
            notes = await fetch_patient_chart_notes(
                context,
                patient_id=patient.patient_id,
                case_id=case_id,
                page=page,
                config=config,
                page_lock=page_lock,
                debug_dir=chart_notes_debug_dir,
                timeout_ms=int(config.chart_timeout_sec * 1000),
            )

    if need_edocs:
        if not docs:
            manifest_rows.append(
                edoc_manifest_row(
                    facility_id=clinic.facility_id,
                    facility_name=clinic.name,
                    patient_id=patient.patient_id,
                    patient_name=patient.patient_name,
                    status="no_docs",
                )
            )
            edoc_summary = summarize_edoc_downloads(
                docs_count=0, results=None, processed=True
            )
        else:
            edoc_results = await download_patient_edocs(
                context,
                docs=docs,
                patient_id=patient.patient_id,
                output_dir=edocs_dir,
                config=config,
                skip_existing=skip_existing,
                parallel_pdfs=parallel_pdfs,
            )
            for r in edoc_results:
                st = "skipped" if r.get("skipped") else ("ok" if r.get("downloaded") else "error")
                manifest_rows.append(
                    edoc_manifest_row(
                        facility_id=clinic.facility_id,
                        facility_name=clinic.name,
                        patient_id=patient.patient_id,
                        patient_name=patient.patient_name,
                        ext_doc_id=str(r.get("ext_doc_id") or ""),
                        filename=r.get("filename") or "",
                        status=st,
                        path=r.get("path") or "",
                        error=r.get("error") or "",
                    )
                )
            edoc_summary = summarize_edoc_downloads(
                docs_count=len(docs), results=edoc_results, processed=True
            )
    elif skip_edocs or chart_notes_only:
        edoc_summary = summarize_edoc_downloads(
            docs_count=0, results=None, processed=chart_notes_only
        )

    if not skip_chart_notes:
        if case_id is None:
            chart_notes_summary = summarize_chart_notes_downloads(
                notes_count=0, results=None, processed=True, no_case=True
            )
            log.warning(
                "Skipping chart notes for patient %s: no case_id from scheduler",
                patient.patient_id,
            )
        else:
            if not parallel_pdfs:
                notes = await fetch_patient_chart_notes(
                    context,
                    patient_id=patient.patient_id,
                    case_id=case_id,
                    page=page,
                    config=config,
                    page_lock=page_lock,
                    debug_dir=chart_notes_debug_dir,
                    timeout_ms=int(config.chart_timeout_sec * 1000),
                )
            log.info(
                "Patient %s case %s: %d chart note(s) found",
                patient.patient_id,
                case_id,
                len(notes),
            )
            if not notes:
                chart_notes_summary = summarize_chart_notes_downloads(
                    notes_count=0, results=None, processed=True
                )
            else:
                chart_note_results = await download_patient_chart_notes(
                    context,
                    notes=notes,
                    patient_id=patient.patient_id,
                    case_id=case_id,
                    output_dir=edocs_dir,
                    config=config,
                    facility_id=clinic.facility_id,
                    skip_existing=skip_existing,
                    parallel_pdfs=parallel_pdfs,
                )
                for r in chart_note_results:
                    st = "skipped" if r.get("skipped") else (
                        "ok" if r.get("downloaded") else "error"
                    )
                    manifest_rows.append(
                        chart_note_manifest_row(
                            facility_id=clinic.facility_id,
                            facility_name=clinic.name,
                            patient_id=patient.patient_id,
                            patient_name=patient.patient_name,
                            note_id=str(r.get("note_id") or ""),
                            filename=r.get("filename") or "",
                            status=st,
                            path=r.get("path") or "",
                            error=r.get("error") or "",
                        )
                    )
                chart_notes_summary = summarize_chart_notes_downloads(
                    notes_count=len(notes),
                    results=chart_note_results,
                    processed=True,
                )
    else:
        chart_notes_summary = summarize_chart_notes_downloads(
            notes_count=0, results=None, processed=False
        )

    ocr_summary = empty_ocr_summary()
    if not skip_ocr and config.ocr_enabled:
        pdf_paths = [
            Path(r["path"])
            for r in edoc_results + chart_note_results
            if r.get("path") and Path(r["path"]).exists()
        ]
        if not pdf_paths and patient_dir.exists():
            pdf_paths = collect_patient_pdf_paths(patient_dir)
        if pdf_paths:
            ocr_summary = run_patient_ocr_validation(
                pdf_paths,
                expected_name=patient.patient_name,
                expected_id=str(patient.patient_id),
                expected_diagnosis=expected_diagnosis,
                patient_dir=patient_dir,
                dpi=config.ocr_dpi,
                tesseract_cmd=config.tesseract_cmd or None,
                force=force_ocr,
            )
        else:
            ocr_summary = empty_ocr_summary(error="no PDF files available for OCR")

    return manifest_rows, edoc_summary, chart_notes_summary, ocr_summary


async def _run_with_browser(config: WebPTConfig, coro, *, fresh_login: bool = False):
    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(
                page, context, config, fresh_login=fresh_login
            )
            return await coro(page, context, session, config)
        finally:
            await save_storage_state(context)
            await context.browser.close()


async def cmd_login(config: WebPTConfig, *, fresh_login: bool = False) -> None:
    if fresh_login and STORAGE_STATE_PATH.exists():
        STORAGE_STATE_PATH.unlink()
        log.info("Deleted stale session file %s", STORAGE_STATE_PATH)

    async def _login(page, context, session, cfg):
        log.info("Login complete. CSRF token present: %s", bool(session.csrf_token))
        return session

    await _run_with_browser(config, _login, fresh_login=fresh_login)
    log.info("Session saved.")


async def cmd_download_patient(
    config: WebPTConfig,
    *,
    patient_id: int,
    case_id: int | None,
    output_dir: Path,
    include_all_cases: bool,
    skip_existing: bool,
    facility_id: str | None,
    skip_edocs: bool = False,
    skip_chart_notes: bool = False,
    chart_notes_only: bool = False,
) -> list[dict[str, Any]]:
    async def _work(page, context, session, cfg):
        if facility_id:
            await switch_clinic(
                page, company_id=cfg.company_id, facility_id=facility_id
            )
            session = await ensure_authenticated(page, context, cfg)

        results: list[dict[str, Any]] = []
        if not skip_edocs and not chart_notes_only:
            docs = await list_patient_edocs(
                context,
                patient_id=patient_id,
                case_id=case_id,
                config=cfg,
                session=session,
                include_all_cases=include_all_cases,
            )
            log.info("Patient %s: %d edoc(s) found", patient_id, len(docs))
            if docs:
                results.extend(
                    await download_patient_edocs(
                        context,
                        docs=docs,
                        patient_id=patient_id,
                        output_dir=output_dir,
                        config=cfg,
                        skip_existing=skip_existing,
                    )
                )

        if not skip_chart_notes and case_id is not None:
            notes = await fetch_patient_chart_notes(
                context,
                patient_id=patient_id,
                case_id=case_id,
                page=page,
                config=cfg,
                debug_dir=output_dir / "debug",
                timeout_ms=int(cfg.chart_timeout_sec * 1000),
            )
            log.info(
                "Patient %s case %s: %d chart note(s) found",
                patient_id,
                case_id,
                len(notes),
            )
            if notes:
                results.extend(
                    await download_patient_chart_notes(
                        context,
                        notes=notes,
                        patient_id=patient_id,
                        case_id=case_id,
                        output_dir=output_dir,
                        config=cfg,
                        facility_id=facility_id or "",
                        skip_existing=skip_existing,
                    )
                )
        elif not skip_chart_notes and case_id is None:
            log.warning(
                "Skipping chart notes for patient %s: pass --case-id",
                patient_id,
            )

        return results

    return await _run_with_browser(config, _work)


async def cmd_download_current_page(
    config: WebPTConfig,
    *,
    output_dir: Path,
    include_all_cases: bool,
    skip_existing: bool,
    wait_timeout_sec: float,
) -> list[dict[str, Any]]:
    async def _work(page, context, session, cfg):
        log.info(
            "Waiting up to %.0fs for patientExtDoc.php URL (navigate in browser)...",
            wait_timeout_sec,
        )
        deadline = time.monotonic() + wait_timeout_sec
        patient_id: int | None = None
        case_id: int | None = None

        while time.monotonic() < deadline:
            url = page.url
            if PATIENT_EXT_DOC_URL_PATTERN.search(url):
                parsed = parse_patient_ext_doc_url(url)
                if parsed:
                    patient_id, case_id = parsed
                    break
            await asyncio.sleep(0.5)

        if patient_id is None:
            raise RuntimeError(
                "Timed out waiting for patientExtDoc.php?ID=...&CaseID=... "
                f"(current URL: {page.url})"
            )

        log.info("Detected patient_id=%s case_id=%s", patient_id, case_id)
        docs = await list_patient_edocs(
            context,
            patient_id=patient_id,
            case_id=case_id,
            config=cfg,
            session=session,
            include_all_cases=include_all_cases,
        )
        log.info("Found %d edoc(s)", len(docs))
        return await download_patient_edocs(
            context,
            docs=docs,
            patient_id=patient_id,
            output_dir=output_dir,
            config=cfg,
            skip_existing=skip_existing,
        )

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            await page.goto(
                "https://app.webpt.com/dashboard.php",
                wait_until="domcontentloaded",
            )
            if not config.headless:
                log.info(
                    "Open a patient eDoc page: patientExtDoc.php?ID=...&CaseID=..."
                )
            results = await _work(page, context, session, config)
            await save_storage_state(context)
            return results
        finally:
            await context.browser.close()


async def cmd_download_batch(
    config: WebPTConfig,
    *,
    input_csv: Path,
    output_dir: Path,
    skip_existing: bool,
    facility_id: str | None,
) -> None:
    rows_in: list[dict[str, str]] = []
    with input_csv.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows_in.append(row)

    manifest: list[dict[str, Any]] = []
    fieldnames = [
        "facility_id",
        "patient_id",
        "patient_name",
        "ext_doc_id",
        "filename",
        "status",
        "path",
        "error",
    ]

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            if facility_id:
                await switch_clinic(
                    page, company_id=config.company_id, facility_id=facility_id
                )
                session = await ensure_authenticated(page, context, config)

            for row in rows_in:
                pid_raw = row.get("patient_id") or row.get("PatientID") or row.get("ID")
                if not pid_raw:
                    log.warning("Skipping row without patient_id: %s", row)
                    continue
                patient_id = int(pid_raw)
                case_raw = row.get("case_id") or row.get("CaseID")
                case_id = int(case_raw) if case_raw else None
                patient_name = row.get("patient_name") or row.get("name") or ""

                docs = await list_patient_edocs(
                    context,
                    patient_id=patient_id,
                    case_id=case_id,
                    config=config,
                    session=session,
                    include_all_cases=True,
                )
                if not docs:
                    manifest.append(
                        {
                            "facility_id": facility_id or row.get("facility_id", ""),
                            "patient_id": patient_id,
                            "patient_name": patient_name,
                            "ext_doc_id": "",
                            "filename": "",
                            "status": "no_docs",
                            "path": "",
                            "error": "",
                        }
                    )
                    continue

                results = await download_patient_edocs(
                    context,
                    docs=docs,
                    patient_id=patient_id,
                    output_dir=output_dir,
                    config=config,
                    skip_existing=skip_existing,
                )
                for r in results:
                    manifest.append(
                        {
                            "facility_id": facility_id or row.get("facility_id", ""),
                            "patient_id": patient_id,
                            "patient_name": patient_name,
                            "ext_doc_id": r.get("ext_doc_id", ""),
                            "filename": r.get("filename", ""),
                            "status": "skipped" if r.get("skipped") else (
                                "ok" if r.get("downloaded") else "error"
                            ),
                            "path": r.get("path", ""),
                            "error": r.get("error") or "",
                        }
                    )
            await save_storage_state(context)
        finally:
            await context.browser.close()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    manifest_path = _manifest_path(output_dir, f"batch_manifest_{ts}.csv")
    _write_manifest_rows(manifest_path, manifest, fieldnames)
    log.info("Wrote manifest: %s (%d rows)", manifest_path, len(manifest))


async def cmd_download_facility(
    config: WebPTConfig,
    *,
    facility_id: str,
    output_dir: Path,
    skip_existing: bool,
    patient_name: str,
    max_patients: int | None,
    checkpoint_every: int,
) -> None:
    manifest: list[dict[str, Any]] = []
    fieldnames = [
        "facility_id",
        "patient_id",
        "patient_name",
        "ext_doc_id",
        "filename",
        "status",
        "path",
        "error",
    ]
    processed = 0

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            await switch_clinic(
                page, company_id=config.company_id, facility_id=facility_id
            )
            session = await ensure_authenticated(page, context, config)

            async for patient in iter_all_patients(
                context,
                config=config,
                session=session,
                patient_name=patient_name,
                max_patients=max_patients,
            ):
                patient_id = int(patient["PatientID"])
                name = _patient_display_name(patient)
                docs = await list_patient_edocs(
                    context,
                    patient_id=patient_id,
                    case_id=None,
                    config=config,
                    session=session,
                    include_all_cases=True,
                )
                if not docs:
                    manifest.append(
                        {
                            "facility_id": facility_id,
                            "patient_id": patient_id,
                            "patient_name": name,
                            "ext_doc_id": "",
                            "filename": "",
                            "status": "no_docs",
                            "path": "",
                            "error": "",
                        }
                    )
                else:
                    results = await download_patient_edocs(
                        context,
                        docs=docs,
                        patient_id=patient_id,
                        output_dir=output_dir,
                        config=config,
                        skip_existing=skip_existing,
                    )
                    for r in results:
                        manifest.append(
                            {
                                "facility_id": facility_id,
                                "patient_id": patient_id,
                                "patient_name": name,
                                "ext_doc_id": r.get("ext_doc_id", ""),
                                "filename": r.get("filename", ""),
                                "status": "skipped" if r.get("skipped") else (
                                    "ok" if r.get("downloaded") else "error"
                                ),
                                "path": r.get("path", ""),
                                "error": r.get("error") or "",
                            }
                        )

                processed += 1
                if checkpoint_every > 0 and processed % checkpoint_every == 0:
                    ckpt = _manifest_path(
                        output_dir, f"checkpoint_{facility_id}_{processed:04d}.csv"
                    )
                    _write_manifest_rows(ckpt, manifest, fieldnames)
                    log.info("Checkpoint: %s (%d patients)", ckpt, processed)

            await save_storage_state(context)
        finally:
            await context.browser.close()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    manifest_path = _manifest_path(
        output_dir, f"facility_{facility_id}_manifest_{ts}.csv"
    )
    _write_manifest_rows(manifest_path, manifest, fieldnames)
    log.info(
        "Facility %s done: %d patients, manifest %s",
        facility_id,
        processed,
        manifest_path,
    )


def _append_unflushed_facility_rows(
    patients_csv: Path,
    patients_export_csv: Path,
    facility_export_rows: list[dict[str, Any]],
    flushed_count: int,
) -> int:
    new_rows = facility_export_rows[flushed_count:]
    if new_rows:
        _append_csv_rows(patients_csv, new_rows, PATIENT_RECENT_FIELDNAMES)
        _append_csv_rows(patients_export_csv, new_rows, PATIENT_EXPORT_FIELDNAMES)
    return len(facility_export_rows)


def _flush_export_checkpoint(
    *,
    checkpoint_path: Path,
    checkpoint: dict[str, list[str]],
    edoc_manifest: list[dict[str, Any]],
    edocs_manifest_path: Path,
    patients_csv: Path,
    patients_export_csv: Path,
    facility_export_rows: list[dict[str, Any]],
    facility_rows_flushed: int,
) -> int:
    _save_checkpoint(checkpoint_path, checkpoint)
    if edoc_manifest:
        _write_manifest_rows(
            edocs_manifest_path,
            edoc_manifest,
            EDOC_MANIFEST_FIELDNAMES,
        )
    return _append_unflushed_facility_rows(
        patients_csv,
        patients_export_csv,
        facility_export_rows,
        facility_rows_flushed,
    )


async def _maybe_refresh_session(
    page,
    context,
    session,
    *,
    total_patients: int,
    interval: int = 100,
) -> Any:
    if interval <= 0 or total_patients <= 0 or total_patients % interval != 0:
        return session
    log.info("Refreshing session after %d patients", total_patients)
    await save_storage_state(context)
    return await refresh_csrf(context, page)


async def cmd_export_recent_appointments(
    config: WebPTConfig,
    *,
    output_dir: Path,
    days: int,
    end_date: date | None,
    lookahead_days: int | None,
    facility_id: str | None,
    skip_edocs: bool,
    skip_chart: bool,
    skip_chart_notes: bool,
    chart_notes_only: bool,
    skip_existing: bool,
    skip_ocr: bool,
    ocr_only: bool,
    max_patients: int | None,
    checkpoint_every: int,
) -> None:
    if ocr_only:
        skip_edocs = True
    if chart_notes_only:
        skip_edocs = True
    if not skip_ocr and config.ocr_enabled and skip_chart:
        log.warning(
            "OCR diagnosis validation requires chart data; enabling chart fetch"
        )
        skip_chart = False
    start_date, range_end, reference_date = resolve_date_range(
        days=days,
        end_date=end_date,
        timezone=config.timezone,
        lookahead_days=lookahead_days,
    )
    look = lookahead_days if lookahead_days is not None else days
    output_dir.mkdir(parents=True, exist_ok=True)
    write_status_guide(output_dir)
    edocs_dir = output_dir / "edocs"
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    patients_csv = output_dir / f"patients_recent_{days}d.csv"
    patients_export_csv = output_dir / f"patients_export_{days}d.csv"
    edocs_manifest_path = output_dir / f"edocs_manifest_{ts}.csv"

    edoc_manifest: list[dict[str, Any]] = []
    export_rows: list[dict[str, Any]] = []
    total_patients = 0
    patients_since_checkpoint = 0

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            clinics = await list_clinics(page, config.company_id)
            if facility_id:
                clinics = [c for c in clinics if c.facility_id == facility_id]
                if not clinics:
                    raise RuntimeError(
                        f"Facility {facility_id} not found for company {config.company_id}"
                    )

            log.info(
                "Export window %s..%s (past %d days, lookahead %d), ref=%s, %d clinic(s)",
                start_date,
                range_end,
                days,
                look,
                reference_date,
                len(clinics),
            )

            for clinic in clinics:
                if clinic.facility_id in checkpoint["completed_facilities"]:
                    log.info(
                        "Skipping completed facility %s (%s)",
                        clinic.facility_id,
                        clinic.name,
                    )
                    continue

                await switch_clinic(
                    page,
                    company_id=clinic.company_id,
                    facility_id=clinic.facility_id,
                )
                await page.goto(
                    SCHEDULER_INDEX_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                session = await ensure_authenticated(page, context, config)

                events: list[dict[str, Any]] | None = None
                for attempt in range(2):
                    try:
                        events = await fetch_scheduler_events(
                            context,
                            facility_id=clinic.facility_id,
                            start_date=start_date,
                            end_date=range_end,
                            session=session,
                            config=config,
                        )
                        break
                    except RuntimeError as exc:
                        if "401" not in str(exc):
                            raise
                        if attempt == 0:
                            log.warning(
                                "Scheduler 401 for facility %s — re-auth and retry",
                                clinic.facility_id,
                            )
                            session = await ensure_authenticated(
                                page, context, config
                            )
                            await page.goto(
                                SCHEDULER_INDEX_URL,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                        else:
                            log.error(
                                "Skipping facility %s (%s): scheduler access denied",
                                clinic.facility_id,
                                clinic.name,
                            )

                if events is None:
                    continue

                if not events:
                    log.warning(
                        "Scheduler returned 0 events for facility %s — skipping "
                        "(will retry on next run)",
                        clinic.facility_id,
                    )
                    continue

                patients = extract_patients_from_events(
                    events,
                    facility_id=clinic.facility_id,
                    reference_date=reference_date,
                )
                log.info(
                    "Facility %s (%s): %d unique patient(s) with appointments",
                    clinic.facility_id,
                    clinic.name,
                    len(patients),
                )

                facility_export_rows: list[dict[str, Any]] = []
                facility_rows_flushed = 0

                for patient in patients:
                    if max_patients is not None and total_patients >= max_patients:
                        log.info("Reached --max-patients %d", max_patients)
                        break

                    key = _patient_key(clinic.facility_id, patient.patient_id)
                    already_done = key in checkpoint["processed_patient_ids"]

                    try:
                        chart_fields: dict[str, str] = {}
                        ocr_summary = empty_ocr_summary()
                        chart_notes_summary = summarize_chart_notes_downloads(
                            notes_count=0, results=None, processed=False
                        )
                        if not skip_chart:
                            case_raw = patient.case_id
                            if case_raw:
                                chart = await fetch_patient_chart(
                                    context,
                                    patient_id=patient.patient_id,
                                    case_id=case_raw,
                                    page=page,
                                    timeout_ms=int(config.chart_timeout_sec * 1000),
                                )
                                chart_fields = chart_to_dict(chart)

                        if already_done and not ocr_only:
                            manifest_rows = _load_edoc_manifest_rows(output_dir)
                            edoc_summary = aggregate_edoc_summary_from_manifest(
                                manifest_rows,
                                patient_id=patient.patient_id,
                                facility_id=clinic.facility_id,
                            )
                            if edoc_summary["edoc_status"] == "pending":
                                edoc_summary = summarize_edoc_downloads(
                                    docs_count=0, results=None, processed=False
                                )
                            chart_notes_summary = aggregate_chart_notes_summary_from_manifest(
                                manifest_rows,
                                patient_id=patient.patient_id,
                                facility_id=clinic.facility_id,
                            )
                            if not skip_ocr and config.ocr_enabled:
                                patient_dir = edocs_dir / str(patient.patient_id)
                                pdf_paths = collect_patient_pdf_paths(patient_dir)
                                if pdf_paths:
                                    ocr_summary = run_patient_ocr_validation(
                                        pdf_paths,
                                        expected_name=patient.patient_name,
                                        expected_id=str(patient.patient_id),
                                        expected_diagnosis=chart_fields.get("diagnosis", ""),
                                        patient_dir=patient_dir,
                                        dpi=config.ocr_dpi,
                                        tesseract_cmd=config.tesseract_cmd or None,
                                    )
                        else:
                            edoc_rows, edoc_summary, chart_notes_summary, ocr_summary = (
                                await _process_patient_edocs(
                                    context,
                                    clinic=clinic,
                                    patient=patient,
                                    config=config,
                                    session=session,
                                    edocs_dir=edocs_dir,
                                    skip_existing=skip_existing,
                                    skip_edocs=skip_edocs,
                                    skip_chart_notes=skip_chart_notes,
                                    chart_notes_only=chart_notes_only,
                                    skip_ocr=skip_ocr,
                                    ocr_only=ocr_only,
                                    expected_diagnosis=chart_fields.get("diagnosis", ""),
                                    page=page,
                                )
                            )
                            if edoc_rows:
                                edoc_manifest.extend(edoc_rows)
                            if not already_done:
                                checkpoint["processed_patient_ids"].append(key)
                                total_patients += 1
                                patients_since_checkpoint += 1

                            session = await _maybe_refresh_session(
                                page,
                                context,
                                session,
                                total_patients=total_patients,
                            )

                        if config.action_delay_sec > 0 and (
                            not skip_chart or not skip_edocs or not skip_chart_notes
                        ):
                            await asyncio.sleep(config.action_delay_sec)

                        if (
                            not already_done
                            and checkpoint_every > 0
                            and patients_since_checkpoint >= checkpoint_every
                        ):
                            facility_rows_flushed = _flush_export_checkpoint(
                                checkpoint_path=checkpoint_path,
                                checkpoint=checkpoint,
                                edoc_manifest=edoc_manifest,
                                edocs_manifest_path=edocs_manifest_path,
                                patients_csv=patients_csv,
                                patients_export_csv=patients_export_csv,
                                facility_export_rows=facility_export_rows,
                                facility_rows_flushed=facility_rows_flushed,
                            )
                            log.info(
                                "Checkpoint saved (%d patients processed this run)",
                                total_patients,
                            )
                            patients_since_checkpoint = 0

                        facility_export_rows.append(
                            build_patient_export_row(
                                clinic_name=clinic.name,
                                patient=patient,
                                chart_fields=chart_fields,
                                edoc_summary=edoc_summary,
                                chart_notes_summary=chart_notes_summary,
                                ocr_summary=ocr_summary,
                            )
                        )
                    except Exception as exc:
                        log.error(
                            "Failed patient %s (%s): %s",
                            patient.patient_id,
                            key,
                            exc,
                        )
                        facility_rows_flushed = _flush_export_checkpoint(
                            checkpoint_path=checkpoint_path,
                            checkpoint=checkpoint,
                            edoc_manifest=edoc_manifest,
                            edocs_manifest_path=edocs_manifest_path,
                            patients_csv=patients_csv,
                            patients_export_csv=patients_export_csv,
                            facility_export_rows=facility_export_rows,
                            facility_rows_flushed=facility_rows_flushed,
                        )
                        raise

                if max_patients is not None and total_patients >= max_patients:
                    export_rows.extend(facility_export_rows)
                    facility_rows_flushed = _append_unflushed_facility_rows(
                        patients_csv,
                        patients_export_csv,
                        facility_export_rows,
                        facility_rows_flushed,
                    )
                    break

                export_rows.extend(facility_export_rows)
                facility_rows_flushed = _append_unflushed_facility_rows(
                    patients_csv,
                    patients_export_csv,
                    facility_export_rows,
                    facility_rows_flushed,
                )

                checkpoint["completed_facilities"].append(clinic.facility_id)
                _save_checkpoint(checkpoint_path, checkpoint)

            await save_storage_state(context)
        finally:
            await context.browser.close()

    _save_checkpoint(checkpoint_path, checkpoint)

    if edoc_manifest:
        _write_manifest_rows(edocs_manifest_path, edoc_manifest, EDOC_MANIFEST_FIELDNAMES)
        log.info("Wrote edocs manifest: %s (%d rows)", edocs_manifest_path, len(edoc_manifest))
    elif skip_edocs:
        log.info("Skipped eDoc downloads (--skip-edocs)")

    if patients_export_csv.exists():
        log.info("Patients export CSV: %s", patients_export_csv)
    elif patients_csv.exists():
        log.info("Patients CSV: %s", patients_csv)
    log.info(
        "Export complete: %d patient(s) processed, checkpoint %s",
        total_patients,
        checkpoint_path,
    )


def _print_file_contribution_table(contributions: list[dict[str, Any]]) -> None:
    if not contributions:
        return
    print("\n=== Per-File OCR Contribution ===")
    print(
        f"{'File':<40} {'Last':<6} {'First':<6} {'EMR ID':<8} "
        f"{'ICD':<20} {'Expected ICD':<14} {'Chars':<8}"
    )
    print("-" * 110)
    for row in contributions:
        print(
            f"{row.get('filename', ''):<40} "
            f"{row.get('has_last_name', ''):<6} "
            f"{row.get('has_first_name', ''):<6} "
            f"{row.get('has_emr_id', ''):<8} "
            f"{(row.get('icd_codes') or '')[:20]:<20} "
            f"{row.get('has_expected_icd', ''):<14} "
            f"{row.get('ocr_chars', 0):<8}"
        )
        if row.get("error"):
            print(f"  error: {row['error']}")


def cmd_ocr_test_patient(
    config: WebPTConfig,
    *,
    patient_id: int,
    edocs_dir: Path,
    expected_name: str = "",
    expected_id: str = "",
    expected_diagnosis: str = "",
    force: bool = False,
) -> None:
    patient_dir = edocs_dir / str(patient_id)
    if not patient_dir.exists():
        raise RuntimeError(f"Patient eDoc folder not found: {patient_dir}")

    pdf_paths = collect_patient_pdf_paths(patient_dir)
    if not pdf_paths:
        raise RuntimeError(f"No PDF files in {patient_dir} or chart_notes/")

    exp_name = expected_name or "Acosta, Amy"
    exp_id = expected_id or str(patient_id)
    exp_diagnosis = expected_diagnosis or (
        "ICD10: N39.3: Stress incontinence (female) (male), "
        "R35.0: Frequency of micturition, N39.41: Urge incontinence"
    )

    log.info("OCR test for patient %s (%d PDFs in %s)", patient_id, len(pdf_paths), patient_dir)
    for pdf in pdf_paths:
        log.info("  - %s (%d bytes)", pdf.name, pdf.stat().st_size)

    summary = run_patient_ocr_validation(
        pdf_paths,
        expected_name=exp_name,
        expected_id=exp_id,
        expected_diagnosis=exp_diagnosis,
        patient_dir=patient_dir,
        dpi=config.ocr_dpi,
        tesseract_cmd=config.tesseract_cmd or None,
        force=force,
    )

    contributions = summary.pop("_file_contributions", [])
    if not contributions:
        contributions = analyze_patient_file_contributions(
            pdf_paths,
            expected_name=exp_name,
            expected_id=exp_id,
            expected_diagnosis=exp_diagnosis,
            dpi=config.ocr_dpi,
            tesseract_cmd=config.tesseract_cmd or None,
        )
    _print_file_contribution_table(contributions)

    print("\n=== OCR Test Results ===")
    print(f"Patient ID: {patient_id}")
    print(f"Expected name: {exp_name}")
    print(f"Expected ID: {exp_id}")
    print(f"Expected diagnosis: {exp_diagnosis}")
    print()
    for key in (
        "edoc_ocr_name",
        "edoc_ocr_name_match",
        "edoc_ocr_patient_id",
        "edoc_ocr_id_match",
        "edoc_ocr_diagnosis",
        "edoc_ocr_diagnosis_match",
        "edoc_ocr_source_files",
        "edoc_ocr_file_hints",
        "edoc_ocr_errors",
    ):
        print(f"{key}: {summary.get(key, '')}")


def cmd_edocs_inventory(
    *,
    edocs_dir: Path,
    output_csv: Path,
) -> None:
    if not edocs_dir.exists():
        raise RuntimeError(f"eDocs directory not found: {edocs_dir}")

    rows: list[dict[str, Any]] = []
    for patient_dir in sorted(edocs_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        pdfs = collect_patient_pdf_paths(patient_dir)
        if not pdfs:
            continue
        rows.append(build_edoc_inventory_row(patient_dir.name, pdfs))

    fieldnames = [
        "patient_id",
        "file_count",
        "filenames",
        "has_intake",
        "has_referral",
        "has_insurance_id",
        "has_mri",
        "has_chart_note",
    ]
    _write_manifest_rows(output_csv, rows, fieldnames)
    log.info("Wrote eDocs inventory: %s (%d patients)", output_csv, len(rows))


def cmd_ocr_all(
    config: WebPTConfig,
    *,
    edocs_dir: Path,
    output_dir: Path,
    force: bool = False,
    force_ocr: bool = False,
    max_patients: int | None = None,
    extract_structured: bool = True,
    include_referral_icd: bool = True,
) -> None:
    """OCR all eDocs + chart_notes PDFs; optionally export daily_notes/cpt CSVs."""
    summary = run_ocr_all(
        edocs_dir,
        output_dir,
        dpi=config.ocr_dpi,
        tesseract_cmd=config.tesseract_cmd or None,
        force=force,
        force_ocr=force_ocr,
        max_patients=max_patients,
    )
    log.info(
        "ocr-all: %d patients, %d files -> %s",
        summary["patients_processed"],
        summary["files_processed"],
        summary["ocr_all_files_path"],
    )
    if summary["errors"]:
        log.warning("OCR errors (first 5): %s", " | ".join(summary["errors"][:5]))

    if extract_structured:
        dn_summary = export_daily_notes(
            edocs_dir,
            output_dir,
            include_referral_icd=include_referral_icd,
            tesseract_cmd=config.tesseract_cmd or None,
            ocr_dpi=config.ocr_dpi,
        )
        log.info(
            "Structured export: %d daily notes, %d CPT lines",
            dn_summary["daily_notes_count"],
            dn_summary["cpt_lines_count"],
        )


def cmd_validate_extraction(
    *,
    edocs_dir: Path,
    extracted_dir: Path,
) -> None:
    summary = run_validate_extraction(edocs_dir, extracted_dir)
    log.info(
        "validate-extraction: disk %d files (%d patients), ocr csv %d -> %s",
        summary["disk_files"],
        summary["disk_patients"],
        summary["ocr_csv_files"],
        summary["validation_report_path"],
    )
    for status, count in sorted(summary["status_counts"].items()):
        log.info("  status %s: %d", status, count)


def cmd_ocr_batch_test(
    config: WebPTConfig,
    *,
    edocs_dir: Path,
    patients_csv: Path,
    output_csv: Path,
    max_patients: int | None = None,
    force: bool = False,
) -> None:
    if not edocs_dir.exists():
        raise RuntimeError(f"eDocs directory not found: {edocs_dir}")

    patient_lookup: dict[str, dict[str, str]] = {}
    if patients_csv.exists():
        for row in _read_csv_rows(patients_csv):
            pid = str(row.get("patient_id") or row.get("PatientID") or "").strip()
            if pid:
                patient_lookup[pid] = row

    report_rows: list[dict[str, Any]] = []
    processed = 0

    for patient_dir in sorted(edocs_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        pdf_paths = collect_patient_pdf_paths(patient_dir)
        if not pdf_paths:
            continue
        if max_patients is not None and processed >= max_patients:
            break

        pid = patient_dir.name
        meta = patient_lookup.get(pid, {})
        exp_name = meta.get("patient_name") or ""
        exp_id = pid
        exp_diagnosis = meta.get("diagnosis") or ""

        log.info(
            "OCR batch [%d%s] patient %s (%d PDFs)",
            processed + 1,
            f"/{max_patients}" if max_patients else "",
            pid,
            len(pdf_paths),
        )

        summary = run_patient_ocr_validation(
            pdf_paths,
            expected_name=exp_name,
            expected_id=exp_id,
            expected_diagnosis=exp_diagnosis,
            patient_dir=patient_dir,
            dpi=config.ocr_dpi,
            tesseract_cmd=config.tesseract_cmd or None,
            force=force,
        )
        summary.pop("_file_contributions", None)
        inventory = build_edoc_inventory_row(pid, pdf_paths)

        report_rows.append(
            {
                "patient_id": pid,
                "patient_name": exp_name,
                "file_count": inventory["file_count"],
                "filenames": inventory["filenames"],
                "has_intake": inventory["has_intake"],
                "has_referral": inventory["has_referral"],
                "has_insurance_id": inventory["has_insurance_id"],
                "has_mri": inventory["has_mri"],
                "diagnosis_expected": exp_diagnosis,
                **{k: summary.get(k, "") for k in (
                    "edoc_ocr_name",
                    "edoc_ocr_name_match",
                    "edoc_ocr_patient_id",
                    "edoc_ocr_id_match",
                    "edoc_ocr_diagnosis",
                    "edoc_ocr_diagnosis_match",
                    "edoc_ocr_source_files",
                    "edoc_ocr_file_hints",
                    "edoc_ocr_errors",
                )},
            }
        )
        processed += 1

    fieldnames = [
        "patient_id",
        "patient_name",
        "file_count",
        "filenames",
        "has_intake",
        "has_referral",
        "has_insurance_id",
        "has_mri",
        "diagnosis_expected",
        "edoc_ocr_name",
        "edoc_ocr_name_match",
        "edoc_ocr_patient_id",
        "edoc_ocr_id_match",
        "edoc_ocr_diagnosis",
        "edoc_ocr_diagnosis_match",
        "edoc_ocr_source_files",
        "edoc_ocr_file_hints",
        "edoc_ocr_errors",
    ]
    _write_manifest_rows(output_csv, report_rows, fieldnames)
    log.info("Wrote OCR batch report: %s (%d patients)", output_csv, len(report_rows))


async def cmd_enrich_patient_export(
    config: WebPTConfig,
    *,
    input_csv: Path,
    output_dir: Path,
    output_csv: Path | None,
    skip_chart: bool,
    manifest_dir: Path | None,
    max_patients: int | None,
) -> None:
    rows_in = _read_csv_rows(input_csv)
    if not rows_in:
        log.warning("No rows in %s", input_csv)
        return

    manifest_source = manifest_dir or output_dir
    manifest_rows = _load_edoc_manifest_rows(manifest_source)
    out_path = output_csv or output_dir / "patients_export_10d.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_status_guide(output_dir)

    enriched: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            await ensure_authenticated(page, context, config)

            for row in rows_in:
                if max_patients is not None and len(enriched) >= max_patients:
                    break
                pid = int(row.get("patient_id") or row.get("PatientID") or 0)
                fid = str(row.get("facility_id") or "")
                case_raw = row.get("case_id") or row.get("CaseID") or ""
                case_id = int(case_raw) if str(case_raw).strip() else None

                patient = SchedulerPatient(
                    patient_id=pid,
                    facility_id=int(fid) if fid else 0,
                    case_id=case_id,
                    patient_name=row.get("patient_name") or "",
                    dob=row.get("dob") or "",
                    ins_name=row.get("ins_name") or "",
                    appointment_count=int(row.get("appointment_count") or 0),
                    appointment_dates=[
                        d.strip()
                        for d in (row.get("appointment_dates") or "").split(";")
                        if d.strip()
                    ],
                    appointments_past_count=int(
                        row.get("appointments_past_count")
                        or row.get("appointment_count")
                        or 0
                    ),
                    appointments_past_dates=[
                        d.strip()
                        for d in (
                            row.get("appointments_past_dates")
                            or row.get("appointment_dates")
                            or ""
                        ).split(";")
                        if d.strip()
                    ],
                    appointments_upcoming_count=int(
                        row.get("appointments_upcoming_count") or 0
                    ),
                    appointments_upcoming_dates=[
                        d.strip()
                        for d in (row.get("appointments_upcoming_dates") or "").split(
                            ";"
                        )
                        if d.strip()
                    ],
                )

                chart_fields: dict[str, str] = {}
                if not skip_chart and case_id:
                    chart = await fetch_patient_chart(
                        context,
                        patient_id=pid,
                        case_id=case_id,
                        page=page,
                        timeout_ms=int(config.chart_timeout_sec * 1000),
                    )
                    chart_fields = chart_to_dict(chart)
                elif not skip_chart:
                    for k in (
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
                    ):
                        if row.get(k):
                            chart_fields[k] = row[k]

                edoc_summary = aggregate_edoc_summary_from_manifest(
                    manifest_rows,
                    patient_id=pid,
                    facility_id=fid,
                )
                if edoc_summary["edoc_status"] == "pending" and row.get("edoc_status"):
                    edoc_summary = {
                        "edoc_status": row.get("edoc_status", ""),
                        "edoc_files_total": int(row.get("edoc_files_total") or 0),
                        "edoc_files_downloaded": int(
                            row.get("edoc_files_downloaded") or 0
                        ),
                        "edoc_files_skipped": int(row.get("edoc_files_skipped") or 0),
                        "edoc_files_failed": int(row.get("edoc_files_failed") or 0),
                        "edoc_errors": row.get("edoc_errors") or "",
                    }

                chart_notes_summary = aggregate_chart_notes_summary_from_manifest(
                    manifest_rows,
                    patient_id=pid,
                    facility_id=fid,
                )
                if chart_notes_summary["chart_notes_status"] == "pending" and row.get(
                    "chart_notes_status"
                ):
                    chart_notes_summary = {
                        "chart_notes_status": row.get("chart_notes_status", ""),
                        "chart_notes_total": int(row.get("chart_notes_total") or 0),
                        "chart_notes_downloaded": int(
                            row.get("chart_notes_downloaded") or 0
                        ),
                        "chart_notes_skipped": int(
                            row.get("chart_notes_skipped") or 0
                        ),
                        "chart_notes_failed": int(
                            row.get("chart_notes_failed") or 0
                        ),
                        "chart_notes_errors": row.get("chart_notes_errors") or "",
                    }

                enriched.append(
                    build_patient_export_row(
                        clinic_name=row.get("facility_name") or "",
                        patient=patient,
                        chart_fields=chart_fields,
                        edoc_summary=edoc_summary,
                        chart_notes_summary=chart_notes_summary,
                    )
                )
                if config.action_delay_sec > 0:
                    await asyncio.sleep(config.action_delay_sec)

            await save_storage_state(context)
        finally:
            await context.browser.close()

    _write_manifest_rows(out_path, enriched, PATIENT_EXPORT_FIELDNAMES)
    log.info("Wrote enriched export: %s (%d rows)", out_path, len(enriched))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WebPT eDoc downloader")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show browser window (overrides WEBPT_HEADLESS)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Log in and save storage_state.json")
    p_login.add_argument(
        "--fresh-login",
        action="store_true",
        help="Delete saved storage_state.json before logging in",
    )

    p_current = sub.add_parser(
        "download-current-page",
        help="Wait for patientExtDoc.php in browser, then download edocs",
    )
    p_current.add_argument("--output", type=Path, default=EDOCS_DIR)
    p_current.add_argument("--include-all-cases", action="store_true")
    p_current.add_argument("--no-skip-existing", action="store_true")
    p_current.add_argument("--wait-timeout", type=float, default=300.0)

    p_patient = sub.add_parser("download-patient", help="Download edocs for one patient")
    p_patient.add_argument("--patient-id", type=int, required=True)
    p_patient.add_argument("--case-id", type=int, default=None)
    p_patient.add_argument("--facility-id", type=str, default=None)
    p_patient.add_argument("--output", type=Path, default=EDOCS_DIR)
    p_patient.add_argument("--include-all-cases", action="store_true", default=True)
    p_patient.add_argument("--no-skip-existing", action="store_true")
    p_patient.add_argument("--skip-edocs", action="store_true")
    p_patient.add_argument("--skip-chart-notes", action="store_true")
    p_patient.add_argument(
        "--chart-notes-only",
        action="store_true",
        help="Download chart notes only (requires --case-id)",
    )

    p_batch = sub.add_parser("download-batch", help="Download from CSV of patient IDs")
    p_batch.add_argument("--input", type=Path, required=True)
    p_batch.add_argument("--facility-id", type=str, default=None)
    p_batch.add_argument("--output", type=Path, default=EDOCS_DIR)
    p_batch.add_argument("--no-skip-existing", action="store_true")

    p_facility = sub.add_parser(
        "download-facility",
        help="Paginate getpatients for a facility and download all edocs",
    )
    p_facility.add_argument("--facility-id", type=str, required=True)
    p_facility.add_argument("--output", type=Path, default=EDOCS_DIR)
    p_facility.add_argument("--patient-name", type=str, default="")
    p_facility.add_argument("--max-patients", type=int, default=None)
    p_facility.add_argument("--checkpoint-every", type=int, default=25)
    p_facility.add_argument("--no-skip-existing", action="store_true")

    p_recent = sub.add_parser(
        "export-recent-appointments",
        help="Export patients with scheduler appointments in the last N days",
    )
    p_recent.add_argument("--days", type=int, default=10)
    p_recent.add_argument("--lookahead-days", type=int, default=None)
    p_recent.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD")
    p_recent.add_argument("--facility-id", type=str, default=None)
    p_recent.add_argument("--output", type=Path, default=Path("output/recent_10d"))
    p_recent.add_argument("--skip-edocs", action="store_true")
    p_recent.add_argument("--skip-chart-notes", action="store_true")
    p_recent.add_argument(
        "--chart-notes-only",
        action="store_true",
        help="Download chart notes only (skip eDocs)",
    )
    p_recent.add_argument("--skip-chart", action="store_true")
    p_recent.add_argument("--skip-ocr", action="store_true")
    p_recent.add_argument("--ocr-only", action="store_true")
    p_recent.add_argument("--max-patients", type=int, default=None)
    p_recent.add_argument("--checkpoint-every", type=int, default=25)
    p_recent.add_argument("--no-skip-existing", action="store_true")

    p_ocr_test = sub.add_parser(
        "ocr-test-patient",
        help="Run OCR extraction/validation on downloaded eDocs for one patient",
    )
    p_ocr_test.add_argument("--patient-id", type=int, required=True)
    p_ocr_test.add_argument("--edocs-dir", type=Path, default=EDOCS_DIR)
    p_ocr_test.add_argument("--expected-name", type=str, default="")
    p_ocr_test.add_argument("--expected-id", type=str, default="")
    p_ocr_test.add_argument("--expected-diagnosis", type=str, default="")
    p_ocr_test.add_argument("--force", action="store_true", help="Ignore OCR cache")

    p_inventory = sub.add_parser(
        "edocs-inventory",
        help="List all downloaded PDFs per patient folder",
    )
    p_inventory.add_argument(
        "--edocs-dir",
        type=Path,
        default=Path("output/recent_10d/edocs"),
    )
    p_inventory.add_argument(
        "--output",
        type=Path,
        default=Path("output/recent_10d/edocs_inventory.csv"),
    )

    p_ocr_batch = sub.add_parser(
        "ocr-batch-test",
        help="Run OCR validation on many patients with local PDFs (offline)",
    )
    p_ocr_batch.add_argument(
        "--edocs-dir",
        type=Path,
        default=Path("output/recent_10d/edocs"),
    )
    p_ocr_batch.add_argument(
        "--patients-csv",
        type=Path,
        default=Path("output/recent_10d/patients_recent_10d.csv"),
    )
    p_ocr_batch.add_argument(
        "--output",
        type=Path,
        default=Path("output/recent_10d/ocr_batch_report.csv"),
    )
    p_ocr_batch.add_argument("--max-patients", type=int, default=20)
    p_ocr_batch.add_argument("--force", action="store_true", help="Ignore OCR cache")

    p_enrich = sub.add_parser(
        "enrich-patient-export",
        help="Enrich discovery CSV with chart fields and eDoc summary",
    )
    p_enrich.add_argument("--input", type=Path, required=True)
    p_enrich.add_argument("--output", type=Path, default=Path("output/recent_10d"))
    p_enrich.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Default: output/patients_export_10d.csv",
    )
    p_enrich.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="Directory with edocs_manifest_*.csv (default: --output)",
    )
    p_enrich.add_argument("--skip-chart", action="store_true")
    p_enrich.add_argument("--max-patients", type=int, default=None)

    p_parallel = sub.add_parser(
        "parallel-download",
        help="Phase 2: parallel PDF download from patients_recent CSV (single browser)",
    )
    p_parallel.add_argument(
        "--input",
        type=Path,
        required=True,
        help="patients_recent_10d.csv from discovery export",
    )
    p_parallel.add_argument("--output", type=Path, required=True)
    p_parallel.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Concurrent patient workers (default: WEBPT_PARALLEL_WORKERS)",
    )
    p_parallel.add_argument("--skip-edocs", action="store_true")
    p_parallel.add_argument("--skip-chart-notes", action="store_true")
    p_parallel.add_argument("--max-patients", type=int, default=None)
    p_parallel.add_argument("--checkpoint-every", type=int, default=25)
    p_parallel.add_argument("--no-skip-existing", action="store_true")

    p_extract_dn = sub.add_parser(
        "extract-daily-notes",
        help="Extract Daily Note billing headers and CPT lines from chart_notes PDFs",
    )
    p_extract_dn.add_argument(
        "--input",
        type=Path,
        required=True,
        help="edocs root (contains {patient_id}/chart_notes/)",
    )
    p_extract_dn.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for daily_notes.csv and cpt_codes.csv",
    )
    p_extract_dn.add_argument(
        "--include-referral-icd",
        action="store_true",
        help="OCR referral eDocs and write referral_icd.csv",
    )

    p_ocr_all = sub.add_parser(
        "ocr-all",
        help="OCR all PDFs (eDocs + chart_notes) and export structured daily note data",
    )
    p_ocr_all.add_argument(
        "--edocs-dir",
        type=Path,
        default=Path("output/recent_10d_fast_chartnotes/edocs"),
    )
    p_ocr_all.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/recent_10d_fast_chartnotes/extracted"),
    )
    p_ocr_all.add_argument(
        "--force",
        action="store_true",
        help="Re-run OCR and overwrite per-patient .ocr_cache.txt",
    )
    p_ocr_all.add_argument(
        "--force-ocr",
        action="store_true",
        help="Always OCR every page (skip native PDF text shortcut)",
    )
    p_ocr_all.add_argument("--max-patients", type=int, default=None)
    p_ocr_all.add_argument(
        "--skip-structured",
        action="store_true",
        help="Skip daily_notes.csv / cpt_codes.csv extraction",
    )
    p_ocr_all.add_argument(
        "--skip-referral-icd",
        action="store_true",
        help="Skip referral_icd.csv during structured export",
    )

    p_validate = sub.add_parser(
        "validate-extraction",
        help="Compare on-disk PDFs with extraction CSVs and write validation_report.csv",
    )
    p_validate.add_argument(
        "--edocs-dir",
        type=Path,
        default=Path("output/recent_10d_fast_chartnotes/edocs"),
    )
    p_validate.add_argument(
        "--extracted-dir",
        type=Path,
        default=Path("output/recent_10d_fast_chartnotes/extracted"),
    )

    return parser


async def async_main(args: argparse.Namespace) -> None:
    config = WebPTConfig.from_env()
    if args.no_headless:
        config.headless = False
    elif args.headless:
        config.headless = True

    skip_existing = not getattr(args, "no_skip_existing", False)

    if args.command == "login":
        await cmd_login(config, fresh_login=getattr(args, "fresh_login", False))
    elif args.command == "download-current-page":
        results = await cmd_download_current_page(
            config,
            output_dir=args.output,
            include_all_cases=args.include_all_cases,
            skip_existing=skip_existing,
            wait_timeout_sec=args.wait_timeout,
        )
        ok = sum(1 for r in results if r.get("downloaded"))
        log.info("Downloaded/skipped %d file(s)", ok)
    elif args.command == "download-patient":
        results = await cmd_download_patient(
            config,
            patient_id=args.patient_id,
            case_id=args.case_id,
            output_dir=args.output,
            include_all_cases=args.include_all_cases,
            skip_existing=skip_existing,
            facility_id=args.facility_id,
            skip_edocs=args.skip_edocs,
            skip_chart_notes=args.skip_chart_notes,
            chart_notes_only=args.chart_notes_only,
        )
        ok = sum(1 for r in results if r.get("downloaded"))
        log.info("Downloaded/skipped %d file(s) for patient %s", ok, args.patient_id)
    elif args.command == "download-batch":
        await cmd_download_batch(
            config,
            input_csv=args.input,
            output_dir=args.output,
            skip_existing=skip_existing,
            facility_id=args.facility_id,
        )
    elif args.command == "download-facility":
        await cmd_download_facility(
            config,
            facility_id=args.facility_id,
            output_dir=args.output,
            skip_existing=skip_existing,
            patient_name=args.patient_name,
            max_patients=args.max_patients,
            checkpoint_every=args.checkpoint_every,
        )
    elif args.command == "export-recent-appointments":
        end_date = None
        if args.end_date:
            end_date = date.fromisoformat(args.end_date)
        await cmd_export_recent_appointments(
            config,
            output_dir=args.output,
            days=args.days,
            end_date=end_date,
            lookahead_days=args.lookahead_days,
            facility_id=args.facility_id,
            skip_edocs=args.skip_edocs,
            skip_chart=args.skip_chart,
            skip_chart_notes=args.skip_chart_notes,
            chart_notes_only=args.chart_notes_only,
            skip_existing=skip_existing,
            skip_ocr=args.skip_ocr,
            ocr_only=args.ocr_only,
            max_patients=args.max_patients,
            checkpoint_every=args.checkpoint_every,
        )
    elif args.command == "ocr-test-patient":
        cmd_ocr_test_patient(
            config,
            patient_id=args.patient_id,
            edocs_dir=args.edocs_dir,
            expected_name=args.expected_name,
            expected_id=args.expected_id,
            expected_diagnosis=args.expected_diagnosis,
            force=args.force,
        )
    elif args.command == "edocs-inventory":
        cmd_edocs_inventory(
            edocs_dir=args.edocs_dir,
            output_csv=args.output,
        )
    elif args.command == "ocr-batch-test":
        cmd_ocr_batch_test(
            config,
            edocs_dir=args.edocs_dir,
            patients_csv=args.patients_csv,
            output_csv=args.output,
            max_patients=args.max_patients,
            force=args.force,
        )
    elif args.command == "enrich-patient-export":
        await cmd_enrich_patient_export(
            config,
            input_csv=args.input,
            output_dir=args.output,
            output_csv=args.output_csv,
            skip_chart=args.skip_chart,
            manifest_dir=args.manifest_dir,
            max_patients=args.max_patients,
        )
    elif args.command == "extract-daily-notes":
        summary = export_daily_notes(
            args.input,
            args.output_dir,
            include_referral_icd=args.include_referral_icd,
            tesseract_cmd=config.tesseract_cmd or None,
            ocr_dpi=config.ocr_dpi,
        )
        log.info(
            "extract-daily-notes: %d visits, %d CPT lines -> %s",
            summary["daily_notes_count"],
            summary["cpt_lines_count"],
            args.output_dir,
        )
        if summary["errors"]:
            log.warning("Errors: %s", " | ".join(summary["errors"][:5]))
    elif args.command == "ocr-all":
        cmd_ocr_all(
            config,
            edocs_dir=args.edocs_dir,
            output_dir=args.output_dir,
            force=args.force,
            force_ocr=args.force_ocr,
            max_patients=args.max_patients,
            extract_structured=not args.skip_structured,
            include_referral_icd=not args.skip_referral_icd,
        )
    elif args.command == "validate-extraction":
        cmd_validate_extraction(
            edocs_dir=args.edocs_dir,
            extracted_dir=args.extracted_dir,
        )
    elif args.command == "parallel-download":
        from parallel_download import run_parallel_download

        await run_parallel_download(
            config,
            input_csv=args.input,
            output_dir=args.output,
            workers=args.workers,
            skip_existing=skip_existing,
            skip_edocs=args.skip_edocs,
            skip_chart_notes=args.skip_chart_notes,
            checkpoint_every=args.checkpoint_every,
            max_patients=args.max_patients,
        )
    else:
        raise SystemExit(f"Unknown command: {args.command}")


def main() -> None:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
