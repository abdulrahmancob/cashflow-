import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext

from auth import AUTH_EXPIRED, is_auth_redirect_url
from chart_notes_api import ChartNoteRef, build_print_pdf_url, patient_chart_note_url
from config import WebPTConfig
from edoc_download import (
    fetch_binary_with_auth_guard,
    response_indicates_auth_expired,
    sanitize_filename,
)
from logging_config import get_logger
from pdf_throttle import pdf_download_slot

log = get_logger("chart_notes_download")

# Optional coverage-recovery observability (repo-root snowflake_pull).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT.parent))


def _obs_emit(**kwargs: Any) -> None:
    try:
        from snowflake_pull.observability import get_global_obs

        obs = get_global_obs()
        if obs is None:
            return
        obs.emit(**kwargs)
    except Exception:
        return

CHART_NOTES_SUBDIR = "chart_notes"
URI_DATE_DN_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}).*?(?P<dn>DN\d+)",
    re.IGNORECASE,
)


def chart_note_filename(note: ChartNoteRef) -> str:
    date_part = note.note_date or "unknown-date"
    type_part = re.sub(r"[^\w\-]+", "_", (note.note_type or "ChartNote").strip())
    type_part = type_part.strip("_") or "ChartNote"

    if note.cnsid:
        return sanitize_filename(
            f"{date_part}_{type_part}_{note.cnsid}.pdf",
            f"chart_{note.cnsid}.pdf",
        )

    if note.uri:
        uri_match = URI_DATE_DN_RE.search(note.uri)
        if uri_match:
            date_part = uri_match.group("date")
            dn_id = uri_match.group("dn")
            return sanitize_filename(
                f"{date_part}_DailyNote_{dn_id}.pdf",
                f"chart_{dn_id}.pdf",
            )
        return sanitize_filename(note.uri, "chart_note.pdf")

    return sanitize_filename(f"{date_part}_{type_part}.pdf", "chart_note.pdf")


def chart_notes_dir(output_dir: Path, patient_id: int) -> Path:
    return output_dir / str(patient_id) / CHART_NOTES_SUBDIR


