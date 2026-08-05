import asyncio
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext

from config import BASE_URL, WebPTConfig
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

# Distinct failure reasons for callers that need to re-switch clinic / re-auth.
FETCH_ERROR_LOGIN = "login"
FETCH_ERROR_DISPLAY_PATIENTS = "display_patients"


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
    # Extended fields discovered on patientChart HTML (Phase 2+)
    dob: str = ""
    age: str = ""
    physician: str = ""
    physician_npi: str = ""
    insurance: str = ""
    insurance_type: str = ""
    address: str = ""
    phone: str = ""
    return_to_dr: str = ""
    labels_all: dict | None = None
    fetch_error: str = ""


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


_NPI_RE = re.compile(r"\((\d{10})\)")


def discover_chart_labels(html: str) -> dict[str, str]:
    """All strong/td labels present in patientChart HTML (discovery, not filter)."""
    labels: dict[str, str] = {}
    for match in LABEL_PATTERN.finditer(html or ""):
        label = match.group("label").strip()
        value = _clean_html_value(match.group("value"))
        key = label.lower()
        # Keep first non-empty; allow later Insurance Type-style duplicates via suffix
        if key not in labels or (not labels[key] and value):
            labels[key] = value
        elif key == "insurance" and value and value != labels[key]:
            labels.setdefault("insurance_type", value)
    return labels


def parse_patient_chart_html(html: str) -> PatientChartInfo:
    info = PatientChartInfo()
    labels = discover_chart_labels(html)
    info.labels_all = dict(labels)

    info.auth_ins_visits = labels.get("auth/ins visits", "")
    info.cancel_no_show = labels.get("cancel/no show", "")
    info.visits_in_case = labels.get("visits in case", "")
    info.assigned_therapist = labels.get("assigned therapist", "")
    info.diagnosis = labels.get("diagnosis", "")
    info.dob = labels.get("dob", "")
    info.age = labels.get("age", "")
    info.physician = labels.get("physician", "")
    npi_m = _NPI_RE.search(info.physician or "")
    info.physician_npi = npi_m.group(1) if npi_m else ""
    info.insurance = labels.get("insurance", "")
    info.insurance_type = labels.get("insurance_type", "")
    # Second "Insurance" label often holds plan type (Medicaid/etc.)
    if not info.insurance_type:
        # discover_chart_labels stores type under insurance_type when duplicate
        pass
    info.address = labels.get("address", "")
    info.phone = labels.get("phone", "")
    info.return_to_dr = labels.get("return to dr", "")

    additional = labels.get("additional info", "")
    info.additional_info_raw = additional.replace(" | ", "\n")
    parsed = _parse_additional_info_fields(additional)
    info.deductible = parsed.get("deductible", "")
    info.copay = parsed.get("copay", "")
    info.limit_per_year = parsed.get("limit_per_year", "")
    info.referral_required = parsed.get("referral_required", "")

    return info


def chart_info_to_export_fields(info: PatientChartInfo) -> dict[str, str]:
    """Flatten PatientChartInfo into export-oriented string fields."""
    return {
        "auth_ins_visits": info.auth_ins_visits or "",
        "cancel_no_show": info.cancel_no_show or "",
        "visits_in_case": info.visits_in_case or "",
        "assigned_therapist": info.assigned_therapist or "",
        "diagnosis": info.diagnosis or "",
        "deductible": info.deductible or "",
        "copay": info.copay or "",
        "limit_per_year": info.limit_per_year or "",
        "referral_required": info.referral_required or "",
        "additional_info_raw": info.additional_info_raw or "",
        "dob": info.dob or "",
        "age": info.age or "",
        "physician": info.physician or "",
        "physician_npi": info.physician_npi or "",
        "insurance": info.insurance or "",
        "insurance_type": info.insurance_type or "",
        "address": info.address or "",
        "phone": info.phone or "",
        "return_to_dr": info.return_to_dr or "",
    }


def patient_chart_url(patient_id: int, case_id: int | None) -> str:
    if case_id:
        return f"{BASE_URL}/patientChart.php?ID={patient_id}&CaseID={case_id}"
    return f"{BASE_URL}/patientChart.php?ID={patient_id}"


