import asyncio
import re
from dataclasses import dataclass
from html import unescape
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext

from config import BASE_URL
from http_utils import is_transient_network_error, retry_delay_sec
from logging_config import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger("patient_chart_api")

LABEL_PATTERN = re.compile(
    r"<strong>\s*(?P<label>[^<:]+?)\s*:\s*</strong>\s*</td>\s*<td[^>]*>(?P<value>.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)

_BLOCKED_MAX_WAIT_SEC = 90
_BLOCKED_RETRY_BASE_SEC = 30


@dataclass
class PatientChartInfo:
    auth_ins_visits: str = ""
    cancel_no_show: str = ""
    visits_in_case: str = ""
    assigned_therapist: str = ""
    diagnosis: str = ""
    additional_info_raw: str = ""
    deductible: str = ""
    copay: str = ""
    limit_per_year: str = ""
    referral_required: str = ""


def _clean_html_value(raw: str) -> str:
    text = unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_additional_info_fields(additional: str) -> dict[str, str]:
    """Extract Deductible/Copay/Limit/Year/Referral from Additional Info block."""
    fields: dict[str, str] = {}
    if not additional:
        return fields
    patterns = [
        ("deductible", r"Deductible\s*:\s*"),
        ("copay", r"Copay\s*:\s*"),
        ("limit_per_year", r"Limit/Year\s*:\s*"),
        ("referral_required", r"Referral required\s*:\s*"),
    ]
    stop = r"(?=\s*(?:Deductible|Copay|Limit/Year|Referral required)\s*:|-{3,}|Insurance Updates|$)"
    for name, prefix in patterns:
        match = re.search(prefix + r"(.+?)" + stop, additional, re.IGNORECASE | re.DOTALL)
        if match:
            fields[name] = re.sub(r"\s+", " ", match.group(1)).strip()
    return fields


def parse_patient_chart_html(html: str) -> PatientChartInfo:
    info = PatientChartInfo()
    labels: dict[str, str] = {}
    for match in LABEL_PATTERN.finditer(html):
        label = match.group("label").strip()
        value = _clean_html_value(match.group("value"))
        labels[label.lower()] = value

    info.auth_ins_visits = labels.get("auth/ins visits", "")
    info.cancel_no_show = labels.get("cancel/no show", "")
    info.visits_in_case = labels.get("visits in case", "")
    info.assigned_therapist = labels.get("assigned therapist", "")
    info.diagnosis = labels.get("diagnosis", "")

    additional = labels.get("additional info", "")
    info.additional_info_raw = additional.replace(" | ", "\n")
    parsed = _parse_additional_info_fields(additional)
    info.deductible = parsed.get("deductible", "")
    info.copay = parsed.get("copay", "")
    info.limit_per_year = parsed.get("limit_per_year", "")
    info.referral_required = parsed.get("referral_required", "")

    return info


def patient_chart_url(patient_id: int, case_id: int | None) -> str:
    if case_id:
        return f"{BASE_URL}/patientChart.php?ID={patient_id}&CaseID={case_id}"
    return f"{BASE_URL}/patientChart.php?ID={patient_id}"


async def fetch_patient_chart(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int | None,
    page: "Page | None" = None,
    timeout_ms: int = 90000,
    retries: int = 5,
    blocked_retries: int = 2,
) -> PatientChartInfo:
    url = patient_chart_url(patient_id, case_id)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Referer": f"{BASE_URL}/dashboard.php",
    }
    last_error = ""
    blocked_attempt = 0
    for attempt in range(retries):
        try:
            response = await context.request.get(
                url,
                headers=headers,
                max_retries=2,
                timeout=timeout_ms,
            )
            if response.ok:
                html = await response.text()
                return parse_patient_chart_html(html)
            last_error = f"HTTP {response.status}"
            if response.status in (403, 429):
                if blocked_attempt < blocked_retries:
                    if page is not None and blocked_attempt == 0:
                        from auth import refresh_csrf

                        await refresh_csrf(context, page)
                    wait = min(
                        _BLOCKED_MAX_WAIT_SEC,
                        int(retry_delay_sec(blocked_attempt, base=_BLOCKED_RETRY_BASE_SEC)),
                    )
                    log.warning(
                        "Chart fetch blocked for patient %s (%s) — wait %ds",
                        patient_id,
                        response.status,
                        wait,
                    )
                    blocked_attempt += 1
                    await asyncio.sleep(wait)
                    continue
                break
            break
        except Exception as exc:
            last_error = str(exc)
            if is_transient_network_error(exc) and attempt < retries - 1:
                wait = int(retry_delay_sec(attempt))
                log.warning(
                    "Chart fetch network error patient %s (attempt %d/%d): %s — retry in %ds",
                    patient_id,
                    attempt + 1,
                    retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            log.warning("Chart fetch failed patient %s: %s", patient_id, exc)
            return PatientChartInfo()
    log.warning("Chart fetch failed patient %s: %s", patient_id, last_error)
    return PatientChartInfo()


def chart_to_dict(chart: PatientChartInfo) -> dict[str, str]:
    return {
        "auth_ins_visits": chart.auth_ins_visits,
        "cancel_no_show": chart.cancel_no_show,
        "visits_in_case": chart.visits_in_case,
        "assigned_therapist": chart.assigned_therapist,
        "diagnosis": chart.diagnosis,
        "deductible": chart.deductible,
        "copay": chart.copay,
        "limit_per_year": chart.limit_per_year,
        "referral_required": chart.referral_required,
        "additional_info_raw": chart.additional_info_raw,
    }
