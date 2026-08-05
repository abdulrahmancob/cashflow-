"""Aggregate case_enrich.json → lean case_export_{batch}.csv + completeness gate."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from snowflake_pull.facility_map import WEBPT_FACILITIES
except Exception:  # pragma: no cover
    try:
        from facility_map import WEBPT_FACILITIES  # type: ignore
    except Exception:  # pragma: no cover
        WEBPT_FACILITIES = {}


# Daily review sheet — identity / clinical / ops only.
# Provenance (*_source/*_confidence) and OCR stay in case_enrich.json / parsed/.
CASE_EXPORT_FIELDNAMES: list[str] = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "dob",
    "case_id",
    "insurance",
    "ins_name",
    "physician",
    "physician_npi",
    "assigned_therapist",
    "diagnosis",
    "auth_ins_visits",
    "visits_in_case",
    "cancel_no_show",
    "copay",
    "deductible",
    "limit_per_year",
    "referral_required",
    "address",
    "phone",
    "appointments_past_count",
    "appointments_past_dates",
    "appointments_upcoming_count",
    "appointments_upcoming_dates",
    "payments_txn_count",
    "payments_total_charge",
    "payments_total_paid",
    "payments_balance",
    "edoc_meta_count",
    "chart_notes_downloaded",
    "edoc_files_downloaded",
    "raw_coverage_pct",
    "enrich_source",
]

# Separate OCR review sheet (daily lean sheet stays without edoc_ocr_*).
_OCR_FIELD_KEYS: list[str] = [
    "edoc_ocr_name",
    "edoc_ocr_name_match",
    "edoc_ocr_patient_id",
    "edoc_ocr_id_match",
    "edoc_ocr_diagnosis",
    "edoc_ocr_diagnosis_match",
    "edoc_ocr_source_files",
    "edoc_ocr_file_hints",
    "edoc_ocr_errors",
    "edoc_ocr_physician",
    "edoc_ocr_npi",
    "edoc_ocr_dob",
    "edoc_ocr_insurance",
    "edoc_ocr_frequency",
    "edoc_ocr_visits",
    "edoc_ocr_poc_date",
    "edoc_ocr_certification",
    "edoc_ocr_goals",
    "edoc_ocr_precautions",
    "edoc_ocr_rom",
    "edoc_ocr_pain",
    "edoc_ocr_signature",
    "edoc_ocr_icd_codes",
]

OCR_EXPORT_FIELDNAMES: list[str] = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "case_id",
    *_OCR_FIELD_KEYS,
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def case_export_fieldnames() -> list[str]:
    return list(CASE_EXPORT_FIELDNAMES)


def normalize_export_row(row: dict[str, Any]) -> dict[str, str]:
    """Project to lean schema and apply last-mile fills for daily sheet."""
    out: dict[str, str] = {}
    for k in CASE_EXPORT_FIELDNAMES:
        v = row.get(k, "")
        out[k] = "" if v is None else str(v)

    if not out.get("ins_name") and out.get("insurance"):
        out["ins_name"] = out["insurance"]

    if not out.get("facility_name"):
        fid = out.get("facility_id") or ""
        name = WEBPT_FACILITIES.get(str(fid), "") if WEBPT_FACILITIES else ""
        if name:
            out["facility_name"] = str(name)

    return out


def iter_enrich_rows(cases_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(Path(cases_dir).glob("*/*/manifests/case_enrich.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append(normalize_export_row(data))
    return rows


def write_case_export_csv(
    rows: list[dict[str, str]],
    out_path: Path,
    *,
    fieldnames: list[str] | None = None,
) -> Path:
    cols = fieldnames or case_export_fieldnames()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            norm = normalize_export_row(r)
            w.writerow({k: norm.get(k, "") for k in cols})
    return out_path


def normalize_ocr_export_row(row: dict[str, Any]) -> dict[str, str]:
    """Project identity + edoc_ocr_* fields for the OCR sheet."""
    out: dict[str, str] = {}
    for k in OCR_EXPORT_FIELDNAMES:
        v = row.get(k, "")
        # Flatten provenance dicts from ocr_fields.json if present
        if isinstance(v, dict) and "value" in v:
            v = v.get("value", "")
        out[k] = "" if v is None else str(v)

    if not out.get("facility_name"):
        fid = out.get("facility_id") or ""
        name = WEBPT_FACILITIES.get(str(fid), "") if WEBPT_FACILITIES else ""
        if name:
            out["facility_name"] = str(name)
    return out


def write_ocr_export_csv(
    rows: list[dict[str, Any]],
    out_path: Path,
    *,
    fieldnames: list[str] | None = None,
) -> Path:
    cols = list(fieldnames or OCR_EXPORT_FIELDNAMES)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            norm = normalize_ocr_export_row(r)
            w.writerow({k: norm.get(k, "") for k in cols})
    return out_path


def iter_ocr_export_rows(cases_dir: Path) -> list[dict[str, str]]:
    """Rows from case_enrich.json that have any OCR field filled (or ocr audit)."""
    rows: list[dict[str, str]] = []
    for path in sorted(Path(cases_dir).glob("*/*/manifests/case_enrich.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        has_ocr = any(str(data.get(k) or "").strip() for k in _OCR_FIELD_KEYS)
        if not has_ocr:
            # Still include if ocr_fields.json exists with values
            ocr_path = path.parents[1] / "parsed" / "ocr_fields.json"
            if ocr_path.is_file():
                try:
                    ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ocr = {}
                if isinstance(ocr, dict):
                    for k in _OCR_FIELD_KEYS:
                        if k not in data or not data.get(k):
                            spec = ocr.get(k)
                            if isinstance(spec, dict):
                                data[k] = spec.get("value", "")
                            elif spec is not None:
                                data[k] = spec
                    has_ocr = any(
                        str(data.get(k) or "").strip() for k in _OCR_FIELD_KEYS
                    )
        if has_ocr:
            rows.append(normalize_ocr_export_row(data))
    return rows


def evaluate_promote_gate(
    *,
    store_summary: dict[str, Any],
    coverage_report: dict[str, Any] | None,
    raw_stats: dict[str, Any] | None,
    audit_stats: dict[str, Any] | None = None,
    min_parse_coverage_pct: float = 40.0,
    min_raw_coverage_pct: float = 50.0,
    min_audit_download_pct: float = 90.0,
    require_empty_queue: bool = True,
) -> dict[str, Any]:
    """Soft/hard gate before promote — Coverage % + raw + audit thresholds."""
    queued = int(store_summary.get("queued", 0) or 0)
    retrying = int(store_summary.get("retry_1", 0) or 0) + int(
        store_summary.get("retry_2", 0) or 0
    ) + int(store_summary.get("retry_3", 0) or 0)
    parse_cov = float((coverage_report or {}).get("coverage_pct") or 0.0)
    raw_cov = float((raw_stats or {}).get("mean_raw_coverage_pct") or 0.0)
    raw_sample_n = int((raw_stats or {}).get("cases_with_raw") or 0)
    audit_dl = float((audit_stats or {}).get("download_complete_pct") or 0.0)
    audit_snap = float((audit_stats or {}).get("raw_snapshot_complete_pct") or 0.0)

    checks = {
        "queue_empty": queued == 0 and retrying == 0,
        "parse_coverage_ok": parse_cov >= min_parse_coverage_pct,
        "raw_coverage_ok": raw_cov >= min_raw_coverage_pct or raw_sample_n == 0,
        "raw_sample_present": raw_sample_n > 0,
        "audit_download_ok": audit_dl >= min_audit_download_pct or audit_stats is None,
        "audit_snapshot_ok": audit_snap >= 50.0 or audit_stats is None,
    }
    hard_ok = checks["queue_empty"] if require_empty_queue else True
    hard_ok = hard_ok and checks["parse_coverage_ok"]
    soft_ok = (
        checks["raw_sample_present"]
        and checks["raw_coverage_ok"]
        and checks["audit_download_ok"]
    )

    return {
        "generated_at": _utc(),
        "promote_allowed": bool(hard_ok and soft_ok),
        "hard_ok": hard_ok,
        "soft_ok": soft_ok,
        "checks": checks,
        "thresholds": {
            "min_parse_coverage_pct": min_parse_coverage_pct,
            "min_raw_coverage_pct": min_raw_coverage_pct,
            "min_audit_download_pct": min_audit_download_pct,
            "require_empty_queue": require_empty_queue,
        },
        "metrics": {
            "queued": queued,
            "retrying": retrying,
            "parse_coverage_pct": parse_cov,
            "mean_raw_coverage_pct": raw_cov,
            "cases_with_raw": raw_sample_n,
            "download_complete_pct": audit_dl,
            "raw_snapshot_complete_pct": audit_snap,
        },
    }


def write_promote_gate(gate: dict[str, Any], reports_dir: Path) -> Path:
    path = Path(reports_dir) / "promote_gate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gate, indent=2), encoding="utf-8")
    return path
