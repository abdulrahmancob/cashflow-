"""Normalization utilities for Waystar denials output data model."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

NAME_ID_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<id>[^)]+)\)\s*$")
CARC_RE = re.compile(r"\b(?P<group>CO|PR|PI|OA)-(?P<num>[A-Z0-9]+)\b", re.I)
RARC_RE = re.compile(r"\b(?P<code>[A-Z]\d+)\b")
MONEY_RE = re.compile(r"[^0-9.\-]+")
NOTE_DATE_RE = re.compile(r"\(\s*(\d{1,2}/\d{1,2}/\d{4})\s*\)")


def parse_money(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return 0.0
    negative = "(" in raw and ")" in raw
    cleaned = MONEY_RE.sub("", raw.replace("(", "").replace(")", ""))
    try:
        amount = float(cleaned or 0)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def format_money(value: Any) -> str:
    return f"{parse_money(value):.2f}"


def parse_date_iso(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    raw = str(value).strip()
    if not raw:
        return default
    if "T" in raw and len(raw) >= 10:
        return raw[:10]
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return default


def parse_snapshot_date(value: Any) -> str:
    if not value:
        return date.today().isoformat()
    raw = str(value).strip()
    if "T" in raw:
        return raw[:10]
    return parse_date_iso(raw, default=date.today().isoformat())


def days_between(start_iso: str, end_iso: str) -> int | None:
    if not start_iso or not end_iso:
        return None
    try:
        start = date.fromisoformat(start_iso)
        end = date.fromisoformat(end_iso)
        return (end - start).days
    except ValueError:
        return None


def aging_bucket(days: int | None) -> str:
    if days is None:
        return ""
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def parse_name_id(value: str) -> tuple[str, str]:
    text = " ".join((value or "").split())
    if not text:
        return "", ""
    match = NAME_ID_RE.match(text)
    if match:
        return match.group("name").strip(), match.group("id").strip()
    return text, ""


def normalize_claim_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "0":
        return ""
    return raw


def parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    return raw in {"true", "1", "yes", "y"}


def extract_carc_codes(value: str) -> list[str]:
    if not value:
        return []
    return [f"{m.group('group').upper()}-{m.group('num').upper()}" for m in CARC_RE.finditer(value)]


def extract_rarc_codes(value: str) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    codes: list[str] = []
    for match in RARC_RE.finditer(value.upper()):
        code = match.group("code")
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def split_pipe_codes(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"\s*\|\|\s*", value) if part.strip()]


def parse_carc_parts(carc_code: str) -> tuple[str, str]:
    codes = extract_carc_codes(carc_code)
    if not codes:
        return "", ""
    first = codes[0]
    match = CARC_RE.match(first)
    if not match:
        return "", first
    return match.group("group").upper(), match.group("num").upper()


def build_line_key(denial_id: str, remit_instance_id: str, line_num: Any) -> str:
    return f"{denial_id}|{remit_instance_id}|{line_num}"


def parse_last_note(value: str) -> tuple[str, str]:
    text = " ".join((value or "").split())
    if not text:
        return "", ""
    match = NOTE_DATE_RE.search(text)
    if not match:
        return "", text
    note_date = parse_date_iso(match.group(1))
    return note_date, text


def parse_appeal_level(value: str) -> int | None:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else None


def normalize_stage(stage: str) -> str:
    return " ".join((stage or "").split())


def denial_status_from_stage(stage: str) -> str:
    stage_lower = (stage or "").lower()
    if "appeal" in stage_lower:
        return "In Appeal"
    if stage_lower in {"updated", "resubmitted", "new"}:
        return "Open"
    if stage_lower in {"closed", "resolved"}:
        return "Closed"
    return "Open" if stage else "Unknown"


def is_reminder_overdue(reminder_date_iso: str, snapshot_date_iso: str) -> bool:
    if not reminder_date_iso or not snapshot_date_iso:
        return False
    try:
        return date.fromisoformat(reminder_date_iso) <= date.fromisoformat(snapshot_date_iso)
    except ValueError:
        return False


def sla_status(stage: str, days_in_phase: Any) -> str:
    try:
        days = int(str(days_in_phase or "0"))
    except ValueError:
        days = 0
    stage_lower = (stage or "").lower()
    threshold = 30 if "appeal" in stage_lower else 45
    if days > threshold:
        return "Breached"
    if days > threshold - 7:
        return "At Risk"
    return "On Track"


def data_quality_flags(row: dict[str, Any]) -> str:
    flags: list[str] = []
    if not normalize_claim_id(row.get("claim_id")):
        flags.append("claim_id_missing")
    if not (row.get("payer_name") or row.get("payer")):
        flags.append("payer_missing")
    if row.get("detail_fetch_error"):
        flags.append("detail_fetch_failed")
    if not row.get("carc_codes") and row.get("proc_code"):
        flags.append("carc_missing")
    return " || ".join(flags)


def normalize_payer_fields(row: dict[str, Any]) -> dict[str, str]:
    payer_name, payer_id = parse_name_id(str(row.get("payer") or row.get("payer_name") or ""))
    remit_name, remit_id = parse_name_id(str(row.get("remit_payer") or ""))
    return {
        "payer_name": payer_name or remit_name,
        "payer_id": payer_id or remit_id,
        "remit_payer_name": remit_name or payer_name,
        "remit_payer_id": remit_id or payer_id,
    }


def normalize_provider_fields(row: dict[str, Any]) -> dict[str, str]:
    rendering_name, rendering_npi = parse_name_id(str(row.get("provider") or ""))
    billing_name, billing_npi = parse_name_id(str(row.get("payee_npi") or ""))
    return {
        "rendering_provider_name": rendering_name,
        "rendering_provider_npi": rendering_npi,
        "billing_provider_name": billing_name,
        "billing_provider_npi": billing_npi,
    }