def _save_chart_debug_html(
    html: str,
    *,
    patient_id: int,
    case_id: int | None,
    debug_dir: Path | None,
) -> None:
    if debug_dir is None or not html:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        suffix = str(case_id) if case_id else "nocase"
        path = debug_dir / f"patient_chart_{patient_id}_{suffix}.html"
        path.write_text(html, encoding="utf-8", errors="replace")
        log.info("Saved patient chart debug HTML to %s", path)
    except Exception as exc:
        log.warning("Could not save chart debug HTML: %s", exc)


def _page_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html[:5000], re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _looks_like_login(page_url: str, html: str) -> bool:
    return (
        "login.webpt.com" in page_url
        or "login.webpt.com" in html[:2000]
        or "Auth0" in html[:3000]
        or 'id="login"' in html[:4000]
    )


def _looks_like_patient_chart(page_url: str, html: str) -> bool:
    """True when HTML is a real patient chart (nav often also says Display Patients)."""
    url = (page_url or "").lower()
    if "patientchart" in url:
        return True
    title = _page_title(html).lower()
    if "patient record" in title:
        return True
    # Chart body markers used by parse_patient_chart_html.
    lower = html.lower()
    return (
        "<strong>diagnosis" in lower
        or "<strong>auth/ins visits" in lower
        or "<strong>additional info" in lower
    )


def _looks_like_display_patients(page_url: str, html: str) -> bool:
    # Valid charts include a "Display Patients" nav link — never treat that alone as failure.
    if _looks_like_patient_chart(page_url, html):
        return False
    title = _page_title(html).lower()
    if "display patients" in title:
        return True
    url = (page_url or "").lower()
    return "display patients" in html and "patientchart" not in url


async def fetch_patient_chart(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int | None,
    page: "Page | None" = None,
    config: WebPTConfig | None = None,
    timeout_ms: int = 90000,
    retries: int = 5,
    blocked_retries: int = 2,
    debug_dir: Path | None = None,
) -> PatientChartInfo:
    url = patient_chart_url(patient_id, case_id)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Referer": f"{BASE_URL}/dashboard.php",
    }
    last_error = ""
    last_html = ""
    blocked_attempt = 0
    for attempt in range(retries):
        try:
            html = ""
            # Prefer the authenticated browser tab — API request context often
            # hits Auth0 login even when the page session is valid.
            if page is not None:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if config is not None:
                    from auth import ensure_page_authenticated

                    await ensure_page_authenticated(page, context, config)
                    # Re-nav if re-auth bounced us off the chart.
                    if not _looks_like_patient_chart(page.url or "", await page.content()):
                        await page.goto(
                            url, wait_until="domcontentloaded", timeout=timeout_ms
                        )
                html = await page.content()
                last_html = html
                page_url = page.url or ""
                if _looks_like_login(page_url, html):
                    last_error = FETCH_ERROR_LOGIN
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay_sec(attempt))
                        continue
                    break
                if _looks_like_patient_chart(page_url, html):
                    return parse_patient_chart_html(html)
                if _looks_like_display_patients(page_url, html):
                    last_error = FETCH_ERROR_DISPLAY_PATIENTS
                    # Wrong clinic — retries without re-switch won't help much.
                    if attempt < retries - 1 and attempt == 0:
                        await asyncio.sleep(retry_delay_sec(attempt))
                        continue
                    break
                # Unknown page — try parse anyway in case markers are partial.
                parsed = parse_patient_chart_html(html)
                if any(
                    (
                        parsed.diagnosis,
                        parsed.copay,
                        parsed.deductible,
                        parsed.assigned_therapist,
                    )
                ):
                    return parsed
                last_error = "unexpected chart page"
                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay_sec(attempt))
                    continue
                break

            response = await context.request.get(
                url,
                headers=headers,
                max_retries=2,
                timeout=timeout_ms,
            )
            if response.ok:
                html = await response.text()
                last_html = html
                if _looks_like_login("", html):
                    last_error = FETCH_ERROR_LOGIN
                    if attempt < retries - 1:
                        await asyncio.sleep(retry_delay_sec(attempt))
                        continue
                    break
                if _looks_like_patient_chart("", html):
                    return parse_patient_chart_html(html)
                if _looks_like_display_patients("", html):
                    last_error = FETCH_ERROR_DISPLAY_PATIENTS
                    break
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
            return PatientChartInfo(fetch_error=str(exc))
    _save_chart_debug_html(
        last_html, patient_id=patient_id, case_id=case_id, debug_dir=debug_dir
    )
    log.warning("Chart fetch failed patient %s: %s", patient_id, last_error)
    return PatientChartInfo(fetch_error=last_error or "unknown")


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
