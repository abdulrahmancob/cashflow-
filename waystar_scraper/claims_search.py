from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from config import CLAIMS_PER_PAGE, PERFORM_SEARCH_URL, REJECTED_STATUS_CODE
from logging_config import get_logger
from network_retry import RetrySettings, retry_transient

log = get_logger("search")


@dataclass
class SearchCriteria:
    transaction_date_span: str = "Last 30 Days"
    trans_from: str = ""
    trans_to: str = ""
    transaction_label: str = "30 days"
    status: str = "-1"
    allow_download_csv: bool = False

    def summary(self) -> str:
        return (
            f"span={self.transaction_date_span!r} "
            f"({self.trans_from} - {self.trans_to}) status={self.status}"
        )


def _transaction_date_range(days: int) -> tuple[str, str, str, str]:
    end = datetime.now()
    start = end - timedelta(days=days)
    trans_from = start.strftime("%m/%d/%Y")
    trans_to = end.strftime("%m/%d/%Y")

    if days <= 7:
        span_label = "Last 7 Days"
    elif days <= 30:
        span_label = "Last 30 Days"
    elif days <= 60:
        span_label = "Last 60 Days"
    elif days <= 90:
        span_label = "Last 90 Days"
    elif days <= 120:
        span_label = "Last 120 Days"
    elif days <= 365:
        span_label = "Last 1 Year"
    elif days <= 730:
        span_label = "Last 2 Years"
    else:
        span_label = "Custom"

    return span_label, trans_from, trans_to, f"{days} days"


def criteria_from_transaction_days(days: int) -> SearchCriteria:
    span_label, trans_from, trans_to, transaction_label = _transaction_date_range(days)
    return SearchCriteria(
        transaction_date_span=span_label,
        trans_from=trans_from,
        trans_to=trans_to,
        transaction_label=transaction_label,
        status="-1",
    )


def criteria_rejected_calendar(
    trans_from: str,
    trans_to: str,
    *,
    status: str = REJECTED_STATUS_CODE,
    transaction_date_span: str = "Custom",
) -> SearchCriteria:
    return SearchCriteria(
        transaction_date_span=transaction_date_span,
        trans_from=trans_from,
        trans_to=trans_to,
        transaction_label="custom",
        status=status,
    )


