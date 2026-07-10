"""Navigation and API helpers for denials.zirmed.com Workcenter."""

import json
from typing import Any

from playwright.async_api import Page

from auth import MFA_URL_FRAGMENT, ensure_mfa_cleared
from config import (
    CATCH_ALL_WORKGROUP_ID,
    CATCH_ALL_WORKGROUP_NAME,
    DENIALS_PER_PAGE,
    DENIALS_WORKCENTER_URL,
    WORKCENTER_ENTRY_URL,
    WaystarConfig,
)
from human import HumanSettings, human_pause
from logging_config import get_logger
from network_retry import RateLimitError, retry_rate_limited

log = get_logger("denials_nav")

DENIALS_BASE = "https://denials.zirmed.com"
CLEAR_FILTERS_URL = f"{DENIALS_BASE}/Workcenter/ClearFilters"
CHANGE_WORKGROUP_URL = f"{DENIALS_BASE}/Workcenter/ChangeWorkgroup"
SAVE_ARGS_URL = f"{DENIALS_BASE}/Workcenter/SaveWorkCenterArgsInSession"
RELOAD_GRID_URL = f"{DENIALS_BASE}/Workcenter/ReloadWorkcenterGrid"
RELOAD_GRID_COUNT_URL = f"{DENIALS_BASE}/Workcenter/ReloadWorkcenterGridCount"
PAGE_AND_SORT_URL = f"{DENIALS_BASE}/Workcenter/PageAndSortGrid"
JSON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": DENIALS_WORKCENTER_URL,
}

WORKGROUP_ALIASES = {
    "catch-all": CATCH_ALL_WORKGROUP_ID,
    "catchall": CATCH_ALL_WORKGROUP_ID,
    "all": CATCH_ALL_WORKGROUP_ID,
}

WORKGROUP_NAMES = {
    CATCH_ALL_WORKGROUP_ID: CATCH_ALL_WORKGROUP_NAME,
}


def resolve_workgroup_id(workgroup: str | None) -> str | None:
    """Return workgroup ID string, or None to keep session default."""
    if not workgroup or workgroup.lower() == "current":
        return None
    key = workgroup.lower().strip()
    if key in WORKGROUP_ALIASES:
        return WORKGROUP_ALIASES[key]
    if workgroup.isdigit():
        return workgroup
    raise ValueError(f"Unknown workgroup: {workgroup!r}. Use catch-all, current, or a numeric ID.")


def parse_workflow_stages(value: str | None) -> list[str]:
    if not value or value.lower().strip() == "all":
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def build_workcenter_search_args(
    *,
    workgroup_id: str,
    workgroup_name: str | None = None,
    workflow_stages: list[str] | None = None,
    denial_date_from: str = "",
    denial_date_to: str = "",
    reminder_due_only: bool = False,
    page_size: int = DENIALS_PER_PAGE,
    page_index: int = 1,
    sort_column: str = "DenialDate",
    sort_direction: str = "Ascending",
) -> dict[str, Any]:
    return {
        "WorkgroupID": workgroup_id,
        "WorkgroupName": workgroup_name or WORKGROUP_NAMES.get(workgroup_id, ""),
        "WorkcenterStages": workflow_stages or [],
        "CategoryIDs": [],
        "PatientName": "",
        "ClaimNumber": "",
        "DenialID": "",
        "PayerIDs": [],
        "Payer": "",
        "AccountIDs": [],
        "Account": "",
        "WorkcenterGridArgs": {
            "SortColumn": sort_column,
            "SortDirection": sort_direction,
            "PageIndex": str(page_index),
            "PageSize": str(page_size),
        },
        "ReminderDateDueOrPastDue": reminder_due_only,
        "FilterPriorityDenials": False,
        "FilterEligibilityResponse": False,
        "FilterCoverageDetectionResponse": False,
        "ProfessionalClaimType": False,
        "InstitutionalClaimType": False,
        "LinkedDenials": False,
        "UnlinkedDenials": False,
        "DeniedAmountMin": "",
        "DeniedAmountMax": "",
        "DenialDateFrom": denial_date_from,
        "DenialDateTo": denial_date_to,
        "ServiceDateFrom": "",
        "ServiceDateTo": "",
        "BillingProviders": [],
        "RenderingProviders": [],
        "CarcCodes": [],
        "RarcCodes": [],
        "IsAdvancedSearchModal": False,
    }


