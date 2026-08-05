import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from playwright.async_api import BrowserContext

from auth import SessionState, ajax_headers
from config import SCHEDULER_DATA_URL, SCHEDULER_INDEX_URL, WebPTConfig
from http_utils import is_transient_network_error, retry_delay_sec
from logging_config import get_logger

log = get_logger("scheduler_api")

_SCHEDULER_TRANSIENT_RETRIES = 3
_SCHEDULER_TRANSIENT_BASE_SEC = 2.0
# Large date windows (e.g. 90d) routinely exceed Playwright's 30s default.
_SCHEDULER_POST_TIMEOUT_MS = 120_000

# Name [- DOB] - (case) [optional trailing junk like *COLLECTIONS*]
TITLE_PATTERN = re.compile(
    r"^(?P<name>.+?)"
    r"(?:\s*-\s*(?P<dob>\d{1,2}/\d{1,2}/\d{4}))?"
    r"\s*-\s*(?P<case>\([^)]*\))"
    r"(?:\s+.*)?$"
)


@dataclass
class SchedulerPatient:
    patient_id: int
    facility_id: int
    case_id: int | None = None
    patient_name: str = ""
    dob: str = ""
    case_label: str = ""
    ins_name: str = ""
    appointment_count: int = 0
    appointment_dates: list[str] = field(default_factory=list)
    appointments_past_count: int = 0
    appointments_past_dates: list[str] = field(default_factory=list)
    appointments_upcoming_count: int = 0
    appointments_upcoming_dates: list[str] = field(default_factory=list)


# Live scheduler probe (Allerton 2026-07-23): status=4 ↔ checkout_time set,
# status=5 ↔ checkin only, status=6 ↔ neither. Prefer checkout_time as ground truth.
CHECKED_OUT_STATUS_CODES = frozenset({4})
STATUS_CODE_LABELS: dict[int, str] = {
    4: "Checked Out",
    5: "Checked In",
    6: "Cancelled/No Show",
}


@dataclass
class CheckoutVisit:
    """One checked-out appointment at a specific facility/case (visit-level)."""

    patient_id: int
    facility_id: int
    case_id: int | None
    case_label: str
    patient_name: str
    dob: str
    ins_name: str
    appointment_at: str
    visit_status: str
    checkin_time: str = ""
    checkout_time: str = ""
    appointment_id: int | None = None


def is_patient_appointment(event: dict[str, Any]) -> bool:
    """True when event represents a patient appointment (not a clinic block)."""
    try:
        return int(event.get("p_id") or 0) > 0
    except (TypeError, ValueError):
        return False


def parse_patient_title(title: str) -> tuple[str, str, str]:
    """Split scheduler title like 'LAST, FIRST - MM/DD/YYYY - (Default)'."""
    raw = (title or "").strip()
    match = TITLE_PATTERN.match(raw)
    if not match:
        return raw, "", ""
    return (
        match.group("name").strip(),
        (match.group("dob") or "").strip(),
        match.group("case").strip(),
    )


def _event_date(event: dict[str, Any]) -> str:
    raw = event.get("start_date") or event.get("startDate") or ""
    return str(raw).strip()


