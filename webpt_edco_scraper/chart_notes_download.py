import asyncio
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext

from chart_notes_api import ChartNoteRef, build_print_pdf_url, patient_chart_note_url
from config import BASE_URL, WebPTConfig
from edoc_download import sanitize_filename
from logging_config import get_logger

log = get_logger("chart_notes_download")

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

    if skip_existing and file_path.exists() and file_path.stat().st_size > 0:
        result["path"] = str(file_path)
        result["downloaded"] = True
        result["skipped"] = True
        log.debug("Skipped existing chart note: %s", filename)
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

    try:
        response = await context.request.get(
            url,
            headers={
                "Referer": referer,
                "Accept": "application/pdf,*/*",
            },
            timeout=timeout_ms,
        )
        if not response.ok:
            result["error"] = f"HTTP {response.status}"
            return result

        body = await response.body()
        content_type = (response.headers.get("content-type") or "").lower()
        if not body:
            result["error"] = "empty response"
            return result
        if "pdf" not in content_type and not body.startswith(b"%PDF"):
            result["error"] = f"not a PDF (content-type={content_type})"
            return result

        file_path.write_bytes(body)
        result["path"] = str(file_path)
        result["downloaded"] = True
        log.info("Downloaded chart note %s (%d bytes)", filename, len(body))
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("Failed to download chart note %s: %s", filename, exc)

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
) -> list[dict[str, Any]]:
    dest_dir = chart_notes_dir(output_dir, patient_id)
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
        )
        results.append(row)
        if config.pdf_delay_sec > 0:
            await asyncio.sleep(config.pdf_delay_sec)
    return results
