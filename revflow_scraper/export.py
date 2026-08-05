"""Browser-based EOB export (spreadsheet download)."""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import APIRequestContext, Page, TimeoutError as PlaywrightTimeoutError

from auth import SessionExpiredError, assert_authenticated_page
from config import API_BASE_URL, EOB_DETAIL_REPORT_ID, RevFlowConfig
from logging_config import get_logger
from reports_api import ReportParams

log = get_logger("export")

EXPORT_BUTTON = "#export_report_button"


def sanitize_filename_part(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value or "")
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned[:max_len] or "unknown"


def _sanitize_filename_display(value: str, *, max_len: int = 150) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned.strip())
    return cleaned[:max_len].rstrip(" ") or "unknown"


def legacy_export_filename(selection: dict, suggested_ext: str = ".csv") -> str:
    """Pre-eob_key naming: {payor} - {check_num}.csv (may collide across EOBs)."""
    payor = _sanitize_filename_display(selection.get("payor", "unknown"), max_len=150)
    check_num = _sanitize_filename_display(selection.get("check_eft_num", "check"), max_len=80)
    ext = suggested_ext if suggested_ext.startswith(".") else f".{suggested_ext}"
    return f"{payor} - {check_num}{ext}"


def export_filename(selection: dict, suggested_ext: str = ".csv") -> str:
    payor = _sanitize_filename_display(selection.get("payor", "unknown"), max_len=150)
    check_num = _sanitize_filename_display(selection.get("check_eft_num", "check"), max_len=80)
    eob_key = _sanitize_filename_display(selection.get("eob_key", ""), max_len=40)
    ext = suggested_ext if suggested_ext.startswith(".") else f".{suggested_ext}"
    if eob_key:
        return f"{payor} - {check_num} - {eob_key}{ext}"
    return f"{payor} - {check_num}{ext}"


def export_file_exists(exports_dir: Path, selection: dict, *, include_legacy: bool = False) -> Path | None:
    """Return path if a matching export exists."""
    for ext in (".csv", ".xlsx"):
        candidate = exports_dir / export_filename(selection, ext)
        if candidate.exists():
            return candidate
        if include_legacy:
            legacy = exports_dir / legacy_export_filename(selection, ext)
            if legacy.exists():
                return legacy
    return None


def selection_key(selection: dict) -> str:
    return "|".join(
        [
            str(selection.get("company_id", "")),
            str(selection.get("eob_key", "")),
            str(selection.get("check_eft_num", "")),
            str(selection.get("eob_date", "")),
        ]
    )


def build_detail_params(selection: dict) -> ReportParams:
    return ReportParams(
        rid=str(selection.get("detail_rid") or EOB_DETAIL_REPORT_ID),
        from_date=selection["from_date"],
        to_date=selection["to_date"],
        clinic_code=selection.get("clinic_code", "PV4"),
        company_id=str(selection.get("company_id", "")),
        eob_key=str(selection.get("eob_key", "")),
        check_eft_num=str(selection.get("check_eft_num", "")),
        payor=str(selection.get("payor", "")),
        eob_date=str(selection.get("eob_date", "")),
    )


async def post_billing_audit(
    request: APIRequestContext,
    bearer_token: str,
    *,
    report_id: str,
    user_id: str,
    company_id: str,
    record_count: int = 0,
    context_label: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    label = context_label or f"{report_id} - Electronic EOB Detail - Export"
    report_data_entry = json.dumps(
        {
            "context": label,
            "user_id": int(user_id) if str(user_id).isdigit() else user_id,
            "company_id": int(company_id) if str(company_id).isdigit() else company_id,
            "entity_type": "a_rpt_ver",
            "event_action": "Export",
            "event_date_time": now,
            "event_meta_data": {"Records": record_count},
            "source_id": "billing",
        },
        separators=(",", ":"),
    )
    body = {
        "reportId": str(report_id),
        "reportEntityTypeId": 10,
        "reportEventActionId": 3,
        "reportDataEntry": report_data_entry,
    }
    url = f"{API_BASE_URL}/v1/reports/billing_audit"
    resp = await request.post(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
    )
    if not resp.ok:
        log.warning("billing_audit returned %s: %s", resp.status, await resp.text())
    else:
        log.debug("billing_audit OK for report %s", report_id)


async def export_eob_spreadsheet(
    page: Page,
    request: APIRequestContext,
    config: RevFlowConfig,
    bearer_token: str,
    user_id: str,
    company_id: str,
    selection: dict,
    output_dir: Path,
    *,
    skip_existing: bool = True,
    record_count: int = 0,
) -> dict:
    params = build_detail_params(selection)
    detail_url = params.ui_url()
    key = selection_key(selection)

    output_dir.mkdir(parents=True, exist_ok=True)

    if skip_existing:
        existing = export_file_exists(output_dir, selection, include_legacy=False)
        if existing is not None:
            log.info("Skipping existing export: %s", existing.name)
            return {
                "key": key,
                "status": "skipped",
                "path": str(existing),
                "selection": selection,
            }

    log.info("Opening EOB detail: check=%s payor=%s", selection.get("check_eft_num"), selection.get("payor"))
    await page.goto(detail_url, wait_until="domcontentloaded", timeout=120_000)
    await assert_authenticated_page(page)
    await asyncio.sleep(config.action_delay_sec)

    export_btn = page.locator(EXPORT_BUTTON)
    button_wait_ms = int(config.export_button_wait_sec * 1000)
    try:
        await export_btn.wait_for(state="visible", timeout=button_wait_ms)
    except PlaywrightTimeoutError:
        await assert_authenticated_page(page)
        raise SessionExpiredError(
            f"Export button not found — session may have expired | check={selection.get('check_eft_num')}"
        )

    await post_billing_audit(
        request,
        bearer_token,
        report_id=str(selection.get("detail_rid") or EOB_DETAIL_REPORT_ID),
        user_id=user_id,
        company_id=company_id or str(selection.get("company_id", "")),
        record_count=record_count,
        context_label=f"{EOB_DETAIL_REPORT_ID} - Electronic EOB Detail for {selection.get('clinic_code', 'PV4')} - Export",
    )

    timeout_ms = int(config.export_timeout_sec * 1000)
    async with page.expect_download(timeout=timeout_ms) as download_info:
        await export_btn.click()

    download = await download_info.value
    ext = Path(download.suggested_filename or "").suffix or ".csv"
    target = output_dir / export_filename(selection, ext)

    await download.save_as(str(target))
    log.info("Saved export: %s", target)

    delay = config.export_delay_sec
    if config.export_delay_jitter_sec > 0:
        delay += random.uniform(0, config.export_delay_jitter_sec)
    await asyncio.sleep(delay)

    return {
        "key": key,
        "status": "ok",
        "path": str(target),
        "selection": selection,
    }
