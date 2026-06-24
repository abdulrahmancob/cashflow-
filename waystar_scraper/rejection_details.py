"""Fetch and parse per-claim rejection details from the Waystar claim editor page.

The editor page (Editor/V5010/Professional/Main.aspx) is server-rendered and
contains a RejectionGrid with each rejection message as a separate row, an
optional "How to Fix" knowledge-base link per message, and the original raw
payer message (with X12 A3:xx category/status codes) in a tooltip div.
"""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from logging_config import get_logger

log = get_logger("rejdetail")

EDITOR_URL_TEMPLATE = (
    "https://claims.zirmed.com/Editor/V5010/Professional/Main.aspx"
    "?&sec=0&draft=N&editclaimid={claim_id}&origin=ClaimListingNew&workgroupName=undefined"
)

MESSAGE_SEPARATOR = " || "

_HEADER_COUNT = re.compile(r"Rejection Messages\s*\((\d+)\)", re.IGNORECASE)


def editor_url(claim_id: str) -> str:
    return EDITOR_URL_TEMPLATE.format(claim_id=claim_id)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _fix_slug(href: str) -> str:
    """Extract the readable article slug from a How-to-Fix IdP link."""
    try:
        target = parse_qs(urlparse(href).query).get("target", [""])[0]
        if "/article/" in target:
            return target.rsplit("/article/", 1)[1]
    except (ValueError, KeyError, IndexError):
        pass
    return ""


def parse_rejection_details(html: str) -> dict[str, Any]:
    """Parse the editor page HTML. Returns messages, fix links, original message."""
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, Any] = {
        "rejection_count": 0,
        "messages": [],          # list of {"message": str, "fix_url": str, "fix_slug": str}
        "original_message": "",
        "found_grid": False,
    }

    header = soup.select_one("#RejectionGrid_lblRejectionHeader")
    if header:
        match = _HEADER_COUNT.search(_clean(header.get_text()))
        if match:
            result["rejection_count"] = int(match.group(1))

    grid = soup.select_one("#RejectionGrid_gvRejectionGrid")
    if grid:
        result["found_grid"] = True
        for row in grid.select("tr"):
            cells = row.select("td")
            if not cells:
                continue
            message_td = None
            for td in cells:
                if td.select_one('input[id*="hdnMessageIndex"]'):
                    message_td = td
                    break
            if message_td is None:
                continue
            message = _clean(message_td.get_text())
            if not message:
                continue
            fix_link = row.select_one('a[id$="knowURL"]')
            fix_url = fix_link.get("href", "") if fix_link else ""
            result["messages"].append(
                {
                    "message": message,
                    "fix_url": fix_url,
                    "fix_slug": _fix_slug(fix_url) if fix_url else "",
                }
            )

    tooltip = soup.select_one("#RejectionGridToolTip")
    if tooltip:
        inner = tooltip.select_one("div")
        if inner:
            for br in inner.find_all("br"):
                br.replace_with("\n")
            lines = [_clean(line) for line in inner.get_text().splitlines()]
            result["original_message"] = "\n".join(line for line in lines if line)

    if not result["rejection_count"]:
        result["rejection_count"] = len(result["messages"])
    return result


def is_login_or_error_page(html: str) -> str | None:
    """Return a short error label if the response is not a usable editor page."""
    lowered = html.lower()
    if "login.zirmed.com" in lowered:
        return "session expired (login redirect)"
    if "waystar log off" in lowered:
        return "logged off"
    if "an error has occurred" in lowered and "rejectiongrid" not in lowered:
        return "editor error page"
    return None


async def fetch_rejection_details(page, claim_id: str, *, timeout_sec: float = 60) -> dict[str, Any]:
    """GET the editor page for one claim and parse its rejection details."""
    url = editor_url(claim_id)
    response = await page.request.get(
        url,
        headers={"Referer": "https://claims.zirmed.com/Claims/Listing/Index?appid=1"},
        timeout=int(timeout_sec * 1000),
    )
    if not response.ok:
        return {"error": f"HTTP {response.status}", "messages": [],
                "rejection_count": 0, "original_message": "", "found_grid": False}

    html = await response.text()
    error = is_login_or_error_page(html)
    if error:
        return {"error": error, "messages": [],
                "rejection_count": 0, "original_message": "", "found_grid": False}

    details = parse_rejection_details(html)
    details["error"] = None
    if not details["found_grid"]:
        log.debug("Claim %s: no RejectionGrid on editor page", claim_id)
    return details
