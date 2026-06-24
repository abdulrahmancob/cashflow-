from collections.abc import AsyncIterator
from typing import Any

from playwright.async_api import BrowserContext

from auth import SessionState, ajax_headers
from config import GET_PATIENTS_URL, PATIENT_DISPLAY_URL, WebPTConfig
from logging_config import get_logger

log = get_logger("patient_api")


def _patient_display_name(row: dict[str, Any]) -> str:
    first = (row.get("FirstName") or "").strip()
    last = (row.get("LastName") or "").strip()
    return f"{last}, {first}".strip(", ")


async def fetch_patients_page(
    context: BrowserContext,
    *,
    start: int,
    limit: int,
    total: int,
    config: WebPTConfig,
    session: SessionState,
    patient_name: str = "",
    patient_name_given: str = "",
    patient_name_family: str = "",
) -> dict[str, Any]:
    form = {
        "start": str(start),
        "limit": str(limit),
        "total": str(total),
        "userId": "",
        "patientName": patient_name,
        "patientNameGiven": patient_name_given,
        "patientNameFamily": patient_name_family,
        "birthDate": "",
        "physicianName": "",
        "payerName": "",
        "payerType": "",
        "patientStatus": "A",
        "caseStatus": "",
        "searchType": "1",
        "changeSearchType": "0",
        "identificationNumber": "",
        "expiredType": "",
        "filter": "",
    }
    headers = ajax_headers(session.csrf_token, PATIENT_DISPLAY_URL)
    response = await context.request.post(
        GET_PATIENTS_URL, form=form, headers=headers
    )
    if not response.ok:
        text = await response.text()
        raise RuntimeError(f"getpatients failed HTTP {response.status}: {text[:200]}")
    return await response.json()


async def iter_all_patients(
    context: BrowserContext,
    *,
    config: WebPTConfig,
    session: SessionState,
    page_size: int | None = None,
    patient_name: str = "",
    max_patients: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator over all patients in the current clinic."""
    limit = page_size or config.patient_page_size
    start = 0
    total = 0
    yielded = 0

    while True:
        body = await fetch_patients_page(
            context,
            start=start,
            limit=limit,
            total=total,
            config=config,
            session=session,
            patient_name=patient_name,
        )
        total = int(body.get("total") or 0)
        rows = body.get("data") or []
        if not rows:
            break

        for row in rows:
            yield row
            yielded += 1
            if max_patients is not None and yielded >= max_patients:
                return

        start += limit
        if start >= total:
            break

    log.info("Listed %d patients (total reported: %d)", yielded, total)