def _parse_event_datetime(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(raw[:size], fmt)
        except ValueError:
            continue
    return None


def _is_past_appointment(appt_date: str, *, reference_date: date) -> bool:
    dt = _parse_event_datetime(appt_date)
    if dt is None:
        return True
    return dt.date() < reference_date


def _parse_case_id(raw: Any) -> int | None:
    if raw in (None, "", 0, "0"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _nonempty_str(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text or text.lower() in ("none", "null"):
        return ""
    return text


def _status_code(event: dict[str, Any]) -> int | None:
    raw = event.get("status")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# Extra keys we want on the case export when present in scheduler JSON.
SCHEDULER_EXTRA_KEYS = (
    "copay",
    "auth_visits",
    "visit_num",
    "apt_type",
    "paid",
    "length",
    "provider",
    "room",
    "resource",
    "color",
    "reason",
    "recurring",
    "cancel_reason",
    "no_show_reason",
    "visit_type",
)


def scheduler_event_extras(event: dict[str, Any]) -> dict[str, str]:
    """Pull optional scheduler fields; empty string when absent (no invention)."""
    out: dict[str, str] = {}
    for key in SCHEDULER_EXTRA_KEYS:
        out[key] = _nonempty_str(event.get(key))
    # Alternate spellings seen in the wild
    if not out.get("visit_type"):
        out["visit_type"] = _nonempty_str(event.get("visitType") or event.get("type"))
    if not out.get("length"):
        out["length"] = _nonempty_str(event.get("event_length") or event.get("duration"))
    return out


def discover_scheduler_keys(events: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for k in ev:
            if k not in seen:
                seen.add(k)
                keys.append(str(k))
    return keys


def parse_visit_status(event: dict[str, Any]) -> str:
    """Human-readable visit status from scheduler event fields."""
    checkout = _nonempty_str(event.get("checkout_time"))
    if checkout:
        return "Checked Out"
    checkin = _nonempty_str(event.get("checkin_time"))
    code = _status_code(event)
    if code in CHECKED_OUT_STATUS_CODES:
        return "Checked Out"
    if checkin:
        return "Checked In"
    if code is not None and code in STATUS_CODE_LABELS:
        return STATUS_CODE_LABELS[code]
    if code is not None:
        return str(code)
    return "Other"


def is_checked_out(event: dict[str, Any]) -> bool:
    """True when the appointment was actually checked out in WebPT Scheduler."""
    if _nonempty_str(event.get("checkout_time")):
        return True
    return _status_code(event) in CHECKED_OUT_STATUS_CODES


def _event_service_date(event: dict[str, Any]) -> date | None:
    dt = _parse_event_datetime(_event_date(event))
    return dt.date() if dt is not None else None


def extract_schedule_visits(
    events: list[dict[str, Any]],
    *,
    facility_id: int | str,
    start_date: date,
    end_date: date,
    checked_out_only: bool = False,
) -> list[CheckoutVisit]:
    """Visit-level rows for patient appointments in ``[start_date, end_date]``.

    Keeps the event's own facility/case (no cross-case/latest-case collapse).
    Dedupes identical (patient_id, appointment_at, case_id) slots.
    When ``checked_out_only`` is True, keeps only Checked Out visits.
    """
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")

    fid = int(facility_id)
    visits: list[CheckoutVisit] = []
    seen: set[tuple[int, str, int | None]] = set()

    for event in events:
        if not is_patient_appointment(event):
            continue
        if checked_out_only and not is_checked_out(event):
            continue
        service = _event_service_date(event)
        if service is None or service < start_date or service > end_date:
            continue

        pid = int(event["p_id"])
        appt_at = _event_date(event)
        case_id = _parse_case_id(event.get("case_id"))
        dedupe_key = (pid, appt_at, case_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        name, dob, case_label = parse_patient_title(str(event.get("title") or ""))
        if not name:
            name = _nonempty_str(event.get("patientName")) or _nonempty_str(
                event.get("payment_title")
            )

        appt_id_raw = event.get("appointment_id") or event.get("id")
        try:
            appointment_id = (
                int(appt_id_raw) if appt_id_raw not in (None, "", 0, "0") else None
            )
        except (TypeError, ValueError):
            appointment_id = None

        visits.append(
            CheckoutVisit(
                patient_id=pid,
                facility_id=fid,
                case_id=case_id,
                case_label=case_label,
                patient_name=name,
                dob=dob,
                ins_name=str(event.get("ins_name") or ""),
                appointment_at=appt_at,
                visit_status=parse_visit_status(event),
                checkin_time=_nonempty_str(event.get("checkin_time")),
                checkout_time=_nonempty_str(event.get("checkout_time")),
                appointment_id=appointment_id,
            )
        )

    visits.sort(key=lambda v: (v.appointment_at, v.patient_id, v.case_id or 0))
    return visits


def extract_checkout_visits(
    events: list[dict[str, Any]],
    *,
    facility_id: int | str,
    service_date: date,
) -> list[CheckoutVisit]:
    """Visit-level rows for Checked Out appointments on ``service_date``."""
    return extract_schedule_visits(
        events,
        facility_id=facility_id,
        start_date=service_date,
        end_date=service_date,
        checked_out_only=True,
    )


def reclassify_appointment_dates(
    dates: list[str],
    *,
    reference_date: date,
) -> tuple[list[str], list[str], int, int]:
    """Split unique appointment datetimes into past/upcoming vs reference_date.

    Returns (past_dates, upcoming_dates, past_count, upcoming_count).
    Counts match unique datetime strings (same as date list lengths).
    """
    unique: list[str] = []
    seen: set[str] = set()
    for raw in dates:
        d = (raw or "").strip()
        if not d or d in seen:
            continue
        seen.add(d)
        unique.append(d)
    unique.sort()

    past: list[str] = []
    upcoming: list[str] = []
    for d in unique:
        if _is_past_appointment(d, reference_date=reference_date):
            past.append(d)
        else:
            upcoming.append(d)
    return past, upcoming, len(past), len(upcoming)


def extract_patients_from_events(
    events: list[dict[str, Any]],
    *,
    facility_id: int | str,
    reference_date: date | None = None,
) -> list[SchedulerPatient]:
    """Dedupe by patient_id within a facility; aggregate past/upcoming dates.

    Appointment counts use unique datetimes (aligned with date lists).
    case_id / title / ins_name prefer the latest appointment that has them.
    """
    from zoneinfo import ZoneInfo

    fid = int(facility_id)
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("US/Eastern")).date()

    by_patient: dict[int, SchedulerPatient] = {}
    # Track best (latest) metadata: (appt_dt_or_min, case_id/name/dob/ins)
    best_meta_dt: dict[int, datetime] = {}

    for event in events:
        if not is_patient_appointment(event):
            continue
        pid = int(event["p_id"])
        appt_date = _event_date(event)
        appt_dt = _parse_event_datetime(appt_date) if appt_date else None
        case_id = _parse_case_id(event.get("case_id"))
        name, dob, case_label = parse_patient_title(str(event.get("title") or ""))
        ins_name = str(event.get("ins_name") or "")

        if pid not in by_patient:
            by_patient[pid] = SchedulerPatient(
                patient_id=pid,
                facility_id=fid,
                case_id=case_id,
                patient_name=name,
                dob=dob,
                case_label=case_label,
                ins_name=ins_name,
            )
            if appt_dt is not None:
                best_meta_dt[pid] = appt_dt
        else:
            patient = by_patient[pid]
            prior_dt = best_meta_dt.get(pid)
            is_newer = appt_dt is not None and (prior_dt is None or appt_dt >= prior_dt)
            if is_newer:
                best_meta_dt[pid] = appt_dt  # type: ignore[assignment]
                if case_id is not None:
                    patient.case_id = case_id
                if name:
                    patient.patient_name = name
                if dob:
                    patient.dob = dob
                if case_label:
                    patient.case_label = case_label
                if ins_name:
                    patient.ins_name = ins_name
            else:
                # Fill blanks from any event when first/best left them empty.
                if patient.case_id is None and case_id is not None:
                    patient.case_id = case_id
                if not patient.dob and dob:
                    patient.dob = dob
                if not patient.patient_name and name:
                    patient.patient_name = name
                if not patient.case_label and case_label:
                    patient.case_label = case_label
                if not patient.ins_name and ins_name:
                    patient.ins_name = ins_name

        patient = by_patient[pid]

        # Unique datetime aggregation (counts == date list lengths).
        if not appt_date or appt_date in patient.appointment_dates:
            continue

        patient.appointment_dates.append(appt_date)
        patient.appointment_count += 1
        if _is_past_appointment(appt_date, reference_date=reference_date):
            patient.appointments_past_dates.append(appt_date)
            patient.appointments_past_count += 1
        else:
            patient.appointments_upcoming_dates.append(appt_date)
            patient.appointments_upcoming_count += 1

    for patient in by_patient.values():
        patient.appointment_dates.sort()
        patient.appointments_past_dates.sort()
        patient.appointments_upcoming_dates.sort()

    return sorted(by_patient.values(), key=lambda p: p.patient_id)


def _iso_date(d: date) -> str:
    return f"{d.isoformat()}T00:00:00"


async def fetch_scheduler_events(
    context: BrowserContext,
    *,
    facility_id: int | str,
    start_date: date,
    end_date: date,
    session: SessionState,
    config: WebPTConfig,
) -> list[dict[str, Any]]:
    """POST scheduler week view and return raw events list."""
    form = {
        "startDate": _iso_date(start_date),
        "endDate": _iso_date(end_date),
        "single_start_date": "",
        "facility_id": str(facility_id),
        "xaction": "read",
    }
    headers = ajax_headers(session.csrf_token, SCHEDULER_INDEX_URL)

    last_exc: BaseException | None = None
    for attempt in range(_SCHEDULER_TRANSIENT_RETRIES):
        try:
            response = await context.request.post(
                SCHEDULER_DATA_URL,
                form=form,
                headers=headers,
                timeout=_SCHEDULER_POST_TIMEOUT_MS,
            )
            if not response.ok:
                text = await response.text()
                raise RuntimeError(
                    f"Scheduler POST failed HTTP {response.status} "
                    f"(facility={facility_id}): {text[:200]}"
                )
            text = await response.text()
            if not (text or "").strip():
                raise RuntimeError(
                    f"Scheduler empty body facility={facility_id} "
                    f"{start_date}..{end_date}"
                )
            try:
                body = __import__("json").loads(text)
            except Exception as exc:
                raise RuntimeError(
                    f"Scheduler invalid JSON facility={facility_id}: {text[:200]}"
                ) from exc
            events = body.get("events") or []
            log.info(
                "Scheduler facility=%s %s..%s -> %d events",
                facility_id,
                start_date,
                end_date,
                len(events),
            )
            return events
        except Exception as exc:
            last_exc = exc
            if (
                (
                    is_transient_network_error(exc)
                    or "empty body" in str(exc).lower()
                    or "invalid json" in str(exc).lower()
                )
                and attempt < _SCHEDULER_TRANSIENT_RETRIES - 1
            ):
                wait = retry_delay_sec(attempt, base=_SCHEDULER_TRANSIENT_BASE_SEC)
                # Avoid logging Playwright call-log arrows (break cp1252 consoles).
                err_summary = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                log.warning(
                    "Scheduler transient error facility=%s (attempt %d/%d): %s - retry in %.1fs",
                    facility_id,
                    attempt + 1,
                    _SCHEDULER_TRANSIENT_RETRIES,
                    err_summary,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            raise

    assert last_exc is not None
    raise last_exc


def resolve_date_range(
    *,
    days: int,
    end_date: date | None,
    timezone: str,
    lookahead_days: int | None = None,
    as_of: date | None = None,
) -> tuple[date, date, date]:
    """Return (start_date, range_end, reference_date) for past/upcoming split.

    ``end_date`` (or today) anchors the fetch window lookback/lookahead.
    ``as_of`` is the past/upcoming cutoff and defaults to **today** in
    ``timezone``, not to ``end_date``. For a historical dump that should
    classify everything as past, pass ``as_of=end_date`` explicitly.
    """
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(timezone)).date()
    window_anchor = end_date if end_date is not None else today
    reference_date = as_of if as_of is not None else today
    if days < 1:
        raise ValueError("--days must be >= 1")
    look = lookahead_days if lookahead_days is not None else days
    if look < 0:
        raise ValueError("--lookahead-days must be >= 0")
    start = window_anchor.fromordinal(window_anchor.toordinal() - (days - 1))
    range_end = window_anchor.fromordinal(window_anchor.toordinal() + look)
    return start, range_end, reference_date
