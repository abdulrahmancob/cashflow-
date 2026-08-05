"""Staged offline enrich — parse_html → parse_json → payments → ocr → merge → export.

Each stage is independently re-runnable; prefer raw/ + payments/ over live WebPT.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from case_artifact_contract import (
    flatten_provenance,
    provenance_field,
    read_json,
    update_audit,
    write_case_sources,
    write_json,
)
from case_paths import case_root, meta_path
from case_payments_stage import store_payments_from_json
from case_raw_capture import raw_coverage, raw_dir
from edoc_ocr import (
    extract_extended_pdf_fields,
    extract_patient_fields,
    load_or_run_patient_ocr,
)
from logging_config import get_logger
from patient_chart_api import chart_info_to_export_fields, parse_patient_chart_html
from scheduler_api import (
    discover_scheduler_keys,
    is_patient_appointment,
    parse_patient_title,
    scheduler_event_extras,
)

log = get_logger("case_enrich_parse")

STAGES = (
    "parse_html",
    "parse_json",
    "payments",
    "ocr",
    "merge",
    "export",
    "all",
)

CASE_EXPORT_EXTRA_FIELDS = [
    "facility_name",
    "ins_name",
    "physician",
    "physician_npi",
    "insurance",
    "address",
    "phone",
    "appointments_past_count",
    "appointments_past_dates",
    "appointments_upcoming_count",
    "appointments_upcoming_dates",
    "payments_total_charge",
    "payments_total_paid",
    "payments_balance",
    "payments_txn_count",
    "edoc_meta_count",
    "chart_notes_downloaded",
    "edoc_files_downloaded",
    "raw_coverage_pct",
    "enrich_source",
]

_RETURN_TO_DR_JUNK = re.compile(
    r"(?i)^\s*return\s+to\s+dr\.?\s*(date:?\s*)?(cancel\s*save)?\s*$"
)


def _clean_return_to_dr(val: Any) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "cancel" in low and "save" in low:
        return ""
    if _RETURN_TO_DR_JUNK.match(s):
        return ""
    # Label-only / no real date
    if re.match(r"(?i)^return\s+to\s+dr", s) and not re.search(
        r"\d{1,2}[/-]\d{1,2}|\d{4}-\d{2}-\d{2}", s
    ):
        return ""
    return s


def _lookup_facility_name(facility_id: str | int) -> str:
    fid = str(facility_id or "").strip()
    if not fid:
        return ""
    try:
        from snowflake_pull.facility_map import WEBPT_FACILITIES

        return str(WEBPT_FACILITIES.get(fid) or "")
    except ImportError:
        pass
    try:
        from facility_map import WEBPT_FACILITIES  # type: ignore

        return str(WEBPT_FACILITIES.get(fid) or "")
    except ImportError:
        return ""


def _appt_date_is_past(appt_date: str, *, reference_date: date) -> bool:
    raw = (appt_date or "").strip()
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(raw[:size], fmt).date() < reference_date
        except ValueError:
            continue
    return True


def _appointments_from_scheduler(
    sched: dict[str, Any],
    *,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Derive past/upcoming appointment columns from parsed scheduler events."""
    ref = reference_date or date.today()
    events = sched.get("events") if isinstance(sched, dict) else None
    if not isinstance(events, list):
        events = []
    past: list[str] = []
    upcoming: list[str] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        appt = str(ev.get("start_date") or "").strip()
        if not appt or appt in seen:
            continue
        seen.add(appt)
        if _appt_date_is_past(appt, reference_date=ref):
            past.append(appt)
        else:
            upcoming.append(appt)
    past.sort()
    upcoming.sort()
    return {
        "appointments_past_count": len(past),
        "appointments_past_dates": "; ".join(past),
        "appointments_upcoming_count": len(upcoming),
        "appointments_upcoming_dates": "; ".join(upcoming),
    }


