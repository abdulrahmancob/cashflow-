import asyncio
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode

from playwright.async_api import BrowserContext

from config import BASE_URL, PATIENT_CHART_URL, PRINT_PDF_URL, WebPTConfig
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
    """Clinical chart records page (Daily Note / Eval PDF links live here)."""
    return f"{PATIENT_CHART_URL}?ID={patient_id}&CaseID={case_id}"


def patient_ext_doc_url(patient_id: int, case_id: int) -> str:
    return f"{BASE_URL}/patientExtDoc.php?ID={patient_id}&CaseID={case_id}"


def extract_case_id_from_url(url: str) -> str:
    """Parse CaseID from a chart / ext-doc URL. Empty if absent."""
    if not url:
        return ""
    from urllib.parse import urlparse

    qs = parse_qs(urlparse(url).query)
    raw = (qs.get("CaseID") or qs.get("caseid") or [""])[0]
    return str(raw or "").strip()


def assert_opened_case_id(opened_case_id: str, scheduled_case_id: str | int) -> None:
    """S1 gate: fail closed when opened CaseID != schedule CaseID."""
    opened = str(opened_case_id or "").strip()
    scheduled = str(scheduled_case_id or "").strip()
    if not scheduled:
        raise ValueError("CaseMissingOnSchedule: scheduled case_id is blank")
    if not opened:
        raise ValueError("CaseOpenFailed: opened CaseID is blank")
    if opened != scheduled:
        raise ValueError(
            f"CaseMismatch: opened_case_id={opened} scheduled_case_id={scheduled}"
        )


def _page_is_auth_redirect(page: "Page") -> bool:
    url = (page.url or "").lower()
    return "login.webpt.com" in url or "/u/login" in url


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


def _html_looks_like_auth(html: str) -> bool:
    lowered = (html or "").lower()
    return (
        "login.webpt.com" in lowered
        or "/u/login" in lowered
        or "already signed in" in lowered
        or 'name="username"' in lowered
        or 'name="password"' in lowered
        or "auth0" in lowered
    )


def _maybe_save_debug_html(
    html: str,
    *,
    patient_id: int,
    case_id: int,
    debug_dir: Path | None,
) -> None:
    if debug_dir is None or "printPDF.php" in html:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"chart_notes_{patient_id}_{case_id}.html"
    path.write_text(html, encoding="utf-8")
    log.info("Saved chart notes debug HTML to %s", path)


