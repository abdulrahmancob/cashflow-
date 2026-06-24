import asyncio
import re
from dataclasses import dataclass, field
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode

from playwright.async_api import BrowserContext

from config import BASE_URL, PATIENT_CHART_NOTE_URL, PRINT_PDF_URL
from http_utils import is_transient_network_error, retry_delay_sec
from logging_config import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger("chart_notes_api")

PRINT_PDF_QUERY_RE = re.compile(
    r"printPDF\.php\?([^\"'>\s]+)",
    re.IGNORECASE,
)
TABLE_ROW_RE = re.compile(
    r"<tr[^>]*>(?P<row>.*?printPDF\.php\?(?P<query>[^\"'>\s]+).*?)</tr>",
    re.IGNORECASE | re.DOTALL,
)
URI_DATE_DN_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}).*?(?P<dn>DN\d+)",
    re.IGNORECASE,
)
NOTE_TYPE_RE = re.compile(
    r"<td[^>]*>\s*(?P<type>Initial Evaluation|Re-Examination|Re-examination|"
    r"Daily Note|Discharge Summary|Progress Note|Plan of Care|Evaluation|"
    r"Orthosis Fabrication|Wound Note|[^<]{3,80}?)\s*</td>",
    re.IGNORECASE,
)
NOTE_DATE_RE = re.compile(
    r"<td[^>]*>\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\s*</td>",
    re.IGNORECASE,
)

_BLOCKED_MAX_WAIT_SEC = 90
_BLOCKED_RETRY_BASE_SEC = 30


@dataclass
class ChartNoteRef:
    cnsid: str = ""
    facility_id: str = ""
    patient_id: str = ""
    uri: str = ""
    case_id: str = ""
    note_type: str = ""
    note_date: str = ""
    print_url: str = ""
    dedupe_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            if self.cnsid:
                self.dedupe_key = f"cns:{self.cnsid}"
            elif self.uri:
                self.dedupe_key = f"uri:{self.uri}"
            else:
                self.dedupe_key = self.print_url


def patient_chart_note_url(patient_id: int, case_id: int) -> str:
    return f"{PATIENT_CHART_NOTE_URL}?ID={patient_id}&CaseID={case_id}"


def _normalize_query(raw_query: str) -> dict[str, str]:
    query = unescape(raw_query).replace("&amp;", "&")
    parsed = parse_qs(query, keep_blank_values=False)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def build_print_pdf_url(
    *,
    cnsid: str = "",
    facility_id: str = "",
    patient_id: str = "",
    uri: str = "",
    case_id: str = "",
) -> str:
    params: dict[str, str] = {}
    if cnsid:
        params["CNSID"] = cnsid
    if facility_id:
        params["CID"] = facility_id
    if patient_id:
        params["PID"] = patient_id
    if uri:
        params["URI"] = uri
    if case_id:
        params["CaseID"] = case_id
    return f"{PRINT_PDF_URL}?{urlencode(params)}"


def _note_type_from_uri(uri: str) -> str:
    if re.search(r"-DN\d+", uri, re.IGNORECASE):
        return "DailyNote"
    return "ChartNote"


def _iso_date_from_us(us_date: str) -> str:
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (us_date or "").strip())
    if not match:
        return ""
    month, day, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _metadata_from_row(row_html: str) -> tuple[str, str]:
    note_type = ""
    note_date = ""
    type_match = NOTE_TYPE_RE.search(row_html)
    if type_match:
        note_type = re.sub(r"\s+", " ", type_match.group("type")).strip()
    date_matches = NOTE_DATE_RE.findall(row_html)
    if date_matches:
        note_date = _iso_date_from_us(date_matches[0])
    return note_type, note_date


def _ref_from_params(
    params: dict[str, str],
    *,
    note_type: str = "",
    note_date: str = "",
) -> ChartNoteRef | None:
    case_id = params.get("CaseID") or params.get("caseid") or ""
    cnsid = params.get("CNSID") or params.get("cnsid") or ""
    facility_id = params.get("CID") or params.get("cid") or ""
    patient_id = params.get("PID") or params.get("pid") or ""
    uri = params.get("URI") or params.get("uri") or ""

    if not cnsid and not uri:
        return None

    if uri and not note_type:
        note_type = _note_type_from_uri(uri)
    if uri and not note_date:
        uri_match = URI_DATE_DN_RE.search(uri)
        if uri_match:
            note_date = uri_match.group("date")

    print_url = build_print_pdf_url(
        cnsid=cnsid,
        facility_id=facility_id,
        patient_id=patient_id,
        uri=uri,
        case_id=case_id,
    )
    return ChartNoteRef(
        cnsid=cnsid,
        facility_id=facility_id,
        patient_id=patient_id,
        uri=uri,
        case_id=case_id,
        note_type=note_type,
        note_date=note_date,
        print_url=print_url,
    )


def parse_chart_notes_html(
    html: str,
    *,
    case_id: int | None = None,
) -> list[ChartNoteRef]:
    """Extract printable chart note PDF links from patientChartNote.php HTML."""
    case_str = str(case_id) if case_id is not None else ""
    by_key: dict[str, ChartNoteRef] = {}

    for row_match in TABLE_ROW_RE.finditer(html):
        row_html = row_match.group("row")
        query = unescape(row_match.group("query")).replace("&amp;", "&")
        params = _normalize_query(query)
        if case_str and params.get("CaseID") and params["CaseID"] != case_str:
            continue
        note_type, note_date = _metadata_from_row(row_html)
        ref = _ref_from_params(params, note_type=note_type, note_date=note_date)
        if ref:
            by_key.setdefault(ref.dedupe_key, ref)

    for query_match in PRINT_PDF_QUERY_RE.finditer(html):
        params = _normalize_query(query_match.group(1))
        if case_str and params.get("CaseID") and params["CaseID"] != case_str:
            continue
        ref = _ref_from_params(params)
        if ref:
            by_key.setdefault(ref.dedupe_key, ref)

    notes = list(by_key.values())
    log.debug(
        "Parsed %d chart note link(s) for case=%s",
        len(notes),
        case_str or "any",
    )
    return notes


async def fetch_patient_chart_notes(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int,
    page: "Page | None" = None,
    timeout_ms: int = 90000,
    retries: int = 5,
    blocked_retries: int = 2,
) -> list[ChartNoteRef]:
    url = patient_chart_note_url(patient_id, case_id)
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
                return parse_chart_notes_html(html, case_id=case_id)
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
                        "Chart notes fetch blocked for patient %s (%s) — wait %ds",
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
                    "Chart notes network error patient %s (attempt %d/%d): %s — retry in %ds",
                    patient_id,
                    attempt + 1,
                    retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            log.warning("Chart notes fetch failed patient %s: %s", patient_id, exc)
            return []
    log.warning(
        "Chart notes fetch failed patient %s case %s: %s",
        patient_id,
        case_id,
        last_error,
    )
    return []


def chart_note_to_dict(note: ChartNoteRef) -> dict[str, Any]:
    return {
        "cnsid": note.cnsid,
        "facility_id": note.facility_id,
        "patient_id": note.patient_id,
        "uri": note.uri,
        "case_id": note.case_id,
        "note_type": note.note_type,
        "note_date": note.note_date,
        "print_url": note.print_url,
        "dedupe_key": note.dedupe_key,
    }
