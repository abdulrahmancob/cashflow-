import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import APIResponse, BrowserContext

from auth import AUTH_EXPIRED, is_auth_redirect_url
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


def is_auth_expired_error(error: str | None) -> bool:
    err = (error or "").strip()
    return err == AUTH_EXPIRED or err.startswith(f"{AUTH_EXPIRED}:")


def response_indicates_auth_expired(response: APIResponse) -> bool:
    if is_auth_redirect_url(response.url):
        return True
    location = (response.headers.get("location") or "").strip()
    return bool(location) and is_auth_redirect_url(location)


async def fetch_binary_with_auth_guard(
    context: BrowserContext,
    url: str,
    *,
    headers: dict[str, str],
    timeout_ms: int,
) -> APIResponse:
    """GET a document URL; fail fast on auth redirects (do not follow Auth0 chains)."""
    # One hop is enough to land on auth.webpt.com when the EMR session is dead.
    # Following further redirects burns up to pdf_timeout (~60s) on login pages.
    return await context.request.get(
        url,
        headers=headers,
        timeout=timeout_ms,
        max_redirects=1,
    )


async def download_edoc_pdf(
    context: BrowserContext,
    *,
    doc: dict[str, Any],
    patient_id: int,
    dest_dir: Path,
    config: WebPTConfig,
    skip_existing: bool = True,
    parallel_pdfs: bool = False,
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
    referer = f"{BASE_URL}/patientExtDoc.php?ID={patient_id}"

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
        # Auth HTML sometimes returned as 200 with text/html.
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
        log.info("Downloaded %s (%d bytes)", filename, len(body))

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
        log.warning("Failed to download %s: %r", filename, exc)

    return result


async def download_patient_edocs(
    context: BrowserContext,
    *,
    docs: list[dict[str, Any]],
    patient_id: int,
    output_dir: Path,
    config: WebPTConfig,
    skip_existing: bool = True,
    parallel_pdfs: bool = False,
) -> list[dict[str, Any]]:
    patient_dir = output_dir / str(patient_id)
    if parallel_pdfs and docs:
        tasks = [
            download_edoc_pdf(
                context,
                doc=doc,
                patient_id=patient_id,
                dest_dir=patient_dir,
                config=config,
                skip_existing=skip_existing,
                parallel_pdfs=True,
            )
            for doc in docs
        ]
        return list(await asyncio.gather(*tasks))

    results: list[dict[str, Any]] = []
    for doc in docs:
        row = await download_edoc_pdf(
            context,
            doc=doc,
            patient_id=patient_id,
            dest_dir=patient_dir,
            config=config,
            skip_existing=skip_existing,
            parallel_pdfs=False,
        )
        results.append(row)
    return results
