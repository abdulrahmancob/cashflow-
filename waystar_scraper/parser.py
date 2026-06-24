from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from logging_config import get_logger

log = get_logger("parser")


def _text(element) -> str:
    if element is None:
        return ""
    return " ".join(element.get_text(strip=True).split())


def _parse_money(value: str) -> float | None:
    cleaned = value.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def parse_search_result_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    total_results = None
    total_el = soup.select_one("#resultCountTotal")
    if total_el:
        try:
            total_results = int(_text(total_el))
        except ValueError:
            total_results = None

    total_pages = None
    pages_el = soup.select_one("#totalPageCount")
    if pages_el:
        try:
            total_pages = int(_text(pages_el))
        except ValueError:
            total_pages = None

    claims: list[dict[str, Any]] = []
    rows = soup.select("tr.gridViewRow.gridViewExpandableRow")

    for row in rows:
        status_link = row.select_one("a.claimStatusLink")
        status_text = _text(status_link)
        status_detail = status_link.get("title", "") if status_link else ""

        workgroup_el = row.select_one(".workGroupCell span")
        workgroup = _text(workgroup_el)

        provider_el = row.select_one(".providerNameCell span")
        provider = _text(provider_el)

        claim_number = _text(row.select_one(".patientNumberCell"))
        transaction_date = _text(row.select_one(".dtLastSubmissionCell"))

        payer_el = row.select_one(".subPayerCell span")
        payer = payer_el.get("title") if payer_el and payer_el.get("title") else _text(payer_el)

        charges_cell = _text(row.select_one(".chargesCell"))

        claims.append(
            {
                "claim_id": row.get("data-claimid", ""),
                "instance_id": row.get("data-instanceid", ""),
                "claim_number": claim_number,
                "patient_name": row.get("data-patientname", ""),
                "service_date": row.get("data-servicedate", ""),
                "transaction_date": transaction_date,
                "payer": payer or row.get("data-payername", ""),
                "payer_id": row.get("data-payerid", ""),
                "charges": _parse_money(charges_cell)
                if charges_cell
                else _parse_money(row.get("data-totalcharges", "")),
                "sequence": row.get("data-sequence", ""),
                "status": status_text,
                "status_detail": status_detail,
                "provider": provider,
                "workgroup": workgroup,
                "has_remit": _parse_bool(row.get("data-hasremit")),
                "is_held": _parse_bool(row.get("data-isheld")),
                "is_open": _parse_bool(row.get("data-isopen")),
                "notice_code": row.get("data-noticecode", ""),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    log.debug(
        "Parsed HTML: total_results=%s total_pages=%s rows=%s",
        total_results,
        total_pages,
        len(claims),
    )

    return {
        "total_results": total_results,
        "total_pages": total_pages,
        "claims": claims,
    }