async def _load_chart_notes_html_via_page(
    page: "Page",
    context: BrowserContext,
    url: str,
    *,
    patient_id: int,
    case_id: int,
    config: WebPTConfig | None,
    timeout_ms: int,
    page_lock: asyncio.Lock | None,
    session_lock: asyncio.Lock | None = None,
) -> str:
    async def _navigate() -> str:
        from auth import ensure_page_authenticated

        ext_doc_url = patient_ext_doc_url(patient_id, case_id)
        await page.goto(ext_doc_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if config is not None:
            await ensure_page_authenticated(page, context, config)

        for attempt in range(2):
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if config is not None:
                await ensure_page_authenticated(page, context, config)
            if not _page_is_auth_redirect(page):
                break
            log.warning(
                "Chart notes navigation auth redirect patient %s (attempt %d)",
                patient_id,
                attempt + 1,
            )

        try:
            await page.wait_for_selector(
                'a[href*="printPDF.php"]',
                timeout=min(15000, timeout_ms),
            )
        except Exception:
            pass
        return await page.content()

    # Hold session_lock then page_lock so reauth cannot interleave with page use.
    if session_lock is not None and page_lock is not None:
        async with session_lock:
            async with page_lock:
                return await _navigate()
    if page_lock is not None:
        async with page_lock:
            return await _navigate()
    if session_lock is not None:
        async with session_lock:
            return await _navigate()
    return await _navigate()


async def _fetch_chart_notes_via_page(
    page: "Page",
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int,
    url: str,
    config: WebPTConfig | None,
    timeout_ms: int,
    retries: int,
    page_lock: asyncio.Lock | None,
    debug_dir: Path | None,
    session_lock: asyncio.Lock | None = None,
) -> list[ChartNoteRef]:
    last_error = ""
    for attempt in range(retries):
        try:
            html = await _load_chart_notes_html_via_page(
                page,
                context,
                url,
                patient_id=patient_id,
                case_id=case_id,
                config=config,
                timeout_ms=timeout_ms,
                page_lock=page_lock,
                session_lock=session_lock,
            )
            notes = parse_chart_notes_html(html, case_id=case_id)
            if not notes:
                _maybe_save_debug_html(
                    html,
                    patient_id=patient_id,
                    case_id=case_id,
                    debug_dir=debug_dir,
                )
                if _page_is_auth_redirect(page):
                    log.warning(
                        "Chart notes page for patient %s looks like login redirect",
                        patient_id,
                    )
            return notes
        except Exception as exc:
            last_error = str(exc)
            if is_transient_network_error(exc) and attempt < retries - 1:
                wait = int(retry_delay_sec(attempt))
                log.warning(
                    "Chart notes page error patient %s (attempt %d/%d): %s — retry in %ds",
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


async def fetch_patient_chart_notes(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int,
    page: "Page | None" = None,
    config: WebPTConfig | None = None,
    page_lock: asyncio.Lock | None = None,
    session_lock: asyncio.Lock | None = None,
    debug_dir: Path | None = None,
    timeout_ms: int = 90000,
    retries: int = 5,
    blocked_retries: int = 2,
    prefer_http: bool = False,
) -> list[ChartNoteRef]:
    """List printable chart notes for a patient/case.

    By default uses Playwright page navigation when ``page`` is set (more
    reliable after clinic switch). With ``prefer_http=True`` (parallel-download),
    try fast ``context.request`` first and only fall back to page on auth-like
    empty HTML.
    """
    import time as _time

    t0 = _time.perf_counter()
    try:
        from snowflake_pull.observability import get_global_obs

        _obs = get_global_obs()
    except Exception:
        _obs = None
    if _obs is not None:
        _obs.emit(
            event="decision",
            level="INFO",
            operation="note_index_start",
            webpt_patient_id=str(patient_id),
            outcome="start",
            extra={"case_id": case_id, "prefer_http": prefer_http},
        )

    url = patient_chart_note_url(patient_id, case_id)

    def _finish(notes: list[ChartNoteRef], *, via: str) -> list[ChartNoteRef]:
        if _obs is not None:
            dates = sorted({n.note_date for n in notes if n.note_date})
            _obs.emit(
                event="decision",
                level="INFO",
                operation="note_index",
                webpt_patient_id=str(patient_id),
                outcome="success",
                decision="note_index_listed",
                decision_reason=via,
                execution_ms=round((_time.perf_counter() - t0) * 1000, 2),
                extra={"note_count": len(notes), "note_dates": dates[:50]},
            )
            _obs.mark_success(
                operation="note_index",
                webpt_patient_id=str(patient_id),
            )
        return notes

    if page is not None and not prefer_http:
        return _finish(
            await _fetch_chart_notes_via_page(
                page,
                context,
                patient_id=patient_id,
                case_id=case_id,
                url=url,
                config=config,
                timeout_ms=timeout_ms,
                retries=retries,
                page_lock=page_lock,
                session_lock=session_lock,
                debug_dir=debug_dir,
            ),
            via="page",
        )

    notes, auth_miss = await _fetch_chart_notes_via_http(
        context,
        patient_id=patient_id,
        case_id=case_id,
        url=url,
        debug_dir=debug_dir,
        timeout_ms=timeout_ms,
        retries=retries,
        blocked_retries=blocked_retries,
    )
    if notes:
        return _finish(notes, via="http")
    if page is not None and auth_miss:
        log.warning(
            "Chart notes HTTP looked like auth for patient %s — retrying via page",
            patient_id,
        )
        if _obs is not None:
            _obs.set_auth_healthy(False)
            _obs.emit(
                event="retry",
                level="WARN",
                operation="note_index",
                webpt_patient_id=str(patient_id),
                decision="retry_via_page",
                decision_reason="http_auth_miss",
                error_type="AuthExpired",
                error_expected=True,
            )
        return _finish(
            await _fetch_chart_notes_via_page(
                page,
                context,
                patient_id=patient_id,
                case_id=case_id,
                url=url,
                config=config,
                timeout_ms=timeout_ms,
                retries=retries,
                page_lock=page_lock,
                session_lock=session_lock,
                debug_dir=debug_dir,
            ),
            via="page_after_http_auth_miss",
        )
    return _finish(notes, via="http_empty")


async def _fetch_chart_notes_via_http(
    context: BrowserContext,
    *,
    patient_id: int,
    case_id: int,
    url: str,
    debug_dir: Path | None,
    timeout_ms: int,
    retries: int,
    blocked_retries: int,
) -> tuple[list[ChartNoteRef], bool]:
    """Return (notes, auth_miss). auth_miss means empty parse + login-like HTML."""
    referer = patient_chart_note_url(patient_id, case_id)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Referer": referer,
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
                notes = parse_chart_notes_html(html, case_id=case_id)
                if not notes:
                    _maybe_save_debug_html(
                        html,
                        patient_id=patient_id,
                        case_id=case_id,
                        debug_dir=debug_dir,
                    )
                    return notes, _html_looks_like_auth(html)
                return notes, False
            last_error = f"HTTP {response.status}"
            if response.status in (403, 429):
                if blocked_attempt < blocked_retries:
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
            return [], False
    log.warning(
        "Chart notes fetch failed patient %s case %s: %s",
        patient_id,
        case_id,
        last_error,
    )
    return [], False


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