def build_search_form(
    *,
    token: str,
    cust_id: str,
    app_id: str = "1",
    page_number: int = 1,
    criteria: SearchCriteria | None = None,
    transaction_days: int | None = None,
    allow_download_csv: bool | None = None,
) -> str:
    if criteria is None:
        days = transaction_days if transaction_days is not None else 30
        criteria = criteria_from_transaction_days(days)

    if allow_download_csv is not None:
        criteria = SearchCriteria(
            transaction_date_span=criteria.transaction_date_span,
            trans_from=criteria.trans_from,
            trans_to=criteria.trans_to,
            transaction_label=criteria.transaction_label,
            status=criteria.status,
            allow_download_csv=allow_download_csv,
        )

    offset = (page_number - 1) * CLAIMS_PER_PAGE

    log.debug(
        "Building search form: page=%s offset=%s %s cust_id=%s",
        page_number,
        offset,
        criteria.summary(),
        cust_id,
    )

    fields: list[tuple[str, str]] = [
        ("explicitSearch", "True"),
        ("__RequestVerificationToken", token),
        ("SearchCriteria.AppId", app_id),
        ("SearchCriteria.CustId", cust_id),
        ("SearchCriteria.Offset", str(offset)),
        ("SearchCriteria.Env", "P"),
        ("SearchCriteria.SortField", "PatientName"),
        ("SearchCriteria.PageNumber", str(page_number)),
        ("SearchCriteria.OrderBy", "Transaction Date"),
        ("SearchCriteria.OrderDirection", "desc"),
        ("SearchCriteria.FromPage", "None"),
        ("SearchCriteria.TransactionDateSpan", criteria.transaction_date_span),
        ("SearchCriteria.ServiceDateSpan", "All"),
        ("SavedSearchID", ""),
        ("AllowDownloadCsv", "True" if criteria.allow_download_csv else "False"),
    ]

    workflow_stages = [
        ("1", "Normal Processing"),
        ("2", "Clearinghouse Rejections"),
        ("3", "Payer Rejections"),
        ("4", "At Payer"),
    ]
    for index, (value, text) in enumerate(workflow_stages):
        fields.extend(
            [
                (f"SearchCriteria.WorkflowStages[{index}].Value", value),
                (f"SearchCriteria.WorkflowStages[{index}].Text", text),
                (f"SearchCriteria.WorkflowStages[{index}].Selected", "True"),
            ]
        )

    empty_fields = [
        ("SearchCriteria.Status", criteria.status),
        ("SearchCriteria.PatNumber", ""),
        ("SearchCriteria.PatientNames", ""),
        ("SearchCriteria.MemberId", ""),
        ("SearchCriteria.ClaimId", ""),
        ("SearchCriteria.PayerField", "Submitted"),
        ("SearchCriteria.PayerName", ""),
        ("Service", "All"),
        ("SearchCriteria.ServiceFrom", ""),
        ("SearchCriteria.ServiceTo", ""),
        ("Transaction", criteria.transaction_label),
        ("SearchCriteria.TransFrom", criteria.trans_from),
        ("SearchCriteria.TransTo", criteria.trans_to),
        ("SearchCriteria.Archived", "0"),
        ("SearchCriteria.DraftClaimsOnly", "false"),
        ("SearchCriteria.WarningsOnly", "false"),
        ("SearchCriteria.StatusAvailable", "false"),
        ("SearchCriteria.NotInWorkGroupCheck", "false"),
        ("SearchCriteria.RejectionFrom", ""),
        ("SearchCriteria.RejectionTo", ""),
        ("SearchCriteria.ReceivedDateFrom", ""),
        ("SearchCriteria.ReceivedDateTo", ""),
        ("SearchCriteria.ChargeMin", ""),
        ("SearchCriteria.ChargeMax", ""),
        ("SearchCriteria.PaymentNumbers", ""),
        ("SearchCriteria.BatchId", ""),
        ("SearchCriteria.BatchName", ""),
        ("SearchCriteria.InstanceId", ""),
        ("SearchCriteria.ClaimPrefix", ""),
        ("SearchCriteria.ProviderNames", ""),
        ("SearchCriteria.NPI", ""),
        ("SearchCriteria.Source", "0"),
        ("SearchCriteria.SeqNum", "0"),
        ("SearchCriteria.HasRemit", "2"),
        ("SearchCriteria.Hidden", "False"),
    ]
    fields.extend(empty_fields)

    return urlencode(fields)


async def perform_search(
    page,
    token: str,
    form_body: str,
    *,
    page_number: int = 1,
    retry: RetrySettings | None = None,
) -> str:
    settings = retry or RetrySettings()
    log.info("PerformSearch POST page=%s → %s", page_number, PERFORM_SEARCH_URL)

    async def _post_search():
        return await page.request.post(
            PERFORM_SEARCH_URL,
            data=form_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://claims.zirmed.com/Claims/Listing/Index?appid=1",
            },
        )

    response = await retry_transient(
        _post_search,
        label=f"PerformSearch page {page_number}",
        max_attempts=settings.max_attempts,
        base_delay_sec=settings.base_delay_sec,
    )
    log.debug("PerformSearch HTTP %s", response.status)

    if not response.ok:
        body_preview = (await response.text())[:500]
        log.error(
            "PerformSearch failed HTTP %s — body preview: %s",
            response.status,
            body_preview,
        )
        raise RuntimeError(
            f"PerformSearch failed (HTTP {response.status}): {body_preview}"
        )

    html = await response.text()
    log.debug("PerformSearch response size=%s bytes", len(html))

    lowered = html.lower()
    if "login.zirmed.com" in lowered or "waystar log off" in lowered:
        log.error("PerformSearch returned login/logoff page — session expired")
        raise RuntimeError("Session expired — login required (PerformSearch redirected)")

    if "claimsgrid" not in lowered and "gridviewrow" not in lowered:
        log.warning(
            "PerformSearch response may not contain claims grid (no grid markers found)"
        )

    return html
