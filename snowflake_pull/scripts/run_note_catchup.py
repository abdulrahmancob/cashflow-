"""Download + extract daily notes for recent visits that still have none.

Targets last N calendar days of core.visit rows (not cancelled/no_show) with no
core.clinical_note. Downloads only Daily Note PDFs for those DOS values, extracts
in a process pool, and writes extracted/catchup/{daily_notes,cpt_codes}.csv.

Run inside the scraper container (Playwright + Tesseract + DB).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cashflow_db.config import CASE_PIPELINE_DIR  # noqa: E402
from cashflow_db.db import connect  # noqa: E402

log = logging.getLogger("note_catchup")

_LOGGED_ERRORS = 20


def chart_list_extra(parameter_names, not_before: str | None) -> dict[str, str]:
    """Pass not_before only when this image's chart listing accepts it.

    The scraper image bakes webpt_edco_scraper and is not rebuilt on deploy.
    Wanted dates are still filtered after the list.
    """
    if not_before and "not_before" in parameter_names:
        return {"not_before": not_before}
    return {}


MISSING_SQL = """
SELECT
    v.visit_id::text AS visit_id,
    v.service_date::text AS service_date,
    pc.webpt_case_id AS case_id,
    p.webpt_patient_id AS patient_id,
    COALESCE(f.webpt_facility_id, '') AS facility_id
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN ref.facility f ON f.facility_id = COALESCE(v.facility_id, pc.facility_id)
WHERE v.service_date >= CURRENT_DATE - (%s::int)
  AND v.service_date <= CURRENT_DATE
  AND v.status NOT IN ('cancelled', 'no_show')
  AND pc.webpt_case_id IS NOT NULL
  AND p.webpt_patient_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id
  )
ORDER BY v.service_date DESC, f.webpt_facility_id, pc.webpt_case_id
"""

MISSING_SQL_SINCE = """
SELECT
    v.visit_id::text AS visit_id,
    v.service_date::text AS service_date,
    pc.webpt_case_id AS case_id,
    p.webpt_patient_id AS patient_id,
    COALESCE(f.webpt_facility_id, '') AS facility_id
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN ref.facility f ON f.facility_id = COALESCE(v.facility_id, pc.facility_id)
WHERE v.service_date >= %s::date
  AND v.service_date <= CURRENT_DATE
  AND v.status NOT IN ('cancelled', 'no_show')
  AND pc.webpt_case_id IS NOT NULL
  AND p.webpt_patient_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id
  )
ORDER BY v.service_date DESC, f.webpt_facility_id, pc.webpt_case_id
"""

MISSING_SQL_ON_DATE = """
SELECT
    v.visit_id::text AS visit_id,
    v.service_date::text AS service_date,
    pc.webpt_case_id AS case_id,
    p.webpt_patient_id AS patient_id,
    COALESCE(f.webpt_facility_id, '') AS facility_id
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN ref.facility f ON f.facility_id = COALESCE(v.facility_id, pc.facility_id)
WHERE v.service_date::text = ANY(%s)
  AND v.status NOT IN ('cancelled', 'no_show')
  AND pc.webpt_case_id IS NOT NULL
  AND p.webpt_patient_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id
  )
