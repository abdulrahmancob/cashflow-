"""Parse Denials Workcenter grid HTML into denial summary dicts."""

import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from logging_config import get_logger

log = get_logger("denials_list")

_MONEY = re.compile(r"[^0-9.\-()]+")


def _clean(text: str) -> str:
    return " ".join(text.split())


def _cell_text(cell) -> str:
    if cell is None:
        return ""
    return _clean(cell.get_text(" ", strip=True))


def parse_money(value: str) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    negative = "(" in raw and ")" in raw
    cleaned = _MONEY.sub("", raw.replace("(", "").replace(")", ""))
    try:
        amount = float(cleaned or 0)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def _parse_service_date(text: str) -> str:
    """Normalize '03/03/26 - 03/03/26' to first date."""
    text = _clean(text)
    if " - " in text:
        return text.split(" - ", maxsplit=1)[0].strip()
    return text


def _notes_for_denial(soup: BeautifulSoup, denial_id: str) -> str:
    tag_row = soup.select_one(f'tr.tagRow[data-denialid="{denial_id}"]')
    if not tag_row:
        return ""
    note = tag_row.select_one(".lastNote")
    return _clean(note.get_text()) if note else ""


def parse_workcenter_grid(html: str) -> list[dict[str, Any]]:
    """Parse grid HTML fragment returned by ReloadWorkcenterGrid / PageAndSortGrid."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    scraped_at = datetime.now(timezone.utc).isoformat()

    for tr in soup.select("tr.gridViewRow.gridViewExpandableRow"):
        denial_id = tr.get("data-denialid", "")
        if not denial_id:
            continue

        def cell_by_prefix(prefix: str) -> str:
            cell = tr.select_one(f'td[id^="{prefix}_"]')
            return _cell_text(cell)

        row = {
            "denial_id": denial_id,
            "claim_id": tr.get("data-claimid", ""),
            "instance_id": tr.get("data-instanceid", ""),
            "claim_number": tr.get("data-claimnumber", ""),
            "remit_id_key": tr.get("data-remitidkey", ""),
            "remit_cust_id": tr.get("data-remitcustid", ""),
            "cust_id": tr.get("data-custid", ""),
            "workgroup_id": tr.get("data-workgroupid", ""),
            "claim_type": tr.get("data-claimtype", ""),
            "can_resubmit": tr.get("data-canresubmit", ""),
            "account": cell_by_prefix("AccountName"),
            "patient_name": cell_by_prefix("PatientName"),
            "pop_score": cell_by_prefix("PODScore"),
            "service_date": _parse_service_date(cell_by_prefix("ServiceDate")),
            "denial_date": cell_by_prefix("DenialDate"),
            "provider": cell_by_prefix("RenderingProvider"),
            "payer": cell_by_prefix("PayerName"),
            "denied_amount": cell_by_prefix("DeniedAmount"),
            "denial_category": cell_by_prefix("DenialCategory"),
            "reminder_date": cell_by_prefix("ReminderDate"),
            "stage": tr.get("data-stage", "") or cell_by_prefix("Stage"),
            "days_in_phase": cell_by_prefix("DaysInPhase"),
            "level_of_appeal": cell_by_prefix("LevelOfAppeal"),
            "payee_npi": cell_by_prefix("PayeeNPI"),
            "service_facility": cell_by_prefix("ServiceFacility"),
            "allowed_amount": cell_by_prefix("AllowedAmount"),
            "billed_amount": cell_by_prefix("BilledAmount"),
            "last_note": _notes_for_denial(soup, denial_id),
            "scraped_at": scraped_at,
        }
        rows.append(row)

    return rows


def parse_grid_pagination(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    total_count_el = soup.select_one("#hdnTotalCount")
    page_size_el = soup.select_one("#pageSize, #ctrl_ItemsPerPage")
    current_page_el = soup.select_one("#CurrentPageInput")
    results_el = soup.select_one("#upResultsCounter")

    return {
        "total_count": int(total_count_el.get("value", "0") if total_count_el else 0),
        "page_size": int(
            page_size_el.get("value", "10")
            if page_size_el and page_size_el.get("value")
            else (page_size_el.get_text(strip=True) if page_size_el else "10")
        ),
        "current_page": int(current_page_el.get("value", "1") if current_page_el else 1),
        "results_text": _clean(results_el.get_text()) if results_el else "",
    }
