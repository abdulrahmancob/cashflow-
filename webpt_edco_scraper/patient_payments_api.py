"""Fetch and parse WebPT Patient Payments history pages."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext

from config import BASE_URL, WebPTConfig
from http_utils import is_transient_network_error, retry_delay_sec
from logging_config import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger("patient_payments_api")

_MONEY_RE = re.compile(r"[^\d.\-]")
_ROW_RE = re.compile(
    r"<tr[^>]*class=\"[^\"]*(?:odd|even)[^\"]*\"[^>]*>(.*?)</tr>",
    re.IGNORECASE | re.DOTALL,
)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class PaymentTxn:
    date_of_service: str = ""
    date_of_transaction: str = ""
    payment_type: str = ""
    description: str = ""
    amount_due: float = 0.0
    amount_paid: float = 0.0
    paid_method: str = ""
    credit_type: str = ""
    auth_check: str = ""
    # Extended ledger fields from var transactions JSON
    transaction_id: str = ""
    status: str = ""
    case_id: str = ""
    facility_id: str = ""
    patient_id: str = ""
    payment_date: str = ""
    collector_initials: str = ""
    emv_payment_type: str = ""
    paid_flag: str = ""
    type_id: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class PatientPaymentsResult:
    patient_id: int
    case_id: int
    transactions: list[PaymentTxn] = field(default_factory=list)
    total_charge: float = 0.0
    total_paid: float = 0.0
    balance: float = 0.0
    fetch_error: str = ""


def patient_payments_url(patient_id: int, case_id: int) -> str:
    return (
        f"{BASE_URL}/patient/transaction/chart"
        f"?ID={patient_id}&CaseID={case_id}"
    )


def _clean_text(raw: str) -> str:
    text = unescape(raw or "")
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_money(raw: str) -> float:
    s = _clean_text(raw)
    if not s:
        return 0.0
    neg = "(" in s and ")" in s
    s = _MONEY_RE.sub("", s.replace(",", ""))
    if not s or s in {".", "-", "-."}:
        return 0.0
    try:
        val = float(s)
    except ValueError:
        return 0.0
    return -val if neg and val > 0 else val


def parse_patient_payments_html(html: str) -> tuple[list[PaymentTxn], dict[str, float]]:
    """Parse payment rows from chart HTML.

    Prefer the embedded ``var transactions = [...]`` JSON (present in raw HTTP
    responses). Fall back to rendered ``#transactions-list`` DataTables rows.
    """
    txns: list[PaymentTxn] = []
    js_ok = False

    m_js = re.search(r"var\s+transactions\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if m_js:
        try:
            raw_list = json.loads(m_js.group(1))
            if isinstance(raw_list, list):
                js_ok = True
                known = {
                    "dateOfService",
                    "dateOfTransaction",
                    "type",
                    "description",
                    "amountDue",
                    "amountPaid",
                    "paidMethodType",
                    "creditType",
                    "checkAuthorizationNumber",
                    "transactionId",
                    "status",
                    "caseId",
                    "facilityId",
                    "patientId",
                    "paymentDate",
                    "collectorInitials",
                    "emvPaymentType",
                    "paid",
                    "typeId",
                }
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    extras = {
                        str(k): item[k] for k in item if k not in known
                    }
                    txns.append(
                        PaymentTxn(
                            date_of_service=str(item.get("dateOfService") or ""),
                            date_of_transaction=str(
                                item.get("dateOfTransaction") or ""
                            ),
                            payment_type=str(item.get("type") or ""),
                            description=str(item.get("description") or ""),
                            amount_due=float(item.get("amountDue") or 0),
                            amount_paid=float(item.get("amountPaid") or 0),
                            paid_method=str(item.get("paidMethodType") or ""),
                            credit_type=str(item.get("creditType") or ""),
                            auth_check=str(
                                item.get("checkAuthorizationNumber") or ""
                            ),
                            transaction_id=str(item.get("transactionId") or ""),
                            status=str(item.get("status") or ""),
                            case_id=str(item.get("caseId") or ""),
                            facility_id=str(item.get("facilityId") or ""),
                            patient_id=str(item.get("patientId") or ""),
                            payment_date=str(item.get("paymentDate") or ""),
                            collector_initials=str(
                                item.get("collectorInitials") or ""
                            ),
                            emv_payment_type=str(item.get("emvPaymentType") or ""),
                            paid_flag=str(item.get("paid") or ""),
                            type_id=str(item.get("typeId") or ""),
                            extras=extras,
                        )
                    )
        except (json.JSONDecodeError, TypeError, ValueError):
            txns = []
            js_ok = False

    if not js_ok:
        # Prefer tbody of transactions-list when present.
        body = html
        m_table = re.search(
            r'id="transactions-list"[^>]*>(.*?)</table>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if m_table:
            body = m_table.group(1)

        for row_html in _ROW_RE.findall(body):
            cells = [_clean_text(td) for td in _TD_RE.findall(row_html)]
            if len(cells) < 6:
                continue
            # Skip header-ish rows
            if cells[0].lower().startswith("date of service"):
                continue
            txn = PaymentTxn(
                date_of_service=cells[0],
                date_of_transaction=cells[1] if len(cells) > 1 else "",
                payment_type=cells[2] if len(cells) > 2 else "",
                description=cells[3] if len(cells) > 3 else "",
                amount_due=_parse_money(cells[4] if len(cells) > 4 else ""),
                amount_paid=_parse_money(cells[5] if len(cells) > 5 else ""),
                paid_method=cells[6] if len(cells) > 6 else "",
                credit_type=cells[7] if len(cells) > 7 else "",
                auth_check=cells[8] if len(cells) > 8 else "",
            )
            txns.append(txn)

    totals = {"total_charge": 0.0, "total_paid": 0.0, "balance": 0.0}
    # Prefer class hooks (avoid matching "balance" inside class="balance-label").
    for cls, key in (
        ("total-due", "total_charge"),
        ("total-paid", "total_paid"),
        ("total-balance", "balance"),
    ):
        m = re.search(
            rf'class="{cls}"[^>]*>(.*?)</td>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            totals[key] = _parse_money(m.group(1))
            continue
        # Fallback: exact label text with colon.
        label = {
            "total_charge": r"Total\s+Charge\s*:",
            "total_paid": r"Total\s+Paid\s*:",
            "balance": r"(?<![\w-])Balance\s*:",
        }[key]
        m2 = re.search(
            label + r".*?</td>\s*<td[^>]*>(.*?)</td>",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if m2:
            totals[key] = _parse_money(m2.group(1))

    # Raw HTTP shell often has empty balance cells; derive from transactions.
    if txns and totals["total_charge"] == 0 and totals["total_paid"] == 0:
        totals["total_charge"] = sum(t.amount_due for t in txns)
        totals["total_paid"] = sum(t.amount_paid for t in txns)
        totals["balance"] = totals["total_charge"] - totals["total_paid"]
    return txns, totals


def _looks_like_login(html: str) -> bool:
    head = html[:4000]
    return (
        "login.webpt.com" in head
        or "Auth0" in head
        or 'id="login"' in head
    )


def _save_debug_html(
    html: str,
    *,
    patient_id: int,
    case_id: int,
    debug_dir: Path | None,
) -> None:
    if debug_dir is None or not html:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"patient_payments_{patient_id}_{case_id}.html"
        path.write_text(html, encoding="utf-8", errors="replace")
        log.info("Saved payments debug HTML to %s", path)
    except Exception as exc:
        log.warning("Could not save payments debug HTML: %s", exc)


async def fetch_patient_payments(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int,
    page: "Page | None" = None,
    config: WebPTConfig | None = None,
    timeout_ms: int = 60000,
    retries: int = 3,
    debug_dir: Path | None = None,
    prefer_http: bool = True,
) -> PatientPaymentsResult:
    url = patient_payments_url(patient_id, case_id)
    result = PatientPaymentsResult(patient_id=patient_id, case_id=case_id)
    last_error = ""
    last_html = ""

    for attempt in range(retries):
        try:
            html = ""
            if prefer_http:
                response = await context.request.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Referer": f"{BASE_URL}/patientChart.php?ID={patient_id}&CaseID={case_id}",
                    },
                    max_retries=2,
                    timeout=timeout_ms,
                )
                if response.ok:
                    html = await response.text()
                else:
                    last_error = f"HTTP {response.status}"
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay_sec(attempt))
                        continue
                    break
            elif page is not None:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if config is not None:
                    from auth import ensure_page_authenticated

                    await ensure_page_authenticated(page, context, config)
                html = await page.content()
            else:
                last_error = "no page and prefer_http=False"
                break

            last_html = html
            if _looks_like_login(html):
                last_error = "login"
                if page is not None and config is not None:
                    from auth import ensure_page_authenticated

                    await ensure_page_authenticated(page, context, config)
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    html = await page.content()
                    last_html = html
                    if _looks_like_login(html):
                        if attempt < retries - 1:
                            await asyncio.sleep(retry_delay_sec(attempt))
                            continue
                        break
                else:
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay_sec(attempt))
                        continue
                    break

            if (
                "var transactions" not in html
                and "transactions-list" not in html
                and "Patient Payments" not in html
            ):
                # HTTP sometimes needs a page warm-up
                if prefer_http and page is not None and attempt == 0:
                    prefer_http = False
                    continue
                last_error = "payments page missing table"
                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay_sec(attempt))
                    continue
                break

            txns, totals = parse_patient_payments_html(html)
            result.transactions = txns
            result.total_charge = totals["total_charge"]
            result.total_paid = totals["total_paid"]
            result.balance = totals["balance"]
            result.fetch_error = ""
            return result
        except Exception as exc:
            last_error = str(exc)
            if is_transient_network_error(exc) and attempt < retries - 1:
                await asyncio.sleep(retry_delay_sec(attempt))
                continue
            log.warning(
                "Payments fetch failed patient=%s case=%s: %s",
                patient_id,
                case_id,
                exc,
            )
            result.fetch_error = last_error
            return result

    _save_debug_html(
        last_html, patient_id=patient_id, case_id=case_id, debug_dir=debug_dir
    )
    result.fetch_error = last_error or "unknown"
    log.warning(
        "Payments fetch failed patient=%s case=%s: %s",
        patient_id,
        case_id,
        result.fetch_error,
    )
    return result


def parse_mmddyyyy(raw: str) -> str | None:
    """Return YYYY-MM-DD from MM/DD/YYYY or None."""
    s = (raw or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{yyyy:04d}-{mm:02d}-{dd:02d}"
