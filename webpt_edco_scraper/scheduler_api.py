import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from playwright.async_api import BrowserContext

from auth import SessionState, ajax_headers
from config import SCHEDULER_DATA_URL, SCHEDULER_INDEX_URL, WebPTConfig
from logging_config import get_logger

log = get_logger("scheduler_api")

TITLE_PATTERN = re.compile(
    r"^(?P<name>.+?)\s*-\s*(?P<dob>\d{1,2}/\d{1,2}/\d{4})\s*-\s*(?P<case>\(.+\))$"
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


def is_patient_appointment(event: dict[str, Any]) -> bool:
    """True when event represents a patient appointment (not a clinic block)."""
    try:
        return int(event.get("p_id") or 0) > 0
    except (TypeError, ValueError):
        return False


def parse_patient_title(title: str) -> tuple[str, str, str]:
    """Split scheduler title like 'LAST, FIRST - MM/DD/YYYY - (Default)'."""
    match = TITLE_PATTERN.match((title or "").strip())
    if not match:
        return (title or "").strip(), "", ""
    return match.group("name").strip(), match.group("dob"), match.group("case").strip()


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


def extract_patients_from_events(
    events: list[dict[str, Any]],
    *,
    facility_id: int | str,
    reference_date: date | None = None,
) -> list[SchedulerPatient]:
    """Dedupe by patient_id within a facility; aggregate past/upcoming dates."""
    from zoneinfo import ZoneInfo

    fid = int(facility_id)
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("US/Eastern")).date()

    by_patient: dict[int, SchedulerPatient] = {}

    for event in events:
        if not is_patient_appointment(event):
            continue
        pid = int(event["p_id"])
        appt_date = _event_date(event)

        if pid not in by_patient:
            name, dob, case_label = parse_patient_title(str(event.get("title") or ""))
            case_raw = event.get("case_id")
            case_id = int(case_raw) if case_raw not in (None, "", 0, "0") else None
            by_patient[pid] = SchedulerPatient(
                patient_id=pid,
                facility_id=fid,
                case_id=case_id,
                patient_name=name,
                dob=dob,
                case_label=case_label,
                ins_name=str(event.get("ins_name") or ""),
            )

        patient = by_patient[pid]
        patient.appointment_count += 1
        if appt_date and appt_date not in patient.appointment_dates:
            patient.appointment_dates.append(appt_date)

        if appt_date:
            if _is_past_appointment(appt_date, reference_date=reference_date):
                if appt_date not in patient.appointments_past_dates:
                    patient.appointments_past_dates.append(appt_date)
                patient.appointments_past_count += 1
            else:
                if appt_date not in patient.appointments_upcoming_dates:
                    patient.appointments_upcoming_dates.append(appt_date)
                patient.appointments_upcoming_count += 1

        if not patient.ins_name and event.get("ins_name"):
            patient.ins_name = str(event["ins_name"])

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
    response = await context.request.post(
        SCHEDULER_DATA_URL,
        form=form,
        headers=headers,
    )
    if not response.ok:
        text = await response.text()
        raise RuntimeError(
            f"Scheduler POST failed HTTP {response.status} "
            f"(facility={facility_id}): {text[:200]}"
        )
    body = await response.json()
    events = body.get("events") or []
    log.info(
        "Scheduler facility=%s %s..%s -> %d events",
        facility_id,
        start_date,
        end_date,
        len(events),
    )
    return events


def resolve_date_range(
    *,
    days: int,
    end_date: date | None,
    timezone: str,
    lookahead_days: int | None = None,
) -> tuple[date, date, date]:
    """Return (start_date, range_end, reference_date) for past/upcoming split."""
    from zoneinfo import ZoneInfo

    reference_date = (
        end_date if end_date is not None else datetime.now(ZoneInfo(timezone)).date()
    )
    if days < 1:
        raise ValueError("--days must be >= 1")
    look = lookahead_days if lookahead_days is not None else days
    if look < 0:
        raise ValueError("--lookahead-days must be >= 0")
    start = reference_date.fromordinal(reference_date.toordinal() - (days - 1))
    range_end = reference_date.fromordinal(reference_date.toordinal() + look)
    return start, range_end, reference_date