ORDER BY f.webpt_facility_id, pc.webpt_case_id
"""


def _on_dates() -> list[str]:
    raw = os.getenv("NOTE_CATCHUP_ON_DATE", "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def query_missing_visits(*, days: int, since: str | None = None) -> list[dict[str, str]]:
    on_dates = _on_dates()
    with connect() as conn:
        if since:
            rows = conn.execute(MISSING_SQL_SINCE, (since,)).fetchall()
        elif on_dates:
            rows = conn.execute(MISSING_SQL_ON_DATE, (on_dates,)).fetchall()
        else:
            rows = conn.execute(MISSING_SQL, (int(days),)).fetchall()
    out: list[dict[str, str]] = []
    for row in rows:
        out.append(
            {
                "visit_id": str(row["visit_id"]),
                "service_date": str(row["service_date"])[:10],
                "case_id": str(row["case_id"] or "").strip(),
                "patient_id": str(row["patient_id"] or "").strip(),
                "facility_id": str(row.get("facility_id") or "").strip(),
            }
        )
    return [r for r in out if r["case_id"] and r["patient_id"]]


def _group_cases(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["facility_id"], row["case_id"], row["patient_id"])
        item = grouped.setdefault(
            key,
            {
                "facility_id": row["facility_id"],
                "case_id": row["case_id"],
                "patient_id": row["patient_id"],
                "dos": set(),
            },
        )
        item["dos"].add(row["service_date"])
    items = list(grouped.values())
    items.sort(key=lambda c: max(c["dos"] or [""]), reverse=True)
    items.sort(key=lambda c: c.get("facility_id") or "")
    return items


def _order_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One clinic at a time, and every case of a patient before the next patient."""
    return sorted(
        cases,
        key=lambda case: (
            case.get("facility_id") or "",
            case.get("patient_id") or "",
            case.get("case_id") or "",
        ),
    )