async def download_chart_note_pdf(
    context: BrowserContext,
    *,
    note: ChartNoteRef,
    patient_id: int,
    case_id: int,
    dest_dir: Path,
    config: WebPTConfig,
    facility_id: str = "",
    skip_existing: bool = True,
    parallel_pdfs: bool = False,
) -> dict[str, Any]:
    note_id = note.cnsid or note.uri or note.dedupe_key
    result: dict[str, Any] = {
        "note_id": note_id,
        "cnsid": note.cnsid,
        "uri": note.uri,
        "patient_id": patient_id,
        "case_id": case_id,
        "filename": "",
        "path": "",
        "downloaded": False,
        "error": None,
        "skipped": False,
    }

    filename = chart_note_filename(note)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = dest_dir / filename
    result["filename"] = filename
    t0 = time.perf_counter()
    _obs_emit(
        event="decision",
        level="INFO",
        operation="chart_download_start",
        webpt_patient_id=str(patient_id),
        facility_id=str(facility_id or ""),
        dos=note.note_date or "",
        outcome="start",
        extra={"note_id": str(note_id), "filename": filename},
    )

    if skip_existing and file_path.exists() and file_path.stat().st_size > 0:
        result["path"] = str(file_path)
        result["downloaded"] = True
        result["skipped"] = True
        log.debug("Skipped existing chart note: %s", filename)
        _obs_emit(
            event="decision",
            level="INFO",
            operation="chart_download",
            webpt_patient_id=str(patient_id),
            facility_id=str(facility_id or ""),
            dos=note.note_date or "",
            outcome="skip",
            decision="skip_existing_pdf",
            decision_reason="pdf_exists_nonzero",
            execution_ms=round((time.perf_counter() - t0) * 1000, 2),
            extra={"path": str(file_path)},
        )
        return result

    url = note.print_url or build_print_pdf_url(
        cnsid=note.cnsid,
        facility_id=note.facility_id or facility_id,
        patient_id=note.patient_id or str(patient_id),
        uri=note.uri,
        case_id=note.case_id or str(case_id),
    )
    timeout_ms = int(config.pdf_timeout_sec * 1000)
    referer = patient_chart_note_url(patient_id, case_id)

    async def _fetch_and_save() -> None:
        try:
            response = await fetch_binary_with_auth_guard(
                context,
                url,
                headers={
                    "Referer": referer,
                    "Accept": "application/pdf,*/*",
                },
                timeout_ms=timeout_ms,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "redirect" in msg or is_auth_redirect_url(str(exc)):
                result["error"] = AUTH_EXPIRED
                return
            raise

        if response_indicates_auth_expired(response):
            result["error"] = AUTH_EXPIRED
            return
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or ""
            if is_auth_redirect_url(location) or is_auth_redirect_url(response.url):
                result["error"] = AUTH_EXPIRED
                return
            result["error"] = f"unexpected redirect HTTP {response.status}"
            return
        if not response.ok:
            result["error"] = f"HTTP {response.status}"
            return

        body = await response.body()
        content_type = (response.headers.get("content-type") or "").lower()
        if not body:
            result["error"] = "empty response"
            return
        if "text/html" in content_type or body.lstrip()[:15].lower().startswith(
            b"<!doctype html"
        ) or body.lstrip()[:6].lower().startswith(b"<html"):
            result["error"] = AUTH_EXPIRED
            return
        if "pdf" not in content_type and not body.startswith(b"%PDF"):
            result["error"] = f"not a PDF (content-type={content_type})"
            return

        file_path.write_bytes(body)
        result["path"] = str(file_path)
        result["downloaded"] = True
        log.info("Downloaded chart note %s (%d bytes)", filename, len(body))

    try:
        if parallel_pdfs:
            async with pdf_download_slot():
                await _fetch_and_save()
        else:
            await _fetch_and_save()
            if config.pdf_delay_sec > 0:
                await asyncio.sleep(config.pdf_delay_sec)
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("Failed to download chart note %s: %s", filename, exc)
        err_type = "AuthExpired" if AUTH_EXPIRED in str(exc) else "ChartDownloadFailed"
        _obs_emit(
            event="error",
            level="ERROR",
            operation="chart_download",
            webpt_patient_id=str(patient_id),
            facility_id=str(facility_id or ""),
            dos=note.note_date or "",
            outcome="fail",
            error_type=err_type,
            error_expected=err_type == "AuthExpired",
            exception=exc,
            execution_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        try:
            from snowflake_pull.observability import get_global_obs

            obs = get_global_obs()
            if obs is not None and err_type == "AuthExpired":
                obs.set_auth_healthy(False)
        except Exception:
            pass
        return result

    if result.get("error") == AUTH_EXPIRED:
        _obs_emit(
            event="error",
            level="ERROR",
            operation="chart_download",
            webpt_patient_id=str(patient_id),
            facility_id=str(facility_id or ""),
            dos=note.note_date or "",
            outcome="fail",
            error_type="AuthExpired",
            error_expected=True,
            decision="auth_expired_abort_batch",
            decision_reason="chart_download_auth_expired",
            execution_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        try:
            from snowflake_pull.observability import get_global_obs

            obs = get_global_obs()
            if obs is not None:
                obs.set_auth_healthy(False)
        except Exception:
            pass
    else:
        _obs_emit(
            event="decision",
            level="INFO" if result.get("downloaded") else "WARN",
            operation="chart_download",
            webpt_patient_id=str(patient_id),
            facility_id=str(facility_id or ""),
            dos=note.note_date or "",
            outcome="success" if result.get("downloaded") else "fail",
            decision=(
                "chart_downloaded"
                if result.get("downloaded")
                else "chart_download_incomplete"
            ),
            decision_reason="ok" if result.get("downloaded") else (result.get("error") or "no_file"),
            error_type=None if result.get("downloaded") else "ChartDownloadFailed",
            error_expected=False,
            execution_ms=round((time.perf_counter() - t0) * 1000, 2),
            extra={"path": result.get("path") or "", "filename": filename},
        )
    if result.get("downloaded"):
        try:
            from snowflake_pull.observability import get_global_obs

            obs = get_global_obs()
            if obs is not None:
                obs.mark_success(
                    operation="chart_download",
                    webpt_patient_id=str(patient_id),
                    facility_id=str(facility_id or ""),
                    dos=note.note_date or "",
                )
                obs.metrics.incr("downloaded")
                obs.metrics.observe_latency((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    return result


async def download_patient_chart_notes(
    context: BrowserContext,
    *,
    notes: list[ChartNoteRef],
    patient_id: int,
    case_id: int,
    output_dir: Path,
    config: WebPTConfig,
    facility_id: str = "",
    skip_existing: bool = True,
    parallel_pdfs: bool = False,
) -> list[dict[str, Any]]:
    dest_dir = chart_notes_dir(output_dir, patient_id)
    if parallel_pdfs and notes:
        tasks = [
            download_chart_note_pdf(
                context,
                note=note,
                patient_id=patient_id,
                case_id=case_id,
                dest_dir=dest_dir,
                config=config,
                facility_id=facility_id,
                skip_existing=skip_existing,
                parallel_pdfs=True,
            )
            for note in notes
        ]
        return list(await asyncio.gather(*tasks))

    results: list[dict[str, Any]] = []
    for note in notes:
        row = await download_chart_note_pdf(
            context,
            note=note,
            patient_id=patient_id,
            case_id=case_id,
            dest_dir=dest_dir,
            config=config,
            facility_id=facility_id,
            skip_existing=skip_existing,
            parallel_pdfs=False,
        )
        results.append(row)
    return results