async def navigate_to_denials_workcenter(
    page: Page,
    human: HumanSettings | None = None,
    *,
    config: WaystarConfig | None = None,
) -> None:
    """Enter via WorkCenter tab then open Denials Workcenter (shared SSO session)."""
    await page.goto(WORKCENTER_ENTRY_URL, wait_until="domcontentloaded", timeout=120_000)
    if config and human and MFA_URL_FRAGMENT in page.url:
        await ensure_mfa_cleared(page, config, human)
    if human:
        await human_pause(human)

    await page.goto(DENIALS_WORKCENTER_URL, wait_until="domcontentloaded", timeout=120_000)
    if config and human and MFA_URL_FRAGMENT in page.url:
        await ensure_mfa_cleared(page, config, human)
        await page.goto(DENIALS_WORKCENTER_URL, wait_until="domcontentloaded", timeout=120_000)

    if "denials.zirmed.com" not in page.url:
        raise RuntimeError(f"Failed to reach denials workcenter: {page.url}")

    log.info("Denials workcenter ready | url=%s", page.url)


async def post_denials(
    page: Page,
    path: str,
    *,
    json_body: dict | None = None,
) -> str:
    url = f"{DENIALS_BASE}{path}" if path.startswith("/") else path

    async def _post() -> str:
        if json_body is not None:
            response = await page.request.post(
                url,
                data=json.dumps(json_body),
                headers=JSON_HEADERS,
            )
        else:
            response = await page.request.post(url, headers=JSON_HEADERS)
        if response.status in {403, 429, 503}:
            text = await response.text()
            raise RateLimitError(response.status, text[:300])
        if not response.ok:
            text = await response.text()
            raise RuntimeError(f"Denials POST {path} failed ({response.status}): {text[:300]}")
        return await response.text()

    return await retry_rate_limited(_post, label=f"POST {path}")


async def change_workgroup(page: Page, workgroup_id: str) -> str:
    await post_denials(
        page,
        CHANGE_WORKGROUP_URL,
        json_body={"workgroupID": workgroup_id},
    )
    grid_html = await reload_workcenter_grid(page)
    log.info("Switched workgroup to %s", workgroup_id)
    return grid_html


async def clear_workcenter_filters(page: Page) -> None:
    await post_denials(page, CLEAR_FILTERS_URL, json_body={})
    log.info("Cleared workcenter filters")


async def apply_workcenter_search(page: Page, args: dict[str, Any]) -> str:
    await post_denials(page, SAVE_ARGS_URL, json_body={"args": args})
    grid_html = await reload_workcenter_grid(page)
    log.info(
        "Applied workcenter search: workgroup=%s stages=%s denial_dates=%s–%s reminder_due=%s",
        args.get("WorkgroupID"),
        args.get("WorkcenterStages") or "all",
        args.get("DenialDateFrom") or "*",
        args.get("DenialDateTo") or "*",
        args.get("ReminderDateDueOrPastDue"),
    )
    return grid_html


async def setup_full_search(
    page: Page,
    *,
    workgroup_id: str | None = None,
    denial_date_from: str = "",
    denial_date_to: str = "",
    workflow_stages: list[str] | None = None,
    reminder_due_only: bool = False,
    page_size: int = DENIALS_PER_PAGE,
    clear_first: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Configure workgroup + filters, return grid HTML and count info."""
    if workgroup_id:
        await change_workgroup(page, workgroup_id)
    elif clear_first:
        await clear_workcenter_filters(page)

    effective_workgroup = workgroup_id
    if not effective_workgroup:
        try:
            selected = await page.locator("#WorkcenterArgs_WorkgroupID").input_value()
            if selected:
                effective_workgroup = selected
        except Exception:
            pass
    if not effective_workgroup:
        raise RuntimeError("Could not determine workgroup ID — pass --workgroup catch-all or a numeric ID")

    search_args = build_workcenter_search_args(
        workgroup_id=effective_workgroup,
        workflow_stages=workflow_stages,
        denial_date_from=denial_date_from,
        denial_date_to=denial_date_to,
        reminder_due_only=reminder_due_only,
        page_size=page_size,
    )
    grid_html = await apply_workcenter_search(page, search_args)
    count_info = await get_workcenter_grid_count(page)
    log.info(
        "Search ready: %s records, %s pages (workgroup=%s)",
        count_info.get("RecordCount"),
        count_info.get("PageCount"),
        effective_workgroup,
    )
    return grid_html, count_info


async def reload_workcenter_grid(page: Page) -> str:
    return await post_denials(page, RELOAD_GRID_URL)


async def get_workcenter_grid_count(page: Page) -> dict[str, Any]:
    raw = await post_denials(page, RELOAD_GRID_COUNT_URL)
    return json.loads(raw)


async def fetch_workcenter_page(
    page: Page,
    *,
    page_index: int = 1,
    page_size: int = 50,
    sort_column: str = "DenialDate",
    sort_direction: str = "Ascending",
) -> str:
    body = {
        "gridArgs": {
            "SortColumn": sort_column,
            "SortDirection": sort_direction,
            "PageIndex": str(page_index),
            "PageSize": str(page_size),
        }
    }
    return await post_denials(page, PAGE_AND_SORT_URL, json_body=body)
