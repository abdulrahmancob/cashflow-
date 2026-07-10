"""Fetch and parse Waystar denial Review page remit/line detail."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Page

from config import DENIALS_REVIEW_URL_TEMPLATE, DENIALS_WORKCENTER_URL
from denials_normalize import extract_carc_codes, extract_rarc_codes
from human import HumanSettings, human_pause
from logging_config import get_logger
from network_retry import RateLimitError, retry_rate_limited

log = get_logger("denial_details")

CODE_SEPARATOR = " || "
REMIT_RECEIVED_RE = re.compile(
    r"received from (.+?), on (\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
REMIT_NUMBER_RE = re.compile(r"Remit\s+(\S+)", re.IGNORECASE)


def denial_review_url(denial_id: str) -> str:
    return DENIALS_REVIEW_URL_TEMPLATE.format(denial_id=denial_id)


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_money(value: str) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    negative = "(" in raw and ")" in raw
    cleaned = re.sub(r"[^0-9.]+", "", raw)
    try:
        amount = float(cleaned or 0)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def _adj_code_text(container) -> tuple[str, str]:
    if container is None:
        return "", ""
    classes = container.get("class") or []
    code_el = container if "adjCode" in classes else container.select_one(".adjCode")
    if code_el is None:
        text = _clean(container.get_text())
        return text, ""
    code_parts: list[str] = []
    for child in code_el.children:
        if getattr(child, "name", None) == "div":
            break
        if isinstance(child, str) and child.strip():
            code_parts.append(child.strip())
    code = _clean("".join(code_parts)) if code_parts else _clean(code_el.get_text())
    tooltip = code_el.select_one(".toolTipInner")
    description = ""
    if tooltip:
        label = tooltip.select_one(".label")
        full = _clean(tooltip.get_text(" ", strip=True))
        if label:
            label_text = _clean(label.get_text())
            description = full.replace(label_text, "", 1).strip(" :")
        else:
            description = full
    return code, description


def _parse_resolution(status_cell) -> dict[str, str]:
    result = {
        "adjustment_status": "",
        "resolution_action": "",
        "resolution_date": "",
        "resolution_rule": "",
        "resolution_payer": "",
    }
    if status_cell is None:
        return result

    plain = _clean(status_cell.get_text())
    if plain in {"Active", "Inactive"}:
        result["adjustment_status"] = plain
        return result

    tooltip = status_cell.select_one(".toolTipInner")
    if tooltip is None:
        result["adjustment_status"] = plain
        return result

    result["adjustment_status"] = "Closed"
    for br in tooltip.find_all("br"):
        br.replace_with("\n")
    lines = [_clean(line) for line in tooltip.get_text().splitlines() if _clean(line)]
    if lines:
        result["resolution_action"] = lines[0]
    for i, line in enumerate(lines):
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", line):
            result["resolution_date"] = line
        if line.startswith("Automatic Rule:"):
            parts = lines[i : i + 3]
            result["resolution_rule"] = " | ".join(parts)
            for part in parts:
                if part.startswith("Payer:"):
                    result["resolution_payer"] = part.replace("Payer:", "", 1).strip()
        if line.startswith("Manual:"):
            result["resolution_rule"] = line
    return result


@dataclass
class DenialLine:
    denial_id: str = ""
    overview_active_remit_count: int = 0
    remit_id: str = ""
    remit_number: str = ""
    remit_instance_id: str = ""
    remit_active: bool = True
    remit_received_date: str = ""
    remit_payer: str = ""
    line_num: int = 0
    proc_code: str = ""
    modifiers: str = ""
    remark_codes: str = ""
    remark_descriptions: str = ""
    billed_amount: float = 0.0
    allowed_amount: float = 0.0
    paid_amount: float = 0.0
    carc_codes: str = ""
    carc_descriptions: str = ""
    adjustment_amount: float = 0.0
    adjustment_status: str = ""
    resolution_action: str = ""
    resolution_date: str = ""
    resolution_rule: str = ""
    resolution_payer: str = ""


def _parse_remit_lines(
    table,
    *,
    denial_id: str,
    remit_meta: dict[str, str],
    overview_active_remit_count: int,
) -> list[DenialLine]:
    lines: list[DenialLine] = []
    for tr in table.select("tbody tr"):
        cells = tr.select("td")
        if len(cells) < 10:
            continue
        line_num_text = _clean(cells[0].get_text())
        try:
            line_num = int(line_num_text)
        except ValueError:
            continue

        remark_codes: list[str] = []
        remark_descs: list[str] = []
        for remark_el in cells[3].select(".adjCode"):
            code, desc = _adj_code_text(remark_el)
            for rarc in extract_rarc_codes(code) or ([code] if code and not desc else []):
                if rarc and rarc not in remark_codes:
                    remark_codes.append(rarc)
            if desc and desc not in remark_descs:
                remark_descs.append(desc)

        amount_cells = [c for c in cells if "rightAlignCol" in (c.get("class") or [])]
        billed = parse_money(_clean(amount_cells[0].get_text())) if len(amount_cells) > 0 else 0.0
        allowed = parse_money(_clean(amount_cells[1].get_text())) if len(amount_cells) > 1 else 0.0
        paid = parse_money(_clean(amount_cells[2].get_text())) if len(amount_cells) > 2 else 0.0

        carc_cell = tr.select_one("td.remitRightTableStart")
        adj_amount_cell = tr.select_one("td.remitRightTable")
        status_cell = tr.select_one("td.remitRightTableEnd")

        carc_code, carc_desc = _adj_code_text(carc_cell)
        carc_list = extract_carc_codes(carc_code)
        carc_code = carc_list[0] if carc_list else carc_code
        resolution = _parse_resolution(status_cell)

        lines.append(
            DenialLine(
                denial_id=denial_id,
                overview_active_remit_count=overview_active_remit_count,
                remit_id=remit_meta.get("remit_id", ""),
                remit_number=remit_meta.get("remit_number", ""),
                remit_instance_id=remit_meta.get("remit_instance_id", ""),
                remit_active=remit_meta.get("remit_active", True),
                remit_received_date=remit_meta.get("remit_received_date", ""),
                remit_payer=remit_meta.get("remit_payer", ""),
                line_num=line_num,
                proc_code=_clean(cells[1].get_text()),
                modifiers=_clean(cells[2].get_text()),
                remark_codes=CODE_SEPARATOR.join(remark_codes),
                remark_descriptions=CODE_SEPARATOR.join(remark_descs),
                billed_amount=billed,
                allowed_amount=allowed,
                paid_amount=paid,
                carc_codes=carc_code,
                carc_descriptions=carc_desc,
                adjustment_amount=parse_money(_clean(adj_amount_cell.get_text()) if adj_amount_cell else ""),
                adjustment_status=resolution.get("adjustment_status", ""),
                resolution_action=resolution.get("resolution_action", ""),
                resolution_date=resolution.get("resolution_date", ""),
                resolution_rule=resolution.get("resolution_rule", ""),
                resolution_payer=resolution.get("resolution_payer", ""),
            )
        )
    return lines


def parse_denial_review(html: str, denial_id: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#remitsContainer")
    overview_count = 0
    overview_el = soup.select_one("#overviewRemits")
    if overview_el:
        match = re.search(r"(\d+)\s+Remit", overview_el.get_text(), re.I)
        if match:
            overview_count = int(match.group(1))

    parsed_id = denial_id
    if not parsed_id:
        link = soup.select_one("a.historyRemitLink")
        if link:
            parsed_id = link.get("data-denialid", "")

    all_lines: list[DenialLine] = []
    if container is None:
        return {
            "denial_id": parsed_id,
            "overview_active_remit_count": overview_count,
            "line_count": 0,
            "lines": [],
            "found_remits": False,
        }

    # Walk remit links and following table containers in document order.
    for link in container.select("a.historyRemitLink"):
        remit_id = link.get("data-remitidkey", "")
        remit_number_match = REMIT_NUMBER_RE.search(_clean(link.get_text()))
        remit_number = remit_number_match.group(1) if remit_number_match else ""

        received_text = ""
        table_container = None
        sibling = link.next_sibling
        while sibling is not None:
            if getattr(sibling, "name", None) == "div" and sibling.get("data-instanceid"):
                table_container = sibling
                break
            if isinstance(sibling, str):
                received_text += sibling
            elif getattr(sibling, "name", None) is not None:
                received_text += sibling.get_text()
            sibling = sibling.next_sibling

        received_match = REMIT_RECEIVED_RE.search(received_text)
        remit_payer = received_match.group(1) if received_match else ""
        remit_date = received_match.group(2) if received_match else ""

        if table_container is None:
            continue

        classes = table_container.get("class") or []
        remit_active = not any("inactiveInstance" in c for c in classes)
        table = table_container.select_one("table.remitTable")
        if table is None:
            continue

        meta = {
            "remit_id": remit_id,
            "remit_number": remit_number,
            "remit_instance_id": table_container.get("data-instanceid", ""),
            "remit_active": remit_active,
            "remit_received_date": remit_date,
            "remit_payer": remit_payer,
        }
        all_lines.extend(
            _parse_remit_lines(
                table,
                denial_id=parsed_id,
                remit_meta=meta,
                overview_active_remit_count=overview_count,
            )
        )

    return {
        "denial_id": parsed_id,
        "overview_active_remit_count": overview_count,
        "line_count": len(all_lines),
        "lines": [asdict(line) for line in all_lines],
        "found_remits": bool(all_lines),
    }


async def fetch_denial_review(
    page: Page,
    denial_id: str,
    *,
    human: HumanSettings | None = None,
) -> dict[str, Any]:
    url = denial_review_url(denial_id)
    if human:
        await human_pause(human, 0.5)

    async def _goto() -> None:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120_000,
            referer=DENIALS_WORKCENTER_URL,
        )
        if response and response.status in {403, 429, 503}:
            raise RateLimitError(response.status)

    try:
        await retry_rate_limited(_goto, label=f"review goto {denial_id}")
        if human:
            await human_pause(human, 0.4)
        html = await page.content()
        final_url = page.url.lower()
    except Exception as exc:
        log.debug("goto failed for denial %s (%s) — falling back to request.get", denial_id, exc)

        async def _get():
            response = await page.request.get(
                url,
                headers={"Referer": DENIALS_WORKCENTER_URL},
            )
            if response.status in {403, 429, 503}:
                raise RateLimitError(response.status, (await response.text())[:300])
            return response

        response = await retry_rate_limited(_get, label=f"review GET {denial_id}")
        if not response.ok:
            return {
                "denial_id": denial_id,
                "error": f"HTTP {response.status}",
                "line_count": 0,
                "lines": [],
                "found_remits": False,
            }
        html = await response.text()
        final_url = (response.url or "").lower()

    if "login.zirmed.com" in final_url:
        return {
            "denial_id": denial_id,
            "error": "session expired",
            "line_count": 0,
            "lines": [],
            "found_remits": False,
        }
    result = parse_denial_review(html, denial_id=denial_id)
    result["review_url"] = url
    return result
