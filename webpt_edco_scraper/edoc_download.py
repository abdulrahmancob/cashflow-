import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import BrowserContext, Page

from config import BASE_URL, VIEW_EXT_DOC_URL, WebPTConfig
from logging_config import get_logger
from pdf_throttle import pdf_download_slot

log = get_logger("edoc_download")

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = fallback
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned


def build_view_url(*, ext_doc_id: int, patient_id: int, uri: str) -> str:
    query = urlencode({"EDID": ext_doc_id, "PID": patient_id, "URI": uri})
    return f"{VIEW_EXT_DOC_URL}?{query}"


async def download_edoc_pdf(
    context: BrowserContext,
    *,
    doc: dict[str, Any],
    patient_id: int,
    dest_dir: Path,
    config: WebPTConfig,
    skip_existing: bool = True,
    page: Page | None = None,
) -> dict[str, Any]:
    ext_doc_id = doc.get("ExtDocID")
    uri = doc.get("URI") or ""
    user_name = doc.get("UserDefName") or ""

    result: dict[str, Any] = {
        "ext_doc_id": ext_doc_id,
        "patient_id": patient_id,
        "uri": uri,
        "filename": "",
        "path": "",
        "downloaded": False,
        "error": None,
        "skipped": False,
    }

    if not ext_doc_id or not uri:
        result["error"] = "missing ExtDocID or URI"
        return result

    fallback = f"{ext_doc_id}_{uri}"
    filename = sanitize_filename(user_name, fallback)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = dest_dir / filename
    result["filename"] = filename

    if skip_existing and file_path.exists() and file_path.stat().st_size > 0:
        result["path"] = str(file_path)
        result["downloaded"] = True
        result["skipped"] = True
        log.debug("Skipped existing: %s", filename)
        return result

    url = build_view_url(ext_doc_id=int(ext_doc_id), patient_id=patient_id, uri=uri)
    timeout_ms = int(config.pdf_timeout_sec * 1000)

    try:
        referer = f"{BASE_URL}/patientExtDoc.php?ID={patient_id}"
        async with pdf_download_slot():
            if page is not None:
                response = await page.goto(
                    url,
                    wait_until="commit",
                    timeout=timeout_ms,
                    referer=referer,
                )
            else:
                response = await context.request.get(
                    url,
                    headers={
                        "Referer": referer,
                        "Accept": "application/pdf,*/*",
                    },
                    timeout=timeout_ms,
                )
        if response is None:
            result["error"] = "no response from navigation"
            return result
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
        log.info("Downloaded %s (%d bytes)", filename, len(body))
    except Exception as exc:
        result["error"] = str(exc)
        log.warning("Failed to download %s: %s", filename, exc)

    return result


async def download_patient_edocs(
    context: BrowserContext,
    *,
    docs: list[dict[str, Any]],
    patient_id: int,
    output_dir: Path,
    config: WebPTConfig,
    skip_existing: bool = True,
    page: Page | None = None,
) -> list[dict[str, Any]]:
    patient_dir = output_dir / str(patient_id)
    results: list[dict[str, Any]] = []
    for doc in docs:
        row = await download_edoc_pdf(
            context,
            doc=doc,
            patient_id=patient_id,
            dest_dir=patient_dir,
            config=config,
            skip_existing=skip_existing,
            page=page,
        )
        results.append(row)
        if config.pdf_delay_sec > 0:
            await asyncio.sleep(config.pdf_delay_sec)
    return results