def _first_scheduler_name(sched: dict[str, Any]) -> str:
    events = sched.get("events") if isinstance(sched, dict) else None
    if not isinstance(events, list):
        return ""
    for ev in events:
        if isinstance(ev, dict):
            name = str(ev.get("patient_name") or "").strip()
            if name and name.upper() != "SEED, PATIENT":
                return name
            if name:
                return name
    return ""


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_json_any(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def parsed_dir(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    root = case_root(base_dir, facility_id, case_id) / "parsed"
    root.mkdir(parents=True, exist_ok=True)
    return root


def parse_edoc_list_metadata(docs: list[dict[str, Any]]) -> dict[str, Any]:
    keys: list[str] = []
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        for k in doc:
            if k not in seen:
                seen.add(k)
                keys.append(str(k))
        rows.append({str(k): doc.get(k) for k in doc})
    return {
        "edoc_meta_count": len(rows),
        "edoc_meta_keys": "; ".join(keys),
        "documents": rows,
        "keys": keys,
    }


def parse_scheduler_raw(events: list[dict[str, Any]]) -> dict[str, Any]:
    extras_rows: list[dict[str, str]] = []
    # Accept full POST envelope
    if isinstance(events, dict):
        data = events.get("data") or events.get("events") or []
        events = data if isinstance(data, list) else []
    for ev in events or []:
        if not isinstance(ev, dict) or not is_patient_appointment(ev):
            continue
        name, dob, case_label = parse_patient_title(str(ev.get("title") or ""))
        extras_rows.append(
            {
                "appointment_id": str(ev.get("appointment_id") or ev.get("id") or ""),
                "patient_id": str(ev.get("p_id") or ""),
                "case_id": str(ev.get("case_id") or ""),
                "patient_name": name,
                "dob": dob,
                "case_label": case_label,
                "ins_name": str(ev.get("ins_name") or ""),
                "start_date": str(ev.get("start_date") or ev.get("startDate") or ""),
                "status": str(ev.get("status") or ""),
                "checkin_time": str(ev.get("checkin_time") or ""),
                "checkout_time": str(ev.get("checkout_time") or ""),
                **scheduler_event_extras(ev),
            }
        )
    flat_events = events if isinstance(events, list) else []
    return {
        "scheduler_event_count": len(extras_rows),
        "scheduler_keys": "; ".join(discover_scheduler_keys(flat_events)),
        "events": extras_rows,
    }


def stage_parse_html(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
) -> dict[str, Any]:
    case_dir = case_root(base_dir, facility_id, case_id)
    raw = raw_dir(base_dir, facility_id, case_id)
    out_dir = parsed_dir(base_dir, facility_id, case_id)
    html = _read_text(raw / "patientChart.cleaned.html") or _read_text(
        raw / "patientChart.html"
    )
    if not html:
        html = _read_text(raw / "chart_notes.cleaned.html") or _read_text(
            raw / "chart_notes.html"
        )
    fields: dict[str, Any] = {}
    if html:
        info = parse_patient_chart_html(html)
        flat = chart_info_to_export_fields(info)
        for k, v in flat.items():
            if v:
                fields[k] = provenance_field(v, source="patientChart", confidence=1.0)
    write_json(out_dir / "chart_fields.json", fields)
    update_audit(case_dir, flag="parse_html_complete", value=bool(fields))
    write_case_sources(case_dir)
    return {"fields": len(fields), "path": str(out_dir / "chart_fields.json")}


def stage_parse_json(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
) -> dict[str, Any]:
    case_dir = case_root(base_dir, facility_id, case_id)
    raw = raw_dir(base_dir, facility_id, case_id)
    out_dir = parsed_dir(base_dir, facility_id, case_id)

    sched = _read_json_any(raw / "scheduler.json")
    sched_out = parse_scheduler_raw(sched if isinstance(sched, (list, dict)) else [])
    write_json(out_dir / "scheduler_fields.json", sched_out)

    edocs = _read_json_any(raw / "edoc_list.json")
    edoc_out = parse_edoc_list_metadata(edocs if isinstance(edocs, list) else [])
    write_json(out_dir / "edoc_meta.json", edoc_out)

    ok = bool(sched_out.get("scheduler_event_count") or edoc_out.get("edoc_meta_count") or sched is not None or edocs is not None)
    update_audit(case_dir, flag="parse_json_complete", value=ok)
    write_case_sources(case_dir)
    return {
        "scheduler_events": sched_out.get("scheduler_event_count"),
        "edoc_meta_count": edoc_out.get("edoc_meta_count"),
    }


def stage_payments_offline(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
) -> dict[str, Any]:
    """Offline payments stage — uses payments/payments.json if present."""
    case_dir = case_root(base_dir, facility_id, case_id)
    pjson = case_dir / "payments" / "payments.json"
    data = _read_json_any(pjson)
    if isinstance(data, list) and data:
        result = store_payments_from_json(
            base_dir,
            facility_id=facility_id,
            case_id=case_id,
            transactions=data,
        )
        return result
    update_audit(
        case_dir,
        flag="payments_complete",
        value=False,
        error="payments.json missing — run deferred live payments pass",
    )
    return {"txn_count": 0, "skipped": True}


def stage_ocr(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    patient_name: str = "",
    patient_id: str = "",
    ocr_dpi: int = 200,
) -> dict[str, Any]:
    case_dir = case_root(base_dir, facility_id, case_id)
    out_dir = parsed_dir(base_dir, facility_id, case_id)
    pdfs = sorted(case_dir.rglob("*.pdf"))
    fields: dict[str, Any] = {}
    error = ""
    if not pdfs:
        error = "no PDFs"
    else:
        try:
            text, used, errors = load_or_run_patient_ocr(
                pdfs, patient_dir=case_dir, dpi=ocr_dpi
            )
            extracted = extract_patient_fields(
                text, expected_name=patient_name, expected_id=patient_id
            )
            extracted.update(extract_extended_pdf_fields(text))
            for k, v in extracted.items():
                if k.startswith("_"):
                    continue
                conf = 0.82 if v else 0.0
                fields[k] = provenance_field(v, source="ocr", confidence=conf)
            fields["edoc_ocr_source_files"] = provenance_field(
                "; ".join(used), source="ocr", confidence=1.0
            )
            if errors:
                fields["edoc_ocr_errors"] = provenance_field(
                    " | ".join(errors[:3]), source="ocr", confidence=1.0
                )
        except Exception as exc:
            error = str(exc)
            log.warning("OCR stage failed %s/%s: %s", facility_id, case_id, exc)
    write_json(out_dir / "ocr_fields.json", fields)
    update_audit(
        case_dir, flag="ocr_complete", value=bool(fields), error=error
    )
    write_case_sources(case_dir)
    return {"fields": len(fields), "error": error}


def stage_merge(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    patient_id: str = "",
    patient_name: str = "",
) -> dict[str, Any]:
    case_dir = case_root(base_dir, facility_id, case_id)
    out_dir = parsed_dir(base_dir, facility_id, case_id)
    chart = read_json(out_dir / "chart_fields.json")
    ocr = read_json(out_dir / "ocr_fields.json")
    sched = _read_json_any(out_dir / "scheduler_fields.json") or {}
    edoc = _read_json_any(out_dir / "edoc_meta.json") or {}
    summary = read_json(case_dir / "payments" / "summary.json")

    prov: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []

    # Chart wins for overlapping clinical keys; record OCR conflicts
    for k, spec in chart.items():
        if isinstance(spec, dict) and "value" in spec:
            prov[k] = spec
    for k, spec in ocr.items():
        if not isinstance(spec, dict) or "value" not in spec:
            continue
        if k in prov and prov[k].get("value") and spec.get("value"):
            if str(prov[k]["value"]).strip() != str(spec["value"]).strip():
                conflicts.append(
                    {
                        "field": k,
                        "chart": prov[k],
                        "ocr": spec,
                        "chosen": "patientChart",
                    }
                )
            # keep chart; store ocr under ocr_ prefix if not already
            if not k.startswith("edoc_ocr_"):
                prov[f"ocr_alt_{k}"] = spec
        else:
            prov[k] = spec

    flat = flatten_provenance(prov)
    appts = _appointments_from_scheduler(sched if isinstance(sched, dict) else {})
    flat.update(
        {
            "facility_id": str(facility_id),
            "facility_name": _lookup_facility_name(facility_id),
            "case_id": str(case_id),
            "patient_id": str(patient_id or ""),
            "patient_name": patient_name or "",
            "enrich_source": "staged_raw",
            "scheduler_event_count": sched.get("scheduler_event_count", 0)
            if isinstance(sched, dict)
            else 0,
            "scheduler_keys": sched.get("scheduler_keys", "")
            if isinstance(sched, dict)
            else "",
            "edoc_meta_count": edoc.get("edoc_meta_count", 0)
            if isinstance(edoc, dict)
            else 0,
            "edoc_meta_keys": edoc.get("edoc_meta_keys", "")
            if isinstance(edoc, dict)
            else "",
            "payments_total_charge": summary.get("Charges", ""),
            "payments_total_paid": summary.get("Payments", ""),
            "payments_balance": summary.get("Balance", ""),
            "payments_txn_count": summary.get("txn_count", ""),
            "raw_coverage_pct": raw_coverage(raw_dir(base_dir, facility_id, case_id)).get(
                "coverage_pct", 0
            ),
            **appts,
        }
    )

    # Clean UI widget junk from chart parse
    if "return_to_dr" in flat:
        flat["return_to_dr"] = _clean_return_to_dr(flat.get("return_to_dr"))

    # insurance → ins_name (daily sheet uses both)
    insurance = str(flat.get("insurance") or "").strip()
    ins_name = str(flat.get("ins_name") or "").strip()
    if not ins_name and insurance:
        flat["ins_name"] = insurance
    elif not insurance and ins_name:
        flat["insurance"] = ins_name

    # meta.json fallbacks
    mf = meta_path(base_dir, facility_id, case_id)
    meta: dict[str, Any] = {}
    if mf.is_file():
        try:
            loaded = json.loads(mf.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except json.JSONDecodeError:
            meta = {}
    flat["patient_id"] = flat["patient_id"] or str(meta.get("patient_id") or "")
    flat["patient_name"] = (
        str(flat.get("patient_name") or "").strip()
        or str(meta.get("patient_name") or "").strip()
        or _first_scheduler_name(sched if isinstance(sched, dict) else {})
    )
    if not flat.get("facility_name"):
        flat["facility_name"] = str(
            meta.get("facility_name") or meta.get("clinic_name") or ""
        ).strip() or _lookup_facility_name(facility_id)
    if not str(flat.get("dob") or "").strip():
        dob_meta = str(meta.get("dob") or "").strip()
        if dob_meta:
            flat["dob"] = dob_meta
        else:
            for ev in (sched.get("events") if isinstance(sched, dict) else None) or []:
                if isinstance(ev, dict) and str(ev.get("dob") or "").strip():
                    flat["dob"] = str(ev.get("dob")).strip()
                    break

    # Manifest counters
    manifest = case_dir / "manifests" / "artifacts_manifest.csv"
    if manifest.is_file():
        with manifest.open(encoding="utf-8-sig", newline="") as f:
            rows_m = list(csv.DictReader(f))
        flat["edoc_files_downloaded"] = sum(
            1 for r in rows_m if (r.get("doc_source") or "") == "edoc"
        )
        flat["chart_notes_downloaded"] = sum(
            1
            for r in rows_m
            if (r.get("doc_source") or "") in {"chart_note", "daily_note", "evaluation"}
        )

    if conflicts:
        write_json(out_dir / "merge_conflicts.json", conflicts)

    enrich_path = case_dir / "manifests" / "case_enrich.json"
    enrich_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        k: ("" if v is None else v)
        for k, v in flat.items()
        if not isinstance(v, (dict, list))
    }
    write_json(enrich_path, serializable)
    write_json(out_dir / "merged_fields.json", {"fields": prov, "flat": serializable})
    update_audit(case_dir, flag="merge_complete", value=True)
    write_case_sources(case_dir)
    flat["_enrich_path"] = str(enrich_path)
    flat["_conflicts"] = len(conflicts)
    return flat


def run_stage(
    stage: str,
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    patient_id: str = "",
    patient_name: str = "",
    run_ocr: bool = True,
) -> dict[str, Any]:
    stage = (stage or "all").lower()
    results: dict[str, Any] = {}
    if stage in {"parse_html", "all"}:
        results["parse_html"] = stage_parse_html(
            base_dir, facility_id=facility_id, case_id=case_id
        )
    if stage in {"parse_json", "all"}:
        results["parse_json"] = stage_parse_json(
            base_dir, facility_id=facility_id, case_id=case_id
        )
    if stage in {"payments", "all"}:
        results["payments"] = stage_payments_offline(
            base_dir, facility_id=facility_id, case_id=case_id
        )
    if stage in {"ocr", "all"} and run_ocr:
        results["ocr"] = stage_ocr(
            base_dir,
            facility_id=facility_id,
            case_id=case_id,
            patient_name=patient_name,
            patient_id=patient_id,
        )
    if stage in {"merge", "all"}:
        results["merge"] = stage_merge(
            base_dir,
            facility_id=facility_id,
            case_id=case_id,
            patient_id=patient_id,
            patient_name=patient_name,
        )
    if stage in {"export", "all"}:
        # export is batch-level; mark case ready
        case_dir = case_root(base_dir, facility_id, case_id)
        update_audit(case_dir, flag="export_complete", value=True)
        results["export"] = {"marked": True}
    return results


def enrich_case_from_raw(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    patient_id: str | int = "",
    patient_name: str = "",
    run_ocr: bool = True,
    ocr_dpi: int = 200,
) -> dict[str, Any]:
    """Back-compat: run all offline stages and return merged flat row."""
    _ = ocr_dpi
    run_stage(
        "all",
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        patient_id=str(patient_id or ""),
        patient_name=patient_name,
        run_ocr=run_ocr,
    )
    enrich_path = (
        case_root(base_dir, facility_id, case_id) / "manifests" / "case_enrich.json"
    )
    data = read_json(enrich_path)
    data["_enrich_path"] = str(enrich_path)
    return data
