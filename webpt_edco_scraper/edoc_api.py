import asyncio
from typing import Any

from playwright.async_api import BrowserContext

from auth import SessionState, ajax_headers
from config import (
    BASE_URL,
    GET_ALL_DOCUMENTS_URL,
    GET_DOCUMENTS_PER_CASE_URL,
    WebPTConfig,
)
from logging_config import get_logger

log = get_logger("edoc_api")


async def _post_form(
    context: BrowserContext,
    url: str,
    form: dict[str, str],
    session: SessionState,
    referer: str,
    *,
    retries: int = 5,
) -> dict[str, Any]:
    headers = ajax_headers(session.csrf_token, referer)
    last_error = ""
    for attempt in range(retries):
        response = await context.request.post(url, form=form, headers=headers)
        if response.ok:
            return await response.json()
        text = await response.text()
        last_error = f"HTTP {response.status}: {text[:200]}"
        if response.status in (403, 429) and attempt < retries - 1:
            wait = 60 * (attempt + 1)
            log.warning(
                "POST %s blocked (%s) — waiting %ds before retry %d/%d",
                url,
                response.status,
                wait,
                attempt + 2,
                retries,
            )
            await asyncio.sleep(wait)
            continue
        break
    raise RuntimeError(f"POST {url} failed {last_error}")


async def get_documents_per_case(
    context: BrowserContext,
    *,
    case_id: int,
    patient_id: int | None,
    config: WebPTConfig,
    session: SessionState,
) -> list[dict[str, Any]]:
    form: dict[str, str] = {
        "case": str(case_id),
        "order": "DateFiled",
        "sort": "desc",
        "timezone": config.timezone,
    }
    if patient_id is not None:
        form["patient"] = str(patient_id)

    referer = f"{BASE_URL}/patientExtDoc.php?ID={patient_id or 0}&CaseID={case_id}"
    body = await _post_form(
        context, GET_DOCUMENTS_PER_CASE_URL, form, session, referer
    )
    data = body.get("data") or []
    log.debug("getdocumentspercase case=%s → %d docs", case_id, len(data))
    return data


async def get_all_documents(
    context: BrowserContext,
    *,
    patient_id: int,
    config: WebPTConfig,
    session: SessionState,
) -> list[dict[str, Any]]:
    form = {
        "patient": str(patient_id),
        "order": "DateFiled",
        "sort": "desc",
        "timezone": config.timezone,
    }
    referer = f"{BASE_URL}/patientExtDoc.php?ID={patient_id}"
    body = await _post_form(context, GET_ALL_DOCUMENTS_URL, form, session, referer)
    data = body.get("data") or []
    log.debug("getalldocuments patient=%s → %d docs", patient_id, len(data))
    return data


async def list_patient_edocs(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int | None,
    config: WebPTConfig,
    session: SessionState,
    include_all_cases: bool = True,
) -> list[dict[str, Any]]:
    """Return merged unique edocs for a patient."""
    seen: set[int] = set()
    docs: list[dict[str, Any]] = []

    if case_id is not None:
        for doc in await get_documents_per_case(
            context,
            case_id=case_id,
            patient_id=patient_id,
            config=config,
            session=session,
        ):
            ext_id = doc.get("ExtDocID")
            if ext_id not in seen:
                seen.add(ext_id)
                docs.append(doc)

    if include_all_cases:
        for doc in await get_all_documents(
            context, patient_id=patient_id, config=config, session=session
        ):
            ext_id = doc.get("ExtDocID")
            if ext_id not in seen:
                seen.add(ext_id)
                docs.append(doc)

    return docs