def select_cases(
    cases: list[dict[str, Any]],
    shard_index: int,
    shard_count: int,
    only_facilities: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Use an explicit clinic list when given. Otherwise bin-pack whole clinics."""
    wanted = {str(facility).strip() for facility in (only_facilities or []) if str(facility).strip()}
    if not wanted:
        return _slice_cases(cases, shard_index, shard_count)
    chosen = [case for case in cases if (case.get("facility_id") or "") in wanted]
    return _order_cases(chosen)


def _slice_cases(
    cases: list[dict[str, Any]], shard_index: int, shard_count: int
) -> list[dict[str, Any]]:
    """Give each account whole clinics. Cases of one patient stay together inside that clinic."""
    if shard_count <= 1:
        return _order_cases(cases)
    by_facility: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_facility.setdefault(case.get("facility_id") or "", []).append(case)

    def weight(facility: str) -> int:
        return sum(len(case["dos"]) for case in by_facility[facility])

    loads = [0] * shard_count
    assigned: list[list[str]] = [[] for _ in range(shard_count)]
    for facility in sorted(by_facility, key=weight, reverse=True):
        target = min(range(shard_count), key=lambda index: (loads[index], index))
        assigned[target].append(facility)
        loads[target] += weight(facility)
    chosen: list[dict[str, Any]] = []
    for facility in assigned[shard_index]:
        chosen.extend(by_facility[facility])
    return _order_cases(chosen)


def _is_daily(note: Any) -> bool:
    t = (getattr(note, "note_type", None) or "").lower()
    uri = (getattr(note, "uri", None) or "").lower()
    return "daily" in t or "dailynote" in uri or "dn" in uri


def _extract_one(payload: tuple[str, str, str, str]) -> dict[str, Any]:
    pdf_s, patient_id, facility_id, case_id = payload
    from case_extract import CASE_CPT_CODES_FIELDNAMES, CASE_DAILY_NOTES_FIELDNAMES  # noqa: F401
    from chart_notes_parse import cpt_code_rows, daily_note_row, extract_daily_note

    pdf = Path(pdf_s)
    extract = extract_daily_note(pdf, patient_id=patient_id)
    base = daily_note_row(extract)
    base["facility_id"] = facility_id
    base["case_id"] = case_id
    base["patient_id"] = patient_id or base.get("patient_id", "")
    base["source_url"] = ""
    base["downloaded_at"] = datetime.now(timezone.utc).isoformat()
    base["chart_id"] = ""
    base["visit_id"] = ""
    base["cnsid"] = ""
    base["appointment_id"] = ""
    cpt_rows = []
    for crow in cpt_code_rows(extract):
        crow["facility_id"] = facility_id
        crow["case_id"] = case_id
        crow["patient_id"] = patient_id
        crow["source_url"] = ""
        crow["downloaded_at"] = base["downloaded_at"]
        cpt_rows.append(crow)
    return {
        "note": base,
        "cpt": cpt_rows,
        "error": extract.error,
        "path": pdf_s,
    }


async def _shutdown_playwright(context: Any, pw: Any) -> None:
    """Close the browser without letting a wedged Chrome block the rest of the job."""
    pages: list[Any] = []
    try:
        pages = list(context.pages)
    except Exception as exc:
        log.warning("list pages failed: %s", exc)
    for page in pages:
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception as exc:
            log.warning("page close timed out: %s", exc)
    try:
        await asyncio.wait_for(context.close(), timeout=20)
    except Exception as exc:
        log.warning("context close timed out: %s", exc)
    try:
        await asyncio.wait_for(pw.stop(), timeout=15)
    except Exception as exc:
        log.warning("playwright stop timed out: %s", exc)


async def _download_cases(
    cases: list[dict[str, Any]],
    *,
    base_dir: Path,
    dry_run: bool,
    on_pdf: Any | None = None,
    on_case_done: Any | None = None,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    from auth import (
        ClinicSwitchError,
        create_context,
        ensure_authenticated,
        save_storage_state,
        switch_clinic,
    )
    from case_download import bounded_gather, note_subdir_for_type
    from case_paths import ensure_case_layout
    from chart_notes_api import fetch_patient_chart_notes
    from chart_notes_download import download_chart_note_pdf
    from config import WebPTConfig
    from pdf_throttle import set_pdf_semaphore

    config = WebPTConfig.from_env()
    downloaded: list[str] = []
    skipped = 0
    skipped_clinic = 0
    errors: list[str] = []
    logged_errors = 0
    fetch_params = inspect.signature(fetch_patient_chart_notes).parameters
    if "not_before" not in fetch_params:
        log.info("chart listing has no not_before filter; dates are filtered after listing")

    def record_error(message: str) -> None:
        nonlocal logged_errors
        errors.append(message)
        if logged_errors < _LOGGED_ERRORS:
            logged_errors += 1
            log.warning("%s", message)

    if dry_run:
        return {
            "downloaded": [],
            "skipped": 0,
            "errors": [],
            "cases": len(cases),
            "dry_run": True,
        }

    pw = await async_playwright().start()
    context = None
    try:
        context = await create_context(pw, config)
        page = await context.new_page()
        await ensure_authenticated(page, context, config)
        set_pdf_semaphore(asyncio.Semaphore(max(2, min(8, config.max_concurrent_pdfs))))
        current_fac = ""
        failed_fac = ""

        async def open_clinic(facility_id: str):
            """Switch clinic, and if the page is stuck retry once on a fresh page."""
            nonlocal page
            try:
                await switch_clinic(
                    page,
                    company_id=config.company_id,
                    facility_id=facility_id,
                )
                return None
            except (ClinicSwitchError, Exception) as first_exc:
                log.warning(
                    "clinic switch %s failed, retrying on a new page: %s",
                    facility_id,
                    first_exc,
                )
                try:
                    await page.close()
                except Exception:
                    pass
                page = await context.new_page()
                try:
                    await ensure_authenticated(page, context, config)
                    await switch_clinic(
                        page,
                        company_id=config.company_id,
                        facility_id=facility_id,
                    )
                    return None
                except (ClinicSwitchError, Exception) as second_exc:
                    return second_exc

        for case in cases:
            fac = case["facility_id"] or "_"
            if failed_fac and fac == failed_fac:
                skipped_clinic += 1
                continue
            if fac and fac != "_" and fac != current_fac:
                switch_exc = await open_clinic(fac)
                if switch_exc is None:
                    current_fac = fac
                    failed_fac = ""
                else:
                    failed_fac = fac
                    skipped_clinic += 1
                    record_error(f"clinic switch {fac} failed: {switch_exc}")
                    continue
            fid = case["facility_id"] or fac
            try:
                pid = int(case["patient_id"])
                cid = int(case["case_id"])
            except (TypeError, ValueError):
                record_error(f"bad ids case={case['case_id']} pid={case['patient_id']}")
                continue
            wanted_dos = set(case["dos"])
            not_before = min(wanted_dos) if wanted_dos else ""
            try:
                notes = await fetch_patient_chart_notes(
                    context,
                    patient_id=pid,
                    case_id=cid,
                    page=page,
                    config=config,
                    prefer_http=True,
                    **chart_list_extra(fetch_params, not_before or None),
                )
            except Exception as exc:
                record_error(f"list {fid}/{cid}: {exc}")
                continue
            targets = [
                n
                for n in notes
                if _is_daily(n) and str(getattr(n, "note_date", "") or "")[:10] in wanted_dos
            ]
            dest = ensure_case_layout(base_dir, fid, cid) / "daily_notes"

            async def _one(note: Any = None, dest_dir: Path = dest) -> dict[str, Any]:
                return await download_chart_note_pdf(
                    context,
                    note=note,
                    patient_id=pid,
                    case_id=cid,
                    dest_dir=dest_dir,
                    config=config,
                    facility_id=str(fid),
                    skip_existing=True,
                    parallel_pdfs=True,
                )

            factories = [lambda n=n: _one(note=n) for n in targets]
            results = await bounded_gather(factories) if factories else []
            for res in results:
                if res.get("error"):
                    record_error(str(res["error"]))
                elif res.get("skipped"):
                    skipped += 1
                    if res.get("path"):
                        downloaded.append(str(res["path"]))
                        if on_pdf:
                            on_pdf(str(res["path"]), case)
                elif res.get("path"):
                    downloaded.append(str(res["path"]))
                    if on_pdf:
                        on_pdf(str(res["path"]), case)
            if on_case_done:
                on_case_done()
            # Type unused import guard
            _ = note_subdir_for_type
        await save_storage_state(context)
        log.info(
            "download done pdfs=%s skipped=%s errors=%s skipped_clinic_cases=%s cases=%s",
            len(downloaded),
            skipped,
            len(errors),
            skipped_clinic,
            len(cases),
        )
    finally:
        if context is not None:
            await _shutdown_playwright(context, pw)
        else:
            try:
                await asyncio.wait_for(pw.stop(), timeout=15)
            except Exception as exc:
                log.warning("playwright stop timed out: %s", exc)
    return {
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": errors[:50],
        "error_count": len(errors),
        "skipped_clinic_cases": skipped_clinic,
        "cases": len(cases),
    }


def extract_pdfs(
    pdfs: list[tuple[str, str, str, str]],
    *,
    workers: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    notes: list[dict[str, str]] = []
    cpt: list[dict[str, str]] = []
    errors: list[str] = []
    if not pdfs:
        return notes, cpt, errors
    workers = max(1, min(int(workers), len(pdfs), 16))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_extract_one, item) for item in pdfs]
        for fut in as_completed(futs):
            try:
                row = fut.result()
            except Exception as exc:
                errors.append(str(exc))
                continue
            if row.get("error"):
                errors.append(f"{row.get('path')}: {row['error']}")
            if row.get("note"):
                notes.append(row["note"])
            cpt.extend(row.get("cpt") or [])
    return notes, cpt, errors


def write_extracts(
    out_dir: Path,
    notes: list[dict[str, str]],
    cpt: list[dict[str, str]],
) -> tuple[Path, Path]:
    from case_extract import CASE_CPT_CODES_FIELDNAMES, CASE_DAILY_NOTES_FIELDNAMES

    out_dir.mkdir(parents=True, exist_ok=True)
    notes_path = out_dir / "daily_notes.csv"
    cpt_path = out_dir / "cpt_codes.csv"

    import csv

    def _write(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in fields})

    _write(notes_path, notes, CASE_DAILY_NOTES_FIELDNAMES)
    _write(cpt_path, cpt, CASE_CPT_CODES_FIELDNAMES)
    return notes_path, cpt_path


def _visit_ids_for_note(note: dict[str, str]) -> list[str]:
    case_id = (note.get("case_id") or "").strip()
    dos = (note.get("date_of_daily_note") or "")[:10]
    if not case_id or len(dos) < 10:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT v.visit_id::text AS visit_id
            FROM core.visit v
            JOIN core.patient_case pc ON pc.case_pk = v.case_pk
            WHERE pc.webpt_case_id = %s AND v.service_date = %s::date
            """,
            (case_id, dos),
        ).fetchall()
    return [str(r["visit_id"]) for r in rows if r.get("visit_id")]


