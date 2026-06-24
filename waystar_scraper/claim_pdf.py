import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Error as PlaywrightError

from auth import extend_session
from config import CLAIMS_LISTING_URL, DEFAULT_PDF_TIMEOUT_SEC, PDF_EXTEND_SESSION_EVERY, VIEW_CLAIM_PDF_URL
from human import HumanSettings
from logging_config import get_logger

log = get_logger("pdf")

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def build_pdf_url(
    *,
    claim_id: str,
    instance_id: str,
    app_id: str = "1",
    form: str = "CMS1500_0212",
) -> str:
    query = urlencode(
        {
            "appId": app_id,
            "claimId": claim_id,
            "instanceId": instance_id,
            "form": form,
        }
    )
    return f"{VIEW_CLAIM_PDF_URL}?{query}"


def pdf_filename(claim: dict[str, Any]) -> str:
    claim_number = claim.get("claim_number") or "unknown"
    claim_id = claim.get("claim_id") or "unknown"
    safe_number = _INVALID_FILENAME.sub("_", claim_number.strip())
    safe_id = _INVALID_FILENAME.sub("_", str(claim_id).strip())
    return f"{safe_number}_{safe_id}.pdf"


def relative_pdf_path(run_id: str, filename: str) -> str:
    return f"pdfs/{run_id}/{filename}".replace("\\", "/")


async def download_claim_pdf(
    page,
    claim: dict[str, Any],
    dest_dir: Path,
    *,
    app_id: str,
    form: str,
    skip_existing: bool,
    pdf_run_id: str,
    pdf_timeout_sec: float = DEFAULT_PDF_TIMEOUT_SEC,
) -> dict[str, Any]:
    claim_id = claim.get("claim_id", "")
    instance_id = claim.get("instance_id", "")

    if not claim_id or not instance_id:
        claim["pdf_path"] = ""
        claim["pdf_downloaded"] = False
        claim["pdf_error"] = "missing claim_id or instance_id"
        log.warning("Skipping PDF — missing IDs for claim %s", claim.get("claim_number"))
        return claim

    filename = pdf_filename(claim)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = dest_dir / filename
    rel_path = relative_pdf_path(pdf_run_id, filename)

    if skip_existing and file_path.exists() and file_path.stat().st_size > 0:
        claim["pdf_path"] = rel_path
        claim["pdf_downloaded"] = True
        claim["pdf_error"] = None
        log.debug("Skipped existing PDF: %s", filename)
        return claim

    url = build_pdf_url(
        claim_id=claim_id,
        instance_id=instance_id,
        app_id=app_id,
        form=form,
    )
    log.debug("GET %s", url)

    timeout_ms = int(pdf_timeout_sec * 1000)

    async def _fetch() -> tuple[Any, str, bytes]:
        response = await page.request.get(
            url,
            headers={
                "Referer": CLAIMS_LISTING_URL,
                "Accept": "application/pdf,*/*",
            },
            timeout=timeout_ms,
        )
        content_type = (response.headers.get("content-type") or "").lower()
        body = await response.body()
        return response, content_type, body

    try:
        response, content_type, body = await asyncio.wait_for(
            _fetch(),
            timeout=pdf_timeout_sec + 5,
        )
    except (PlaywrightError, asyncio.TimeoutError) as exc:
        claim["pdf_path"] = ""
        claim["pdf_downloaded"] = False
        claim["pdf_error"] = str(exc).split("\n", maxsplit=1)[0]
        log.warning("PDF failed for claim %s: %s", claim_id, claim["pdf_error"])
        return claim

    if not response.ok:
        claim["pdf_path"] = ""
        claim["pdf_downloaded"] = False
        claim["pdf_error"] = f"HTTP {response.status}"
        log.warning(
            "PDF failed for claim %s: HTTP %s",
            claim_id,
            response.status,
        )
        return claim

    is_pdf = "application/pdf" in content_type or body[:4] == b"%PDF"
    body_text_lower = body[:500].lower()
    if not is_pdf or b"waystar log off" in body_text_lower or b"<html" in body_text_lower[:200]:
        claim["pdf_path"] = ""
        claim["pdf_downloaded"] = False
        claim["pdf_error"] = f"not a PDF (content-type={content_type or 'unknown'})"
        log.warning(
            "PDF failed for claim %s: expected PDF, got %s",
            claim_id,
            content_type or "unknown",
        )
        return claim

    file_path.write_bytes(body)
    claim["pdf_path"] = rel_path
    claim["pdf_downloaded"] = True
    claim["pdf_error"] = None
    size_kb = len(body) / 1024
    log.info("Downloaded %s (%.1f KB)", filename, size_kb)
    return claim


async def download_pdfs_for_claims(
    page,
    claims: list[dict[str, Any]],
    dest_dir: Path,
    human: HumanSettings | None,
    *,
    app_id: str,
    form: str,
    pdf_delay_sec: float,
    skip_existing: bool,
    pdf_run_id: str,
    page_number: int | None = None,
    pdf_timeout_sec: float = DEFAULT_PDF_TIMEOUT_SEC,
) -> tuple[int, int]:
    ok_count = 0
    fail_count = 0

    for index, claim in enumerate(claims):
        if index > 0 and index % PDF_EXTEND_SESSION_EVERY == 0:
            await extend_session(page)

        if human and index > 0:
            await asyncio.sleep(pdf_delay_sec)
        elif index > 0 and pdf_delay_sec > 0:
            await asyncio.sleep(pdf_delay_sec)

        await download_claim_pdf(
            page,
            claim,
            dest_dir,
            app_id=app_id,
            form=form,
            skip_existing=skip_existing,
            pdf_run_id=pdf_run_id,
            pdf_timeout_sec=pdf_timeout_sec,
        )
        if claim.get("pdf_downloaded"):
            ok_count += 1
        else:
            fail_count += 1

    page_label = f"Page {page_number}: " if page_number is not None else ""
    log.info(
        "%s%s/%s PDFs OK, %s failed",
        page_label,
        ok_count,
        len(claims),
        fail_count,
    )
    return ok_count, fail_count
