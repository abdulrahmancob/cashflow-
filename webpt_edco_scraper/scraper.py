import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from auth import (
    ClinicSwitchError,
    create_context,
    ensure_authenticated,
    ensure_session_fresh,
    list_clinics,
    parse_patient_ext_doc_url,
    refresh_csrf,
    restart_browser,
    safe_close_context,
    save_storage_state,
    switch_clinic,
    switch_clinic_and_settle,
)
from chart_notes_api import fetch_patient_chart_notes
from chart_notes_download import download_patient_chart_notes
from config import EDOCS_DIR, SCHEDULER_INDEX_URL, STORAGE_STATE_PATH, WebPTConfig
from edoc_api import list_patient_edocs
from edoc_download import download_patient_edocs, is_auth_expired_error
from edoc_ocr import (
    analyze_patient_file_contributions,
    build_edoc_inventory_row,
    collect_patient_pdf_paths,
    run_ocr_all,
    run_patient_ocr_validation,
)
from chart_notes_parse import (
    export_daily_notes,
    export_plans_of_care,
    run_validate_extraction,
)
from export_utils import (
    CHECKOUT_EXPORT_FIELDNAMES,
    EDOC_MANIFEST_FIELDNAMES,
    PATIENT_EXPORT_FIELDNAMES,
    PATIENT_RECENT_FIELDNAMES,
    SCHEDULE_EXPORT_FIELDNAMES,
    aggregate_edoc_summary_from_manifest,
    aggregate_chart_notes_summary_from_manifest,
    build_checkout_export_row,
    build_patient_export_row,
    chart_note_manifest_row,
    edoc_manifest_row,
    empty_ocr_summary,
    patients_export_filename_from_input,
    repair_patient_export_row,
    summarize_chart_notes_downloads,
    summarize_edoc_downloads,
    write_status_guide,
)
from http_utils import is_browser_connection_lost, is_transient_network_error
from logging_config import get_logger, setup_logging
from patient_api import _patient_display_name, iter_all_patients
from patient_chart_api import (
    FETCH_ERROR_DISPLAY_PATIENTS,
    FETCH_ERROR_LOGIN,
    chart_to_dict,
    fetch_patient_chart,
)
from scheduler_api import (
    SchedulerPatient,
    extract_checkout_visits,
    extract_patients_from_events,
    extract_schedule_visits,
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


_WEBPT_SESSION_LOCK = Path(__file__).resolve().parent / ".webpt_session.lock"
_webpt_lock_fh = None


def _other_webpt_scraper_pids() -> list[int]:
    """PIDs of other WebPT browser scrapers that would fight for the single login."""
    my_pid = os.getpid()
    parent_pid = os.getppid()
    markers = (
        "parallel-download",
        "enrich-patient-export",
        "export-recent-appointments",
        "export-checkouts",
        "export-schedule",
        "scrape-patient-payments",
        "login --fresh",
    )
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='python'\" | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            timeout=20,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("Could not check for concurrent WebPT scrapers: %s", exc)
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Could not parse process list for WebPT exclusivity check")
        return []
    if isinstance(data, dict):
        data = [data]
    pids: list[int] = []
    for row in data:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid in (0, my_pid, parent_pid):
            continue
        cmd = str(row.get("CommandLine") or "")
        if "scraper.py" not in cmd:
            continue
        if any(m in cmd for m in markers):
            pids.append(pid)
    return pids


def assert_exclusive_webpt_session() -> None:
    """Refuse to start if another scraper already holds the WebPT login.

    Uses an exclusive lock file (reliable on Windows with venv launchers) and
    also warns about other python scraper processes when detectable.
    """
    global _webpt_lock_fh
    holders = _other_webpt_scraper_pids()
    if holders:
        log.warning(
            "Other WebPT scraper process(es) detected (PIDs %s) — "
            "continuing only if lock is free",
            holders,
        )
    _WEBPT_SESSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(_WEBPT_SESSION_LOCK, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                fh.close()
                raise RuntimeError(
                    "Another WebPT scraper already holds "
                    f"{_WEBPT_SESSION_LOCK.name}. Finish one job at a time — "
                    "WebPT allows only one login."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                fh.close()
                raise RuntimeError(
                    "Another WebPT scraper already holds "
                    f"{_WEBPT_SESSION_LOCK.name}. Finish one job at a time — "
                    "WebPT allows only one login."
                ) from exc
    except Exception:
        raise
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _webpt_lock_fh = fh  # keep locked for process lifetime



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


def _index_manifest_rows_by_patient(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Split manifest rows into edoc vs chart_note lists keyed by facility:patient."""
    edoc_by_key: dict[str, list[dict[str, Any]]] = {}
    chart_by_key: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        fid = str(r.get("facility_id") or "")
        pid = str(r.get("patient_id") or "")
        if not fid or not pid:
            continue
        key = f"{fid}:{pid}"
        if r.get("doc_source") == "chart_note":
            chart_by_key.setdefault(key, []).append(r)
        else:
            edoc_by_key.setdefault(key, []).append(r)
    return edoc_by_key, chart_by_key


def _summary_from_indexed_manifest(
    rows: list[dict[str, Any]] | None,
    *,
    kind: str,
) -> dict[str, Any]:
    """Build edoc or chart_notes summary from pre-filtered manifest rows."""
    if not rows:
        if kind == "chart_note":
            return summarize_chart_notes_downloads(
                notes_count=0, results=None, processed=False
            )
        return summarize_edoc_downloads(docs_count=0, results=None, processed=False)

    if kind != "chart_note":
        if len(rows) == 1 and rows[0].get("status") == "no_docs":
            return summarize_edoc_downloads(docs_count=0, results=None, processed=True)
        pseudo: list[dict[str, Any]] = []
        for r in rows:
            st = r.get("status", "")
            pseudo.append(
                {
                    "downloaded": st in ("ok", "skipped"),
                    "skipped": st == "skipped",
                    "error": r.get("error") if st == "error" else None,
                }
            )
        return summarize_edoc_downloads(
            docs_count=len(rows), results=pseudo, processed=True
        )

    pseudo_cn: list[dict[str, Any]] = []
    for r in rows:
        st = r.get("status", "")
        pseudo_cn.append(
            {
                "downloaded": st in ("ok", "skipped"),
                "skipped": st == "skipped",
                "error": r.get("error") if st == "error" else None,
            }
        )
    return summarize_chart_notes_downloads(
        notes_count=len(rows), results=pseudo_cn, processed=True
    )


def _disk_edoc_chart_summaries(
    edocs_dir: Path, patient_id: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fallback summaries from on-disk PDFs when manifests are pending/empty."""
    patient_dir = edocs_dir / str(patient_id)
    if not patient_dir.is_dir():
        return None, None
    edoc_files = [
        p
        for p in patient_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf"
    ]
    cn_dir = patient_dir / "chart_notes"
    cn_files = (
        [p for p in cn_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
        if cn_dir.is_dir()
        else []
    )
    edoc_summary = None
    chart_summary = None
    if edoc_files:
        results = [{"downloaded": True, "skipped": True, "error": None} for _ in edoc_files]
        edoc_summary = summarize_edoc_downloads(
            docs_count=len(edoc_files), results=results, processed=True
        )
    if cn_files:
        results = [{"downloaded": True, "skipped": True, "error": None} for _ in cn_files]
        chart_summary = summarize_chart_notes_downloads(
            notes_count=len(cn_files), results=results, processed=True
        )
    return edoc_summary, chart_summary


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
    session_lock=None,
    chart_notes_debug_dir: Path | None = None,
    prefer_http_chart_notes: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    # OBSOLETE for Case-centric pipeline — uses patient edocs/{patient_id}/ layout and
    # include_all_cases=True. Case work must use webpt_edco_scraper.case_download instead.
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
    notes_listed = False

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
                    session_lock=session_lock,
                    debug_dir=chart_notes_debug_dir,
                    timeout_ms=int(config.chart_timeout_sec * 1000),
                    prefer_http=prefer_http_chart_notes,
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
                notes_listed = True
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
                session_lock=session_lock,
                debug_dir=chart_notes_debug_dir,
                timeout_ms=int(config.chart_timeout_sec * 1000),
                prefer_http=prefer_http_chart_notes,
            )
            notes_listed = True

    # Ensure chart-note list is ready before PDF downloads (may already be listed).
    if not skip_chart_notes:
        if case_id is None:
            chart_notes_summary = summarize_chart_notes_downloads(
                notes_count=0, results=None, processed=True, no_case=True
            )
            log.warning(
                "Skipping chart notes for patient %s: no case_id from scheduler",
                patient.patient_id,
            )
        elif not notes_listed and need_chart_notes:
            notes = await fetch_patient_chart_notes(
                context,
                patient_id=patient.patient_id,
                case_id=case_id,
                page=page,
                config=config,
                page_lock=page_lock,
                session_lock=session_lock,
                debug_dir=chart_notes_debug_dir,
                timeout_ms=int(config.chart_timeout_sec * 1000),
                prefer_http=prefer_http_chart_notes,
            )
            notes_listed = True

    if need_chart_notes and case_id is not None:
        log.info(
            "Patient %s case %s: %d chart note(s) found",
            patient.patient_id,
            case_id,
            len(notes),
        )

    download_edocs = need_edocs and bool(docs)
    download_notes = (
        not skip_chart_notes and case_id is not None and bool(notes)
    )

    if parallel_pdfs and download_edocs and download_notes:
        edoc_results, chart_note_results = await asyncio.gather(
            download_patient_edocs(
                context,
                docs=docs,
                patient_id=patient.patient_id,
                output_dir=edocs_dir,
                config=config,
                skip_existing=skip_existing,
                parallel_pdfs=True,
            ),
            download_patient_chart_notes(
                context,
                notes=notes,
                patient_id=patient.patient_id,
                case_id=case_id,
                output_dir=edocs_dir,
                config=config,
                facility_id=clinic.facility_id,
                skip_existing=skip_existing,
                parallel_pdfs=True,
            ),
        )
    else:
        if download_edocs:
            edoc_results = await download_patient_edocs(
                context,
                docs=docs,
                patient_id=patient.patient_id,
                output_dir=edocs_dir,
                config=config,
                skip_existing=skip_existing,
                parallel_pdfs=parallel_pdfs,
            )
        if download_notes:
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

    # Session died mid-download: reauth once and retry only auth_expired files.
    auth_failed_edocs = [
        r for r in edoc_results if is_auth_expired_error(r.get("error"))
    ]
    auth_failed_notes = [
        r for r in chart_note_results if is_auth_expired_error(r.get("error"))
    ]
    if (auth_failed_edocs or auth_failed_notes) and page is not None:
        log.warning(
            "Patient %s: %d edoc + %d chart-note auth_expired — reauth + retry",
            patient.patient_id,
            len(auth_failed_edocs),
            len(auth_failed_notes),
        )

        async def _force_reauth():
            return await ensure_session_fresh(
                page,
                context,
                config,
                facility_id=str(clinic.facility_id),
                company_id=getattr(clinic, "company_id", None) or config.company_id,
                allow_oust=True,
                force=True,
            )

        if session_lock is not None:
            async with session_lock:
                if page_lock is not None:
                    async with page_lock:
                        fresh = await _force_reauth()
                else:
                    fresh = await _force_reauth()
        elif page_lock is not None:
            async with page_lock:
                fresh = await _force_reauth()
        else:
            fresh = await _force_reauth()
        # Mutate shared SessionState so the browser pool keeps a fresh CSRF.
        if session is not None:
            session.csrf_token = fresh.csrf_token
            session.vega_user_id = fresh.vega_user_id
        else:
            session = fresh

        if auth_failed_edocs and docs:
            failed_ids: set[int] = set()
            for r in auth_failed_edocs:
                eid = r.get("ext_doc_id")
                if eid is None:
                    continue
                try:
                    failed_ids.add(int(eid))
                except (TypeError, ValueError):
                    continue
            retry_docs = []
            for d in docs:
                eid = d.get("ExtDocID")
                if eid is None:
                    continue
                try:
                    if int(eid) in failed_ids:
                        retry_docs.append(d)
                except (TypeError, ValueError):
                    continue
            if retry_docs:
                retry_edoc = await download_patient_edocs(
                    context,
                    docs=retry_docs,
                    patient_id=patient.patient_id,
                    output_dir=edocs_dir,
                    config=config,
                    skip_existing=skip_existing,
                    parallel_pdfs=parallel_pdfs,
                )
                by_id = {}
                for r in retry_edoc:
                    eid = r.get("ext_doc_id")
                    if eid is None:
                        continue
                    try:
                        by_id[int(eid)] = r
                    except (TypeError, ValueError):
                        continue
                merged: list[dict[str, Any]] = []
                for r in edoc_results:
                    if not is_auth_expired_error(r.get("error")):
                        merged.append(r)
                        continue
                    eid = r.get("ext_doc_id")
                    try:
                        key = int(eid) if eid is not None else None
                    except (TypeError, ValueError):
                        key = None
                    merged.append(by_id.get(key, r) if key is not None else r)
                edoc_results = merged

        if auth_failed_notes and notes and case_id is not None:
            failed_note_ids = {
                r.get("note_id") for r in auth_failed_notes if r.get("note_id")
            }
            retry_notes = [
                n
                for n in notes
                if (n.cnsid or n.uri or n.dedupe_key) in failed_note_ids
            ]
            if retry_notes:
                retry_cn = await download_patient_chart_notes(
                    context,
                    notes=retry_notes,
                    patient_id=patient.patient_id,
                    case_id=case_id,
                    output_dir=edocs_dir,
                    config=config,
                    facility_id=clinic.facility_id,
                    skip_existing=skip_existing,
                    parallel_pdfs=parallel_pdfs,
                )
                by_nid = {r.get("note_id"): r for r in retry_cn}
                chart_note_results = [
                    by_nid.get(r.get("note_id"), r)
                    if is_auth_expired_error(r.get("error"))
                    else r
                    for r in chart_note_results
                ]

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
            pass  # summary already set above
        elif not notes:
            chart_notes_summary = summarize_chart_notes_downloads(
                notes_count=0, results=None, processed=True
            )
        else:
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
            await safe_close_context(context)


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
            await safe_close_context(context)


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
            await safe_close_context(context)

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
            await safe_close_context(context)

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


async def _maybe_restart_browser(
    playwright,
    config: WebPTConfig,
    *,
    context,
    page,
    session,
    clinic,
    total_patients: int,
    browser_restarts: int,
) -> tuple[Any, Any, Any, int]:
    every = config.browser_restart_every
    if every <= 0 or total_patients <= 0 or total_patients % every != 0:
        return context, page, session, browser_restarts
    if browser_restarts >= config.browser_restart_max:
        log.debug(
            "Skipping proactive browser restart (max restarts %d reached)",
            config.browser_restart_max,
        )
        return context, page, session, browser_restarts
    context, page, session = await restart_browser(
        playwright,
        config,
        old_context=context,
        clinic=clinic,
        reason=f"proactive restart after {total_patients} patients",
    )
    return context, page, session, browser_restarts + 1


async def cmd_export_recent_appointments(
    config: WebPTConfig,
    *,
    output_dir: Path,
    days: int,
    end_date: date | None,
    lookahead_days: int | None,
    as_of: date | None,
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
    rescan_facilities: bool = False,
    skip_completed_facilities: bool = False,
    parallel_pdfs: bool = True,
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
        as_of=as_of,
    )
    look = lookahead_days if lookahead_days is not None else days
    output_dir.mkdir(parents=True, exist_ok=True)
    write_status_guide(output_dir)
    edocs_dir = output_dir / "edocs"
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path)

    if rescan_facilities:
        cleared = len(checkpoint["completed_facilities"])
        checkpoint["completed_facilities"] = []
        _save_checkpoint(checkpoint_path, checkpoint)
        log.info(
            "Rescan: cleared %d completed_facilities (patient checkpoint kept)",
            cleared,
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    patients_csv = output_dir / f"patients_recent_{days}d.csv"
    patients_export_csv = output_dir / f"patients_export_{days}d.csv"
    edocs_manifest_path = output_dir / f"edocs_manifest_{ts}.csv"

    edoc_manifest: list[dict[str, Any]] = []
    export_rows: list[dict[str, Any]] = []
    total_patients = 0
    patients_since_checkpoint = 0

    if parallel_pdfs:
        from pdf_throttle import set_pdf_semaphore

        set_pdf_semaphore(asyncio.Semaphore(config.max_concurrent_pdfs))

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        browser_restarts = 0
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
                if (
                    skip_completed_facilities
                    and clinic.facility_id in checkpoint["completed_facilities"]
                ):
                    log.info(
                        "Skipping completed facility %s (%s) (--skip-completed-facilities)",
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
                try:
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
                except Exception as exc:
                    if is_transient_network_error(exc):
                        log.error(
                            "Skipping facility %s (%s) after scheduler network errors: %s",
                            clinic.facility_id,
                            clinic.name,
                            exc,
                        )
                        continue
                    raise

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
                discovery_only = skip_edocs and skip_chart_notes and not ocr_only

                for patient in patients:
                    if max_patients is not None and total_patients >= max_patients:
                        log.info("Reached --max-patients %d", max_patients)
                        break

                    key = _patient_key(clinic.facility_id, patient.patient_id)
                    already_done = key in checkpoint["processed_patient_ids"]

                    # Discovery mode: skip already-downloaded patients with no I/O.
                    if already_done and discovery_only:
                        continue

                    patient_done = False

                    while not patient_done:
                        try:
                            chart_fields: dict[str, str] = {}
                            ocr_summary = empty_ocr_summary()
                            chart_notes_summary = summarize_chart_notes_downloads(
                                notes_count=0, results=None, processed=False
                            )
                            # Skip chart fetch for checkpointed patients unless OCR-only
                            # (diagnosis validation) needs it.
                            fetch_chart = not skip_chart and (
                                not already_done or ocr_only
                            )
                            if fetch_chart:
                                case_raw = patient.case_id
                                if case_raw:
                                    chart = await fetch_patient_chart(
                                        context,
                                        patient_id=patient.patient_id,
                                        case_id=case_raw,
                                        page=page,
                                        config=config,
                                        timeout_ms=int(config.chart_timeout_sec * 1000),
                                        debug_dir=output_dir / "debug",
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
                                        parallel_pdfs=parallel_pdfs,
                                    )
                                )
                                if edoc_rows:
                                    edoc_manifest.extend(edoc_rows)
                                # Only mark processed when downloads (or OCR-only) actually ran.
                                # Discovery-only must leave patients pending for parallel-download.
                                if not already_done:
                                    if not discovery_only:
                                        checkpoint["processed_patient_ids"].append(key)
                                    total_patients += 1
                                    patients_since_checkpoint += 1

                                if not discovery_only:
                                    session = await _maybe_refresh_session(
                                        page,
                                        context,
                                        session,
                                        total_patients=total_patients,
                                    )
                                    context, page, session, browser_restarts = (
                                        await _maybe_restart_browser(
                                            playwright,
                                            config,
                                            context=context,
                                            page=page,
                                            session=session,
                                            clinic=clinic,
                                            total_patients=total_patients,
                                            browser_restarts=browser_restarts,
                                        )
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
                            patient_done = True
                        except Exception as exc:
                            if (
                                is_browser_connection_lost(exc)
                                and browser_restarts < config.browser_restart_max
                            ):
                                log.warning(
                                    "Browser driver lost on patient %s (%s) — restart %d/%d",
                                    patient.patient_id,
                                    key,
                                    browser_restarts + 1,
                                    config.browser_restart_max,
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
                                context, page, session = await restart_browser(
                                    playwright,
                                    config,
                                    old_context=context,
                                    clinic=clinic,
                                )
                                browser_restarts += 1
                                continue
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

                # Progress hint only — default runs always re-query the scheduler.
                if clinic.facility_id not in checkpoint["completed_facilities"]:
                    checkpoint["completed_facilities"].append(clinic.facility_id)
                _save_checkpoint(checkpoint_path, checkpoint)

            await save_storage_state(context)
        finally:
            await safe_close_context(context)
            if parallel_pdfs:
                from pdf_throttle import set_pdf_semaphore

                set_pdf_semaphore(None)

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
        poc_summary = export_plans_of_care(
            edocs_dir,
            output_dir,
            tesseract_cmd=config.tesseract_cmd or None,
            ocr_dpi=config.ocr_dpi,
        )
        log.info(
            "Structured export: %d plans of care, %d POC goals",
            poc_summary["plans_of_care_count"],
            poc_summary.get("poc_goals_count", 0),
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
    skip_filled: bool = False,
    facility_id: str | None = None,
) -> None:
    assert_exclusive_webpt_session()
    rows_in = _read_csv_rows(input_csv)
    if not rows_in:
        log.warning("No rows in %s", input_csv)
        return

    if facility_id:
        want = str(facility_id)
        before = len(rows_in)
        rows_in = [r for r in rows_in if str(r.get("facility_id") or "") == want]
        log.info(
            "Filtered to facility_id=%s: %d / %d rows", want, len(rows_in), before
        )
        if not rows_in:
            log.warning("No rows for facility_id=%s", want)
            return

    manifest_source = manifest_dir or output_dir
    log.info("Loading manifests from %s ...", manifest_source)
    manifest_rows = _load_edoc_manifest_rows(manifest_source)
    log.info("Indexing %d manifest rows ...", len(manifest_rows))
    edoc_idx, chart_idx = _index_manifest_rows_by_patient(manifest_rows)
    out_path = output_csv or (output_dir / patients_export_filename_from_input(input_csv))
    if output_csv is None and input_csv.name == "patients_export_10d.csv":
        out_path = output_dir / "patients_export_10d.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_status_guide(output_dir)
    debug_dir = output_dir / "debug"
    # Never overwrite the canonical export with a partial mid-run snapshot.
    partial_path = out_path.with_name(out_path.stem + ".enrich_partial" + out_path.suffix)

    enriched: list[dict[str, Any]] = []

    def _row_edoc_chart_summaries(pid: int, fid: str, row: dict[str, str]):
        key = f"{fid}:{pid}"
        edoc_summary = _summary_from_indexed_manifest(edoc_idx.get(key), kind="edoc")
        if edoc_summary["edoc_status"] == "pending" and row.get("edoc_status"):
            edoc_summary = {
                "edoc_status": row.get("edoc_status", ""),
                "edoc_files_total": int(row.get("edoc_files_total") or 0),
                "edoc_files_downloaded": int(row.get("edoc_files_downloaded") or 0),
                "edoc_files_skipped": int(row.get("edoc_files_skipped") or 0),
                "edoc_files_failed": int(row.get("edoc_files_failed") or 0),
                "edoc_errors": row.get("edoc_errors") or "",
            }
        chart_notes_summary = _summary_from_indexed_manifest(
            chart_idx.get(key), kind="chart_note"
        )
        if chart_notes_summary["chart_notes_status"] == "pending" and row.get(
            "chart_notes_status"
        ):
            chart_notes_summary = {
                "chart_notes_status": row.get("chart_notes_status", ""),
                "chart_notes_total": int(row.get("chart_notes_total") or 0),
                "chart_notes_downloaded": int(row.get("chart_notes_downloaded") or 0),
                "chart_notes_skipped": int(row.get("chart_notes_skipped") or 0),
                "chart_notes_failed": int(row.get("chart_notes_failed") or 0),
                "chart_notes_errors": row.get("chart_notes_errors") or "",
            }
        return edoc_summary, chart_notes_summary

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            await ensure_authenticated(page, context, config)
            clinics = await list_clinics(page, config.company_id)
            clinics_by_id = {str(c.facility_id): c for c in clinics}
            current_facility: str | None = None

            # Process by facility so clinic switches are rare.
            rows_sorted = sorted(
                rows_in,
                key=lambda r: (
                    str(r.get("facility_id") or ""),
                    str(r.get("patient_id") or ""),
                ),
            )

            for row in rows_sorted:
                if max_patients is not None and len(enriched) >= max_patients:
                    break
                pid = int(row.get("patient_id") or row.get("PatientID") or 0)
                fid = str(row.get("facility_id") or "")
                case_raw = row.get("case_id") or row.get("CaseID") or ""
                case_id = int(case_raw) if str(case_raw).strip() else None

                already_filled = bool(
                    (row.get("diagnosis") or "").strip()
                    or (row.get("copay") or "").strip()
                    or (row.get("deductible") or "").strip()
                )
                if skip_filled and already_filled and not skip_chart:
                    # Keep existing chart fields; still refresh edoc/chart_notes from manifests.
                    chart_fields = {
                        k: row.get(k) or ""
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
                        )
                        if row.get(k)
                    }
                    edoc_summary, chart_notes_summary = _row_edoc_chart_summaries(
                        pid, fid, row
                    )
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
                            row.get("appointments_past_count") or 0
                        ),
                        appointments_past_dates=[
                            d.strip()
                            for d in (row.get("appointments_past_dates") or "").split(";")
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
                    enriched.append(
                        build_patient_export_row(
                            clinic_name=row.get("facility_name") or "",
                            patient=patient,
                            chart_fields=chart_fields,
                            edoc_summary=edoc_summary,
                            chart_notes_summary=chart_notes_summary,
                        )
                    )
                    continue

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
                    log.info(
                        "Fetching chart patient=%s case=%s facility=%s",
                        pid,
                        case_id,
                        fid,
                    )
                    clinic = clinics_by_id.get(fid)
                    if clinic is None:
                        log.warning(
                            "Unknown facility_id=%s for patient=%s — chart fetch may fail",
                            fid,
                            pid,
                        )
                    elif fid != current_facility:
                        try:
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=clinic.company_id,
                                facility_id=clinic.facility_id,
                            )
                            current_facility = fid
                        except ClinicSwitchError as exc:
                            log.warning(
                                "Clinic switch failed facility=%s patient=%s: %s",
                                fid,
                                pid,
                                exc,
                            )
                            current_facility = None

                    chart = await fetch_patient_chart(
                        context,
                        patient_id=pid,
                        case_id=case_id,
                        page=page,
                        config=config,
                        timeout_ms=int(config.chart_timeout_sec * 1000),
                        debug_dir=debug_dir,
                    )
                    chart_fields = chart_to_dict(chart)
                    needs_reswitch = chart.fetch_error in (
                        FETCH_ERROR_DISPLAY_PATIENTS,
                        FETCH_ERROR_LOGIN,
                    ) or not any(chart_fields.values())
                    if needs_reswitch and clinic is not None:
                        log.warning(
                            "Chart empty/wrong clinic patient=%s facility=%s (%s) — re-settle once",
                            pid,
                            fid,
                            chart.fetch_error or "empty",
                        )
                        current_facility = None
                        try:
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=clinic.company_id,
                                facility_id=clinic.facility_id,
                            )
                            current_facility = fid
                            chart = await fetch_patient_chart(
                                context,
                                patient_id=pid,
                                case_id=case_id,
                                page=page,
                                config=config,
                                timeout_ms=int(config.chart_timeout_sec * 1000),
                                debug_dir=debug_dir,
                            )
                            chart_fields = chart_to_dict(chart)
                        except ClinicSwitchError as exc:
                            log.warning(
                                "Re-settle failed facility=%s patient=%s: %s",
                                fid,
                                pid,
                                exc,
                            )
                            current_facility = None

                    # Empty parse usually means Display Patients redirect (wrong clinic).
                    if not any(chart_fields.values()):
                        log.warning(
                            "Chart empty for patient=%s facility=%s — keeping prior row fields if any",
                            pid,
                            fid,
                        )
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
                    log.info(
                        "Chart patient=%s diagnosis=%r copay=%r",
                        pid,
                        (chart_fields.get("diagnosis") or "")[:60],
                        chart_fields.get("copay") or "",
                    )
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

                edoc_summary, chart_notes_summary = _row_edoc_chart_summaries(
                    pid, fid, row
                )

                enriched.append(
                    build_patient_export_row(
                        clinic_name=row.get("facility_name") or "",
                        patient=patient,
                        chart_fields=chart_fields,
                        edoc_summary=edoc_summary,
                        chart_notes_summary=chart_notes_summary,
                    )
                )
                if len(enriched) % 100 == 0:
                    log.info("Enrich progress %d / %d", len(enriched), len(rows_in))
                if len(enriched) % 250 == 0:
                    _write_manifest_rows(
                        partial_path, enriched, PATIENT_EXPORT_FIELDNAMES
                    )
                    log.info(
                        "Enrich checkpoint wrote %d rows to %s",
                        len(enriched),
                        partial_path,
                    )
                if config.action_delay_sec > 0:
                    await asyncio.sleep(config.action_delay_sec)

            await save_storage_state(context)
        finally:
            await safe_close_context(context)

    _write_manifest_rows(out_path, enriched, PATIENT_EXPORT_FIELDNAMES)
    log.info("Wrote enriched export: %s (%d rows)", out_path, len(enriched))
    if partial_path.exists() and partial_path != out_path:
        try:
            partial_path.unlink()
        except OSError:
            pass


def cmd_repair_patient_export(
    *,
    input_csv: Path,
    output_dir: Path,
    output_csv: Path | None,
    as_of: date,
    manifest_dir: Path | None,
) -> None:
    """Offline repair: reclassify appointments, fix titles, restore edoc/chart status."""
    rows_in = _read_csv_rows(input_csv)
    if not rows_in:
        log.warning("No rows in %s", input_csv)
        return

    manifest_source = manifest_dir or output_dir
    log.info("Loading manifests from %s ...", manifest_source)
    manifest_rows = _load_edoc_manifest_rows(manifest_source)
    log.info("Indexing %d manifest rows ...", len(manifest_rows))
    edoc_idx, chart_idx = _index_manifest_rows_by_patient(manifest_rows)
    edocs_dir = output_dir / "edocs"

    out_path = output_csv or (output_dir / patients_export_filename_from_input(input_csv))
    # Keep legacy 10d path writable when repairing that file specifically.
    if output_csv is None and input_csv.name == "patients_export_10d.csv":
        out_path = output_dir / "patients_export_10d.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_status_guide(output_dir)

    repaired: list[dict[str, Any]] = []
    upcoming_gt0 = 0
    for i, row in enumerate(rows_in):
        if i and i % 5000 == 0:
            log.info("Repair progress %d / %d", i, len(rows_in))
        pid = int(row.get("patient_id") or row.get("PatientID") or 0)
        fid = str(row.get("facility_id") or "")
        key = f"{fid}:{pid}"
        edoc_summary = _summary_from_indexed_manifest(
            edoc_idx.get(key), kind="edoc"
        )
        chart_notes_summary = _summary_from_indexed_manifest(
            chart_idx.get(key), kind="chart_note"
        )
        # Disk fallback when manifests still say pending but PDFs exist.
        disk_edoc, disk_chart = _disk_edoc_chart_summaries(edocs_dir, pid)
        if disk_edoc and (
            str(edoc_summary.get("edoc_status") or "") in ("", "pending")
        ):
            edoc_summary = disk_edoc
        if disk_chart and (
            str(chart_notes_summary.get("chart_notes_status") or "")
            in ("", "pending")
        ):
            chart_notes_summary = disk_chart

        fixed = repair_patient_export_row(
            row,
            reference_date=as_of,
            edoc_summary=edoc_summary,
            chart_notes_summary=chart_notes_summary,
        )
        if int(fixed.get("appointments_upcoming_count") or 0) > 0:
            upcoming_gt0 += 1
        repaired.append(fixed)

    _write_manifest_rows(out_path, repaired, PATIENT_EXPORT_FIELDNAMES)
    log.info(
        "Repaired export: %s (%d patients, %d with upcoming>0, as_of=%s)",
        out_path,
        len(repaired),
        upcoming_gt0,
        as_of,
    )


async def cmd_export_checkouts(
    config: WebPTConfig,
    *,
    output_dir: Path,
    service_date: date | None,
    facility_id: str | None,
    skip_chart: bool,
    max_patients: int | None,
) -> None:
    """Export Checked Out visits for one service date (default: yesterday)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(config.timezone)).date()
    target = service_date if service_date is not None else today - timedelta(days=1)
    # WebPT scheduler returns 0 events when startDate == endDate (midnight-midnight);
    # fetch through the next calendar day and filter by service_date below.
    fetch_end = target + timedelta(days=1)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"checkouts_{target.isoformat()}.csv"
    checkpoint_path = output_dir / f"checkouts_{target.isoformat()}_checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path)
    completed_facilities = set(checkpoint.get("completed_facilities") or [])
    rows: list[dict[str, Any]] = []
    if out_path.exists() and completed_facilities:
        with out_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        log.info(
            "Resuming checkouts export with %d row(s) from %d facility(ies)",
            len(rows),
            len(completed_facilities),
        )
    total_visits = len(rows)

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
                "Export checkouts for %s across %d clinic(s) (skip_chart=%s)",
                target,
                len(clinics),
                skip_chart,
            )

            for clinic in clinics:
                if max_patients is not None and total_visits >= max_patients:
                    break

                fid = str(clinic.facility_id)
                if fid in completed_facilities:
                    log.info(
                        "Skipping completed facility %s (%s)",
                        fid,
                        clinic.name,
                    )
                    continue

                switched = False
                for switch_attempt in range(2):
                    try:
                        session = await switch_clinic_and_settle(
                            page,
                            context,
                            config,
                            company_id=clinic.company_id,
                            facility_id=clinic.facility_id,
                        )
                        switched = True
                        break
                    except ClinicSwitchError as exc:
                        log.warning(
                            "Clinic switch attempt %d failed facility %s (%s): %s",
                            switch_attempt + 1,
                            clinic.facility_id,
                            clinic.name,
                            exc,
                        )
                        try:
                            session = await ensure_authenticated(
                                page, context, config
                            )
                        except Exception as auth_exc:  # noqa: BLE001
                            log.warning(
                                "Re-auth after clinic switch failure also failed: %s",
                                auth_exc,
                            )
                if not switched:
                    log.error(
                        "Skipping facility %s (%s): clinic switch failed",
                        clinic.facility_id,
                        clinic.name,
                    )
                    continue

                try:
                    events = await fetch_scheduler_events(
                        context,
                        facility_id=clinic.facility_id,
                        start_date=target,
                        end_date=fetch_end,
                        session=session,
                        config=config,
                    )
                except Exception as exc:
                    msg = str(exc)
                    waf_blocked = "HTTP 403" in msg or "HTTP 429" in msg
                    if is_transient_network_error(exc) or waf_blocked:
                        log.error(
                            "Skipping facility %s (%s) after scheduler error: %s",
                            clinic.facility_id,
                            clinic.name,
                            exc,
                        )
                        if waf_blocked:
                            try:
                                session = await ensure_authenticated(
                                    page, context, config
                                )
                            except Exception as auth_exc:  # noqa: BLE001
                                log.warning(
                                    "Re-auth after scheduler WAF also failed: %s",
                                    auth_exc,
                                )
                        continue
                    raise

                visits = extract_checkout_visits(
                    events,
                    facility_id=clinic.facility_id,
                    service_date=target,
                )
                log.info(
                    "Facility %s (%s): %d checked-out visit(s) on %s",
                    clinic.facility_id,
                    clinic.name,
                    len(visits),
                    target,
                )

                for visit in visits:
                    if max_patients is not None and total_visits >= max_patients:
                        break

                    chart_fields: dict[str, str] = {}
                    if not skip_chart and visit.case_id:
                        chart = await fetch_patient_chart(
                            context,
                            patient_id=visit.patient_id,
                            case_id=visit.case_id,
                            page=page,
                            config=config,
                            timeout_ms=int(config.chart_timeout_sec * 1000),
                            debug_dir=output_dir / "debug",
                        )
                        chart_fields = chart_to_dict(chart)
                    elif not skip_chart and not visit.case_id:
                        log.warning(
                            "No case_id for patient %s at facility %s — chart skipped",
                            visit.patient_id,
                            clinic.facility_id,
                        )

                    rows.append(
                        build_checkout_export_row(
                            clinic_name=clinic.name,
                            visit=visit,
                            chart_fields=chart_fields,
                        )
                    )
                    total_visits += 1

                # Flush after each clinic so mid-run CSV is inspectable.
                _write_manifest_rows(out_path, rows, CHECKOUT_EXPORT_FIELDNAMES)
                completed_facilities.add(fid)
                checkpoint["completed_facilities"] = sorted(completed_facilities)
                _save_checkpoint(checkpoint_path, checkpoint)
                log.info(
                    "Flushed %d checkout row(s) -> %s (after %s)",
                    len(rows),
                    out_path,
                    clinic.name,
                )
        finally:
            await save_storage_state(context)
            await safe_close_context(context)

    _write_manifest_rows(out_path, rows, CHECKOUT_EXPORT_FIELDNAMES)
    log.info("Wrote %d checkout row(s) -> %s", len(rows), out_path)


def _iter_date_chunks(
    start_date: date, end_date: date, *, chunk_days: int = 31
) -> list[tuple[date, date]]:
    """Inclusive date chunks for large scheduler windows."""
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    from datetime import timedelta

    chunks: list[tuple[date, date]] = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end_date)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _load_schedule_chart_lookup(chart_csv: Path | None) -> dict[tuple[str, str, str], dict[str, str]]:
    """Map (facility_id, patient_id, case_id) -> auth/copay/deductible/ins_name."""
    if chart_csv is None or not chart_csv.exists():
        return {}
    lookup: dict[tuple[str, str, str], dict[str, str]] = {}
    with chart_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (
                str(row.get("facility_id") or "").strip(),
                str(row.get("patient_id") or "").strip(),
                str(row.get("case_id") or "").strip(),
            )
            if not key[1]:
                continue
            lookup[key] = {
                "ins_name": str(row.get("ins_name") or ""),
                "auth_ins_visits": str(row.get("auth_ins_visits") or ""),
                "copay": str(row.get("copay") or ""),
                "deductible": str(row.get("deductible") or ""),
            }
    return lookup


async def cmd_export_schedule(
    config: WebPTConfig,
    *,
    output_dir: Path,
    start_date: date,
    end_date: date,
    facility_id: str | None,
    skip_chart: bool,
    chart_csv: Path | None,
    max_patients: int | None,
    chunk_days: int = 31,
) -> None:
    """Export all patient appointments in a date range with check-in/out times."""
    from datetime import timedelta

    if end_date < start_date:
        raise ValueError("--end-date must be >= --start-date")

    # Scheduler returns 0 events when startDate == endDate; extend fetch end by 1 day
    # per chunk, then filter via extract_schedule_visits.
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = (
        output_dir
        / f"schedule_visits_{start_date.isoformat()}_{end_date.isoformat()}.csv"
    )
    checkpoint_path = output_dir / "schedule_checkpoint.json"
    checkpoint = _load_checkpoint(checkpoint_path)
    completed = set(checkpoint.get("completed_facilities") or [])

    chart_lookup = {} if skip_chart else _load_schedule_chart_lookup(chart_csv)
    if chart_csv and not skip_chart:
        log.info(
            "Loaded %d chart lookup key(s) from %s",
            len(chart_lookup),
            chart_csv,
        )

    rows: list[dict[str, Any]] = []
    if out_path.exists() and completed:
        with out_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        log.info("Resuming schedule export with %d existing row(s)", len(rows))

    live_chart_cache: dict[tuple[str, int, int], dict[str, str]] = {}
    total_visits = len(rows)
    chunks = _iter_date_chunks(start_date, end_date, chunk_days=chunk_days)

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
                "Export schedule %s..%s (%d chunk(s)) across %d clinic(s) skip_chart=%s",
                start_date,
                end_date,
                len(chunks),
                len(clinics),
                skip_chart,
            )

            for clinic in clinics:
                if max_patients is not None and total_visits >= max_patients:
                    break
                fid = str(clinic.facility_id)
                if fid in completed:
                    log.info(
                        "Skipping completed facility %s (%s)",
                        fid,
                        clinic.name,
                    )
                    continue

                switched = False
                for switch_attempt in range(2):
                    try:
                        session = await switch_clinic_and_settle(
                            page,
                            context,
                            config,
                            company_id=clinic.company_id,
                            facility_id=clinic.facility_id,
                        )
                        switched = True
                        break
                    except ClinicSwitchError as exc:
                        log.warning(
                            "Clinic switch attempt %d failed facility %s (%s): %s",
                            switch_attempt + 1,
                            clinic.facility_id,
                            clinic.name,
                            exc,
                        )
                        try:
                            session = await ensure_authenticated(page, context, config)
                        except Exception as auth_exc:  # noqa: BLE001
                            log.warning(
                                "Re-auth after clinic switch failure also failed: %s",
                                auth_exc,
                            )
                    except Exception as exc:  # noqa: BLE001
                        if is_transient_network_error(exc) or "ERR_" in str(exc):
                            log.warning(
                                "Clinic switch network error facility %s attempt %d: %s",
                                clinic.facility_id,
                                switch_attempt + 1,
                                exc,
                            )
                            try:
                                session = await ensure_authenticated(
                                    page, context, config
                                )
                            except Exception as auth_exc:  # noqa: BLE001
                                log.warning("Re-auth failed: %s", auth_exc)
                        else:
                            raise
                if not switched:
                    log.error(
                        "Skipping facility %s (%s): clinic switch failed",
                        clinic.facility_id,
                        clinic.name,
                    )
                    continue

                facility_visits: list[Any] = []
                facility_ok = True
                for chunk_start, chunk_end in chunks:
                    fetch_end = chunk_end + timedelta(days=1)
                    try:
                        events = await fetch_scheduler_events(
                            context,
                            facility_id=clinic.facility_id,
                            start_date=chunk_start,
                            end_date=fetch_end,
                            session=session,
                            config=config,
                        )
                    except Exception as exc:
                        if is_transient_network_error(exc):
                            log.error(
                                "Facility %s chunk %s..%s scheduler error: %s",
                                clinic.facility_id,
                                chunk_start,
                                chunk_end,
                                exc,
                            )
                            facility_ok = False
                            break
                        raise
                    facility_visits.extend(
                        extract_schedule_visits(
                            events,
                            facility_id=clinic.facility_id,
                            start_date=chunk_start,
                            end_date=chunk_end,
                            checked_out_only=False,
                        )
                    )

                if not facility_ok:
                    continue

                # Dedupe across chunk overlaps (should be none, but safe).
                seen_visit: set[tuple[int, str, int | None]] = set()
                deduped = []
                for v in facility_visits:
                    key = (v.patient_id, v.appointment_at, v.case_id)
                    if key in seen_visit:
                        continue
                    seen_visit.add(key)
                    deduped.append(v)
                facility_visits = deduped

                log.info(
                    "Facility %s (%s): %d visit(s) in %s..%s",
                    clinic.facility_id,
                    clinic.name,
                    len(facility_visits),
                    start_date,
                    end_date,
                )

                for visit in facility_visits:
                    if max_patients is not None and total_visits >= max_patients:
                        break

                    chart_fields: dict[str, str] = {}
                    if not skip_chart:
                        lookup_key = (
                            str(visit.facility_id),
                            str(visit.patient_id),
                            str(visit.case_id or ""),
                        )
                        chart_fields = dict(chart_lookup.get(lookup_key) or {})
                        if (
                            not chart_fields.get("auth_ins_visits")
                            and not chart_fields.get("copay")
                            and visit.case_id
                        ):
                            cache_key = (
                                str(visit.facility_id),
                                visit.patient_id,
                                visit.case_id,
                            )
                            if cache_key in live_chart_cache:
                                chart_fields = live_chart_cache[cache_key]
                            else:
                                chart = await fetch_patient_chart(
                                    context,
                                    patient_id=visit.patient_id,
                                    case_id=visit.case_id,
                                    page=page,
                                    config=config,
                                    timeout_ms=int(config.chart_timeout_sec * 1000),
                                    debug_dir=output_dir / "debug",
                                )
                                chart_fields = chart_to_dict(chart)
                                live_chart_cache[cache_key] = chart_fields

                    row = build_checkout_export_row(
                        clinic_name=clinic.name,
                        visit=visit,
                        chart_fields=chart_fields,
                    )
                    # Prefer scheduler ins_name; fall back to chart lookup.
                    if not row.get("ins_name") and chart_fields.get("ins_name"):
                        row["ins_name"] = chart_fields["ins_name"]
                    rows.append(row)
                    total_visits += 1

                _write_manifest_rows(out_path, rows, SCHEDULE_EXPORT_FIELDNAMES)
                completed.add(fid)
                checkpoint["completed_facilities"] = sorted(completed)
                _save_checkpoint(checkpoint_path, checkpoint)
                log.info(
                    "Flushed %d schedule row(s) -> %s (after %s)",
                    len(rows),
                    out_path,
                    clinic.name,
                )
        finally:
            await save_storage_state(context)
            await safe_close_context(context)

    _write_manifest_rows(out_path, rows, SCHEDULE_EXPORT_FIELDNAMES)
    log.info("Wrote %d schedule row(s) -> %s", len(rows), out_path)


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
    p_recent.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD fetch window end")
    p_recent.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="YYYY-MM-DD past/upcoming cutoff (default: today ET, not --end-date)",
    )
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
    p_recent.add_argument(
        "--rescan-facilities",
        action="store_true",
        help="Clear completed_facilities so every clinic is re-queried (keeps processed patients)",
    )
    p_recent.add_argument(
        "--skip-completed-facilities",
        action="store_true",
        help="Old behavior: permanently skip clinics listed in checkpoint completed_facilities",
    )
    p_recent.add_argument(
        "--no-parallel-pdfs",
        action="store_true",
        help="Download PDFs one-by-one instead of concurrent gathers",
    )

    p_checkouts = sub.add_parser(
        "export-checkouts",
        help="Export Checked Out visits for one day (default: yesterday) with chart auth/copay/deductible",
    )
    p_checkouts.add_argument(
        "--date",
        type=str,
        default=None,
        help="YYYY-MM-DD service date (default: yesterday in WEBPT_TIMEZONE)",
    )
    p_checkouts.add_argument("--facility-id", type=str, default=None)
    p_checkouts.add_argument(
        "--output",
        type=Path,
        default=Path("output/checkouts"),
    )
    p_checkouts.add_argument(
        "--skip-chart",
        action="store_true",
        help="Skip patient chart fetch (scheduler fields only)",
    )
    p_checkouts.add_argument("--max-patients", type=int, default=None)

    p_schedule = sub.add_parser(
        "export-schedule",
        help="Export all patient appointments in a date range with check-in/out times",
    )
    p_schedule.add_argument(
        "--start-date",
        type=str,
        required=True,
        help="YYYY-MM-DD inclusive window start",
    )
    p_schedule.add_argument(
        "--end-date",
        type=str,
        required=True,
        help="YYYY-MM-DD inclusive window end",
    )
    p_schedule.add_argument("--facility-id", type=str, default=None)
    p_schedule.add_argument(
        "--output",
        type=Path,
        default=Path("output/jan_aug_2026"),
    )
    p_schedule.add_argument(
        "--skip-chart",
        action="store_true",
        help="Skip chart enrichment (scheduler fields only)",
    )
    p_schedule.add_argument(
        "--chart-csv",
        type=Path,
        default=None,
        help="Join auth/copay/deductible/ins_name from an existing patients export CSV",
    )
    p_schedule.add_argument(
        "--chunk-days",
        type=int,
        default=31,
        help="Scheduler fetch chunk size in days (default 31)",
    )
    p_schedule.add_argument("--max-patients", type=int, default=None)

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
        help="Default: output/patients_export_{N}d.csv from --input stem",
    )
    p_enrich.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="Directory with edocs_manifest_*.csv (default: --output)",
    )
    p_enrich.add_argument("--skip-chart", action="store_true")
    p_enrich.add_argument(
        "--skip-filled",
        action="store_true",
        help="Skip chart fetch when diagnosis/copay/deductible already present",
    )
    p_enrich.add_argument(
        "--facility-id",
        type=str,
        default=None,
        help="Only enrich rows for this facility_id (smoke / single clinic)",
    )
    p_enrich.add_argument("--max-patients", type=int, default=None)

    p_payments = sub.add_parser(
        "scrape-patient-payments",
        help="Scrape Patient Payments; write paid dump + unpaid sheet",
    )
    p_payments.add_argument(
        "--outreach-csv",
        type=Path,
        default=Path("output/jun_jul_2026/patients_copay_no_upcoming.csv"),
    )
    p_payments.add_argument(
        "--export-csv",
        type=Path,
        default=Path("output/jun_jul_2026/patients_export_10d.csv"),
    )
    p_payments.add_argument(
        "--output",
        type=Path,
        default=Path("output/jun_jul_2026"),
    )
    p_payments.add_argument("--from-month", type=str, default="2026-01")
    p_payments.add_argument("--to-month", type=str, default="2026-05")
    p_payments.add_argument("--max-patients", type=int, default=None)
    p_payments.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent HTTP payment fetches (single browser session)",
    )
    p_payments.add_argument(
        "--all-export",
        action="store_true",
        help="Scrape every patient/case in --export-csv (ignore --outreach-csv)",
    )

    p_repair = sub.add_parser(
        "repair-patient-export",
        help="Offline: reclassify appointments, fix titles, restore edoc/chart status from manifests",
    )
    p_repair.add_argument("--input", type=Path, required=True)
    p_repair.add_argument("--output", type=Path, required=True)
    p_repair.add_argument("--output-csv", type=Path, default=None)
    p_repair.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="YYYY-MM-DD past/upcoming cutoff (default: today ET)",
    )
    p_repair.add_argument(
        "--manifest-dir",
        type=Path,
        default=None,
        help="Directory with edocs_manifest_*.csv (default: --output)",
    )

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
        help=(
            "Patient workers (clamped to 1: WebPT single-session). "
            "PDF speed via WEBPT_MAX_CONCURRENT_PDFS"
        ),
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

    p_export_poc = sub.add_parser(
        "export-plans-of-care",
        help="Extract Frequency / Duration / Plan and Short/Long Term Goals from Plan of Care chart_notes PDFs",
    )
    p_export_poc.add_argument(
        "--edocs-dir",
        type=Path,
        required=True,
        help="edocs root (contains {patient_id}/chart_notes/)",
    )
    p_export_poc.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory for plans_of_care.csv",
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
        as_of = None
        if getattr(args, "as_of", None):
            as_of = date.fromisoformat(args.as_of)
        await cmd_export_recent_appointments(
            config,
            output_dir=args.output,
            days=args.days,
            end_date=end_date,
            lookahead_days=args.lookahead_days,
            as_of=as_of,
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
            rescan_facilities=args.rescan_facilities,
            skip_completed_facilities=args.skip_completed_facilities,
            parallel_pdfs=not args.no_parallel_pdfs,
        )
    elif args.command == "export-checkouts":
        service_date = None
        if args.date:
            service_date = date.fromisoformat(args.date)
        await cmd_export_checkouts(
            config,
            output_dir=args.output,
            service_date=service_date,
            facility_id=args.facility_id,
            skip_chart=args.skip_chart,
            max_patients=args.max_patients,
        )
    elif args.command == "export-schedule":
        await cmd_export_schedule(
            config,
            output_dir=args.output,
            start_date=date.fromisoformat(args.start_date),
            end_date=date.fromisoformat(args.end_date),
            facility_id=args.facility_id,
            skip_chart=args.skip_chart,
            chart_csv=args.chart_csv,
            max_patients=args.max_patients,
            chunk_days=args.chunk_days,
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
            skip_filled=getattr(args, "skip_filled", False),
            facility_id=getattr(args, "facility_id", None),
        )
    elif args.command == "scrape-patient-payments":
        from payments_scrape import cmd_scrape_patient_payments

        await cmd_scrape_patient_payments(
            config,
            outreach_csv=None if args.all_export else args.outreach_csv,
            export_csv=args.export_csv,
            output_dir=args.output,
            start_month=args.from_month,
            end_month=args.to_month,
            max_patients=args.max_patients,
            concurrency=args.concurrency,
            all_export=args.all_export,
            assert_exclusive=assert_exclusive_webpt_session,
        )
    elif args.command == "repair-patient-export":
        from zoneinfo import ZoneInfo

        as_of = (
            date.fromisoformat(args.as_of)
            if args.as_of
            else datetime.now(ZoneInfo(config.timezone)).date()
        )
        cmd_repair_patient_export(
            input_csv=args.input,
            output_dir=args.output,
            output_csv=args.output_csv,
            as_of=as_of,
            manifest_dir=args.manifest_dir,
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
    elif args.command == "export-plans-of-care":
        summary = export_plans_of_care(
            args.edocs_dir,
            args.output,
            tesseract_cmd=config.tesseract_cmd or None,
            ocr_dpi=config.ocr_dpi,
        )
        log.info(
            "export-plans-of-care: %d rows, %d goals -> %s",
            summary["plans_of_care_count"],
            summary["poc_goals_count"],
            summary["plans_of_care_path"],
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