def _publish_note(
    row: dict[str, Any],
    live_dir: Path,
    counters: dict[str, int],
) -> None:
    note = row.get("note") or {}
    if not note:
        return
    notes_path, cpt_path = write_extracts(live_dir, [note], list(row.get("cpt") or []))
    try:
        from cashflow_db.loaders.load_webpt import load_webpt
        from cashflow_db.services.cpt_audit import run_cpt_audit

        load = load_webpt(
            notes_csv=notes_path,
            cpt_csv=cpt_path if cpt_path.exists() else None,
            notes_only=True,
        )
        counters["loaded_notes"] += int(load.get("notes") or 0)
        vids = _visit_ids_for_note(note)
        if vids:
            run_cpt_audit(visit_ids=vids)
            counters["audited_visits"] += len(vids)
        log.info(
            "published note case=%s dos=%s loaded=%s visits=%s",
            note.get("case_id"),
            note.get("date_of_daily_note"),
            load.get("notes"),
            vids,
        )
    except Exception as exc:
        log.warning("publish failed %s: %s", row.get("path"), exc)
        counters["publish_errors"] += 1


def _drain_ocr(
    futs: list[Any],
    *,
    live_dir: Path,
    counters: dict[str, int],
    notes: list[dict[str, str]],
    cpt: list[dict[str, str]],
    errors: list[str],
) -> list[Any]:
    still: list[Any] = []
    for fut in futs:
        if not fut.done():
            still.append(fut)
            continue
        try:
            row = fut.result()
        except Exception as exc:
            errors.append(str(exc))
            continue
        if row.get("error"):
            errors.append(f"{row.get('path')}: {row['error']}")
        if row.get("note"):
            notes.append(row["note"])
            cpt.extend(row.get("cpt") or [])
            _publish_note(row, live_dir, counters)
    return still


