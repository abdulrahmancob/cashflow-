"""Parse official Waystar Claims Screen CSV and claim-number keys.

Stdlib-only so the scraper and cashflow_db loaders can share it.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

EXCEL_FORMULA_RE = re.compile(r'^="(.*)"$')
PV4_PREFIX_RE = re.compile(r"^PV4", re.IGNORECASE)
REMIT_SPLIT_RE = re.compile(r"[;,\s|/]+")
CHECK_SEP_RE = re.compile(r"[\s\-]")

OFFICIAL_CSV_COLUMNS = (
    "Trans Date",
    "Patient Name",
    "Claim Number",
    "Claim ID",
    "Instance ID",
    "Payer Name",
    "Payer ID",
    "From Date",
    "To Date",
    "Rendering Provider",
    "Charges",
    "Status",
    "Sequence",
    "Source",
    "Hidden",
    "Remit #s",
    "Total Remit Amount",
    "Last Note",
    "Last Event Message",
)

PROBE_CANDIDATE_PATHS = (
    "/Claims/Listing/DownloadCsv",
    "/Claims/Listing/DownloadCSV",
    "/Claims/Listing/Download",
    "/Claims/Listing/ExportCsv",
    "/Claims/Listing/ExportCSV",
    "/Claims/Listing/Export",
    "/Claims/Listing/ExportToCsv",
)

DATE_WINDOW_FALLBACK = "date_windows"
OFFSET_CSV = "offset_csv"
SINGLE_CSV = "single_csv"
UNKNOWN_STRATEGY = "unknown"


class CompletenessError(RuntimeError):
    """Pulled row count does not match the UI result count."""


def unwrap_excel_formula(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    match = EXCEL_FORMULA_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith("="):
        return text[1:].strip().strip('"')
    return text


def strip_pv4(claim_number: Any) -> str:
    """PV410582026 → 10582026. Also unwraps Excel `="PV4..."`. """
    text = unwrap_excel_formula(claim_number)
    text = PV4_PREFIX_RE.sub("", text).strip()
    return normalize_id_key(text)


def normalize_id_key(value: Any) -> str:
    text = unwrap_excel_formula(value)
    if not text:
        return ""
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def compact_check_ref(value: Any) -> str:
    text = unwrap_excel_formula(value).upper()
    compact = CHECK_SEP_RE.sub("", text)
    if compact.isdigit():
        return compact.lstrip("0") or "0"
    return compact


def parse_remit_numbers(value: Any) -> list[str]:
    raw = unwrap_excel_formula(value)
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in REMIT_SPLIT_RE.split(raw):
        token = part.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _parse_money(value: Any) -> float | None:
    text = unwrap_excel_formula(value).replace("$", "").replace(",", "").strip()
    if not text or text in {"-", "N/A", "NA"}:
        return None
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1].strip()
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if neg else amount


def _parse_int(value: Any) -> int | None:
    text = unwrap_excel_formula(value)
    if not text:
        return None
    try:
        return int(float(text.replace(",", "")))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    text = unwrap_excel_formula(value)
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})$", text)
    if not match:
        return None
    month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _normalize_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("\ufeff", "").strip())


def _locate_header_index(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        stripped = line.lstrip("\ufeff").lstrip()
        if stripped.lower().startswith("trans date,"):
            return i
    raise ValueError("Claims Screen CSV is missing a 'Trans Date' header row")


def iter_claims_screen_rows(path: Path) -> Iterator[dict[str, str]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.splitlines()
    header_at = _locate_header_index(lines)
    buf = io.StringIO("\n".join(lines[header_at:]) + "\n")
    reader = csv.DictReader(buf)
    if not reader.fieldnames:
        return
    fieldnames = [_normalize_header(name) for name in reader.fieldnames]
    reader.fieldnames = fieldnames
    for row in reader:
        yield {(_normalize_header(k) if k else ""): (v or "") for k, v in row.items()}


def parse_claims_screen_row(row: dict[str, str]) -> dict[str, Any] | None:
    instance_id = unwrap_excel_formula(row.get("Instance ID") or row.get("InstanceID"))
    claim_number = unwrap_excel_formula(row.get("Claim Number"))
    if not instance_id and not claim_number:
        return None
    from_date = _parse_date(row.get("From Date"))
    claim_key = strip_pv4(claim_number)
    remit_numbers = parse_remit_numbers(row.get("Remit #s") or row.get("Remit #s ".strip()))
    return {
        "trans_date": _parse_date(row.get("Trans Date")),
        "patient_name": unwrap_excel_formula(row.get("Patient Name")),
        "claim_number": claim_number,
        "claim_key": claim_key,
        "claim_id": unwrap_excel_formula(row.get("Claim ID")),
        "instance_id": instance_id,
        "payer_name": unwrap_excel_formula(row.get("Payer Name")),
        "payer_id": unwrap_excel_formula(row.get("Payer ID")),
        "from_date": from_date,
        "to_date": _parse_date(row.get("To Date")),
        "rendering_provider": unwrap_excel_formula(row.get("Rendering Provider")),
        "charges": _parse_money(row.get("Charges")),
        "status": unwrap_excel_formula(row.get("Status")),
        "sequence": _parse_int(row.get("Sequence")) or 1,
        "source": unwrap_excel_formula(row.get("Source")),
        "hidden": unwrap_excel_formula(row.get("Hidden")),
        "remit_numbers": remit_numbers,
        "total_remit_amount": _parse_money(row.get("Total Remit Amount")),
        "last_note": unwrap_excel_formula(row.get("Last Note")),
        "last_event_message": unwrap_excel_formula(row.get("Last Event Message")),
    }


def parse_claims_screen_csv(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in iter_claims_screen_rows(path):
        parsed = parse_claims_screen_row(raw)
        if parsed is None:
            continue
        rows.append(parsed)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def merge_claim_rows(batches: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    by_instance: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for batch in batches:
        for row in batch:
            key = str(row.get("instance_id") or "").strip()
            if not key:
                extras.append(row)
                continue
            by_instance[key] = row
    return list(by_instance.values()) + extras


def completeness_gate(
    rows: list[dict[str, Any]],
    expected_total: int | None,
    *,
    max_missing: int = 0,
) -> dict[str, Any]:
    instance_ids = {str(r.get("instance_id") or "") for r in rows if r.get("instance_id")}
    pulled = len(instance_ids) if instance_ids else len(rows)
    report = {
        "pulled_rows": len(rows),
        "distinct_instance_ids": len(instance_ids),
        "expected_total": expected_total,
        "ok": True,
        "missing": 0,
    }
    if expected_total is None or expected_total <= 0:
        return report
    missing = expected_total - pulled
    report["missing"] = missing
    if missing > max_missing:
        report["ok"] = False
        raise CompletenessError(
            f"Waystar claims pull incomplete: got {pulled} distinct instance_id "
            f"vs UI total {expected_total} (missing {missing})"
        )
    return report


def bisect_date_windows(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]]:
    if end < start:
        raise ValueError("end before start")
    span = (end - start).days
    mid = start + timedelta(days=span // 2)
    left_end = mid
    right_start = mid + timedelta(days=1)
    if right_start > end:
        right_start = end
        left_end = end - timedelta(days=1) if end > start else end
    return (start, left_end), (right_start, end)


def format_mdy(value: date) -> str:
    return value.strftime("%m/%d/%Y")


RECENT_LOOKBACK_DAYS = 60
RECENT_MAX_LOOKBACK_DAYS = 365
DEFAULT_RECENT_MONTHS_BACK = 2


def months_back_window(end: date, months_back: int) -> tuple[date, date]:
    """First day of (end's month minus ``months_back``) through ``end`` inclusive.

    ``months_back=2`` on 2026-08-24 → 2026-06-01 .. 2026-08-24.
    """
    months_back = max(int(months_back), 0)
    year, month = end.year, end.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), end


def choose_recent_window(
    end: date,
    *,
    csv_cap: int = 10_000,
    lookback_days: int = RECENT_LOOKBACK_DAYS,
    max_lookback_days: int = RECENT_MAX_LOOKBACK_DAYS,
    count_fn=None,
) -> tuple[date, date]:
    """Pick a recent [start, end] window whose UI count is at most csv_cap.

    Starts at ``lookback_days``. If the count is over the cap, shrinks toward
    ``end``. If under the cap, expands up to ``max_lookback_days``. When
    ``count_fn`` is omitted the initial lookback is returned unchanged.
    """
    lookback_days = max(int(lookback_days), 0)
    max_lookback_days = max(int(max_lookback_days), lookback_days)
    initial_start = end - timedelta(days=lookback_days)
    if count_fn is None:
        return initial_start, end

    def count_days(days: int) -> int | None:
        start = end - timedelta(days=max(days, 0))
        return count_fn(start, end)

    n = count_days(lookback_days)
    if n is None:
        return initial_start, end

    if n <= csv_cap:
        lo, hi = lookback_days, max_lookback_days
        best = lookback_days
        while lo <= hi:
            mid = (lo + hi) // 2
            mid_n = count_days(mid)
            if mid_n is None or mid_n > csv_cap:
                hi = mid - 1
            else:
                best = mid
                lo = mid + 1
        return end - timedelta(days=best), end

    lo, hi = 0, lookback_days
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        mid_n = count_days(mid)
        if mid_n is None or mid_n > csv_cap:
            hi = mid - 1
        else:
            best = mid
            lo = mid + 1
    return end - timedelta(days=best), end


def write_probe_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = json.loads(json.dumps(payload, default=str))
    for key in ("cookies", "cookie", "set_cookie", "authorization"):
        sanitized.pop(key, None)
    path.write_text(json.dumps(sanitized, indent=2) + "\n", encoding="utf-8")


def load_probe_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def choose_download_strategy(probe: dict[str, Any]) -> str:
    if probe.get("offset_works"):
        return OFFSET_CSV
    if probe.get("full_csv_under_cap"):
        return SINGLE_CSV
    if probe.get("csv_row_cap") and probe.get("ui_total", 0) > int(probe["csv_row_cap"]):
        return DATE_WINDOW_FALLBACK
    if probe.get("download_url") and probe.get("content_type", "").startswith("text/csv"):
        return SINGLE_CSV
    return UNKNOWN_STRATEGY