def run(
    *,
    days: int = 7,
    since: str | None = None,
    shard_index: int = 0,
    shard_count: int = 1,
    only_facilities: list[str] | None = None,
    out_dir: Path | None = None,
    workers: int | None = None,
    dry_run: bool = False,
    skip_download: bool = False,
) -> dict[str, Any]:
    base = Path(out_dir or CASE_PIPELINE_DIR)
    extract_dir = base / "extracted" / "catchup"
    pinned = [str(facility).strip() for facility in (only_facilities or []) if str(facility).strip()]
    if pinned:
        tag = min(pinned)
        live_dir = extract_dir / f"live-fac-{tag}"
        summary_dir = extract_dir / f"shard-fac-{tag}"
    elif shard_count > 1:
        live_dir = extract_dir / f"live-{shard_index}"
        summary_dir = extract_dir / f"shard-{shard_index}"
    else:
        live_dir = extract_dir / "live"
        summary_dir = extract_dir
    workers = max(1, min(int(workers or os.getenv("OCR_WORKERS", "10")), 16))
    on_dates = _on_dates()
    on_date = ",".join(on_dates)
    missing = query_missing_visits(days=days, since=since)
    all_cases = _group_cases(missing)
    cases = select_cases(all_cases, shard_index, shard_count, pinned)
    shard_visits = sum(len(case["dos"]) for case in cases)
    shard_facilities = len({case.get("facility_id") or "" for case in cases})
    summary: dict[str, Any] = {
        "missing_visits": shard_visits,
        "total_missing_visits": len(missing),
        "target_cases": len(cases),
        "facilities": shard_facilities,
        "ocr_workers": workers,
        "total_cases": len(all_cases),
        "days": days,
        "since": since,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "only_facilities": pinned,
        "on_date": on_date or None,
        "extract_dir": str(summary_dir),
        "loaded_notes": 0,
        "audited_visits": 0,
        "publish_errors": 0,
    }
    log.info(
        "note-catchup missing_visits=%s cases=%s facilities=%s ocr_workers=%s days=%s since=%s shard=%s/%s only_facilities=%s on_date=%s",
        shard_visits,
        len(cases),
        shard_facilities,
        workers,
        days,
        since or "-",
        shard_index,
        shard_count,
        ",".join(pinned) or "-",
        on_date or "-",
    )
    notes: list[dict[str, str]] = []
    cpt: list[dict[str, str]] = []
    errors: list[str] = []
    counters = {"loaded_notes": 0, "audited_visits": 0, "publish_errors": 0}
    futs: list[Any] = []

    def _pid_for(path: str, fac: str, case_id: str) -> str:
        for case in cases:
            if case["case_id"] == case_id and case["facility_id"] == fac:
                return case["patient_id"]
        return ""

    with ProcessPoolExecutor(max_workers=workers) as pool:
        def submit_pdf(path: str, pid: str, fac: str, case_id: str) -> None:
            futs.append(pool.submit(_extract_one, (path, pid, fac, case_id)))

        def on_pdf(path: str, case: dict[str, Any]) -> None:
            submit_pdf(path, case["patient_id"], case["facility_id"], case["case_id"])

        def on_case_done() -> None:
            nonlocal futs
            futs = _drain_ocr(
                futs,
                live_dir=live_dir,
                counters=counters,
                notes=notes,
                cpt=cpt,
                errors=errors,
            )

        if skip_download:
            from case_extract import iter_case_daily_note_pdfs
            from case_paths import parse_facility_case_from_path

            for pdf in iter_case_daily_note_pdfs(base / "cases"):
                try:
                    fac, case_id = parse_facility_case_from_path(pdf)
                except ValueError:
                    continue
                submit_pdf(str(pdf), _pid_for(str(pdf), fac, case_id), fac, case_id)
                if len(futs) >= workers * 2:
                    on_case_done()
        else:
            dl = asyncio.run(
                _download_cases(
                    cases,
                    base_dir=base,
                    dry_run=dry_run,
                    on_pdf=on_pdf,
                    on_case_done=on_case_done,
                )
            )
            summary["download"] = {
                "pdfs": len(dl.get("downloaded") or []),
                "skipped_existing": dl.get("skipped"),
                "errors": dl.get("error_count") or len(dl.get("errors") or []),
                "error_samples": (dl.get("errors") or [])[:_LOGGED_ERRORS],
                "skipped_clinic_cases": dl.get("skipped_clinic_cases") or 0,
            }
            if dry_run:
                summary["status"] = "dry_run"
                return summary

        remaining = list(futs)
        for fut in as_completed(remaining):
            futs = _drain_ocr(
                [fut],
                live_dir=live_dir,
                counters=counters,
                notes=notes,
                cpt=cpt,
                errors=errors,
            )

    notes_path, cpt_path = write_extracts(summary_dir, notes, cpt)
    summary["extracted_notes"] = len(notes)
    summary["extracted_cpt"] = len(cpt)
    summary["extract_errors"] = errors[:30]
    summary["notes_csv"] = str(notes_path)
    summary["cpt_csv"] = str(cpt_path)
    summary["loaded_notes"] = counters["loaded_notes"]
    summary["audited_visits"] = counters["audited_visits"]
    summary["publish_errors"] = counters["publish_errors"]
    summary["status"] = "ok"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catch up missing daily notes (last N days)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", default=None, help="Inclusive start date YYYY-MM-DD; end is CURRENT_DATE")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument(
        "--only-facility",
        action="append",
        default=[],
        help="Limit the run to this clinic id. Repeat for each clinic. Skips weight-based sharding.",
    )
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args(argv)
    if args.since:
        datetime.strptime(args.since, "%Y-%m-%d")
    if args.shard_count < 1:
        parser.error("--shard-count must be >= 1")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be >= 0 and less than --shard-count")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = run(
        days=args.days,
        since=args.since,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        only_facilities=args.only_facility,
        out_dir=args.out_dir,
        workers=args.workers,
        dry_run=args.dry_run,
        skip_download=args.skip_download,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in {"ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
