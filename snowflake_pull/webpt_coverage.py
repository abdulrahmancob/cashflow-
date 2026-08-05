"""Coverage Report: inventory fields → parser → extracted → missing → reason."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowflake_pull.webpt_inventory import load_inventory

# Field catalog used for coverage when inventory fields_discovered is sparse.
CANONICAL_FIELDS: list[dict[str, Any]] = [
    # chart
    {"domain": "chart", "field": "diagnosis", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "auth_ins_visits", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "visits_in_case", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "assigned_therapist", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "cancel_no_show", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "deductible", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "copay", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "limit_per_year", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "referral_required", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "physician", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "physician_npi", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "dob", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "insurance", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 1},
    {"domain": "chart", "field": "insurance_type", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 2},
    {"domain": "chart", "field": "address", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 2},
    {"domain": "chart", "field": "phone", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 2},
    # payments
    {"domain": "payments", "field": "transactions", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 1},
    {"domain": "payments", "field": "total_charge", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 1},
    {"domain": "payments", "field": "total_paid", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 1},
    {"domain": "payments", "field": "balance", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 1},
    {"domain": "payments", "field": "paid_method", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 2},
    {"domain": "payments", "field": "auth_check", "endpoint": "/patient/transaction/chart", "parser": "patient_payments_api", "priority": 2},
    {"domain": "payments", "field": "adjustment", "endpoint": "/patient/transaction/chart", "parser": None, "priority": 3},
    {"domain": "payments", "field": "aging", "endpoint": "/patient/transaction/chart", "parser": None, "priority": 3},
    # scheduler
    {"domain": "scheduler", "field": "start_date", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 1},
    {"domain": "scheduler", "field": "checkin_time", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 1},
    {"domain": "scheduler", "field": "checkout_time", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 1},
    {"domain": "scheduler", "field": "copay", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 2},
    {"domain": "scheduler", "field": "auth_visits", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 2},
    {"domain": "scheduler", "field": "apt_type", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 2},
    {"domain": "scheduler", "field": "length", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 2},
    {"domain": "scheduler", "field": "provider", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 2},
    {"domain": "scheduler", "field": "room", "endpoint": "/scheduler/index/data/t/e", "parser": "scheduler_api", "priority": 3},
    # insurance extras
    {"domain": "insurance", "field": "eligibility", "endpoint": "/patient/chart/benefitsstatus", "parser": None, "priority": 2},
    {"domain": "insurance", "field": "authorization", "endpoint": "/patientchart.php", "parser": "patient_chart_api", "priority": 2},
    {"domain": "insurance", "field": "member_id", "endpoint": "/patientchart.php", "parser": None, "priority": 2},
    {"domain": "insurance", "field": "group_number", "endpoint": "/patientchart.php", "parser": None, "priority": 3},
    {"domain": "insurance", "field": "policy_holder", "endpoint": "/patientchart.php", "parser": None, "priority": 3},
    # docs
    {"domain": "documents", "field": "ExtDocID", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "edoc_api", "priority": 1},
    {"domain": "documents", "field": "URI", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "edoc_api", "priority": 1},
    {"domain": "documents", "field": "UserDefName", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "edoc_api", "priority": 1},
    {"domain": "documents", "field": "DateFiled", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "case_enrich_parse", "priority": 1},
    {"domain": "documents", "field": "Category", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "case_enrich_parse", "priority": 2},
    {"domain": "documents", "field": "Signed", "endpoint": "/edoc/edoc/getdocumentspercase", "parser": "case_enrich_parse", "priority": 2},
    {"domain": "documents", "field": "getalldocuments", "endpoint": "/edoc/edoc/getalldocuments", "parser": None, "priority": 9, "forbidden": True},
    # OCR / PDF
    {"domain": "pdf_ocr", "field": "edoc_ocr_name", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 1},
    {"domain": "pdf_ocr", "field": "edoc_ocr_diagnosis", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 1},
    {"domain": "pdf_ocr", "field": "edoc_ocr_physician", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 2},
    {"domain": "pdf_ocr", "field": "edoc_ocr_npi", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 2},
    {"domain": "pdf_ocr", "field": "edoc_ocr_goals", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 2},
    {"domain": "pdf_ocr", "field": "edoc_ocr_frequency", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 2},
    {"domain": "pdf_ocr", "field": "edoc_ocr_rom", "endpoint": "pdf", "parser": "edoc_ocr", "priority": 3},
    # fax — observed but case-scoped use only
    {"domain": "fax", "field": "faxoutbound", "endpoint": "/patient/outbounddocument/faxoutbound", "parser": None, "priority": 3},
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field_extracted(sample_row: dict[str, Any] | None, field: str) -> bool:
    if not sample_row:
        return False
    # Document metadata keys are listed in edoc_meta_keys when present on JSON
    if field in {"DateFiled", "Category", "Signed", "ExtDocID", "URI", "UserDefName"}:
        keys_blob = str(sample_row.get("edoc_meta_keys") or "")
        if field in keys_blob:
            return True
        try:
            return int(sample_row.get("edoc_meta_count") or 0) > 0 and field in {
                "ExtDocID",
                "URI",
                "UserDefName",
            }
        except (TypeError, ValueError):
            return False
    # Scheduler extras appear in scheduler_keys blob and/or event count
    if field in {
        "start_date",
        "checkin_time",
        "checkout_time",
        "auth_visits",
        "apt_type",
        "length",
        "provider",
        "room",
    }:
        try:
            if int(sample_row.get("scheduler_event_count") or 0) > 0:
                keys_blob = str(sample_row.get("scheduler_keys") or "")
                if field in {"start_date", "checkin_time", "checkout_time"}:
                    return True
                return field in keys_blob
        except (TypeError, ValueError):
            pass
    # map inventory-ish names to export keys
    aliases = {
        "transactions": "payments_txn_count",
        "total_charge": "payments_total_charge",
        "total_paid": "payments_total_paid",
        "balance": "payments_balance",
        "authorization": "auth_ins_visits",
        "copay": "copay",
        "paid_method": "payments_txn_count",
        "auth_check": "payments_txn_count",
    }
    key = aliases.get(field, field)
    val = sample_row.get(key)
    if val is None:
        val = sample_row.get(field)
    if val is None or val == "" or val == 0 or val == "0" or val == 0.0:
        return False
    return True


def build_coverage_report(
    *,
    inventory_path: Path | None = None,
    sample_enrich_rows: list[dict[str, Any]] | None = None,
    raw_sample_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inventory = load_inventory(inventory_path) if inventory_path and inventory_path.is_file() else {}
    endpoints = {
        (str(e.get("method", "")).upper(), str(e.get("endpoint", "")).lower()): e
        for e in (inventory.get("endpoints") or [])
    }
    sample = sample_enrich_rows[0] if sample_enrich_rows else None
    rows: list[dict[str, Any]] = []

    for spec in CANONICAL_FIELDS:
        field = spec["field"]
        endpoint = str(spec["endpoint"]).lower()
        parser = spec.get("parser")
        forbidden = bool(spec.get("forbidden"))
        ep_hit = any(ep == endpoint for (_, ep) in endpoints) or endpoint in {"pdf"}
        extracted = False
        reason = ""
        if forbidden:
            reason = "golden_rule_forbidden"
        elif not ep_hit and endpoint not in {"pdf"}:
            reason = "no_api"
        elif parser is None:
            reason = "not_parsed"
            # eligibility may simply not be in response
            if field in {"adjustment", "aging", "member_id", "group_number", "policy_holder", "eligibility"}:
                reason = "not_in_response" if ep_hit else "no_api"
        elif sample is not None:
            extracted = _field_extracted(sample, field)
            if not extracted:
                if spec["domain"] == "pdf_ocr":
                    reason = "needs_ocr"
                elif spec["domain"] in {"scheduler", "payments", "documents", "chart", "insurance"}:
                    # distinguish empty vs not parsed
                    reason = "not_in_response"
                else:
                    reason = "not_parsed"
        else:
            # no sample enrich yet — parser exists counts as ready but not extracted
            reason = "not_in_response"
            if parser:
                extracted = False

        # If parser exists and inventory lists the field, credit parse-ready
        status = "extracted" if extracted else "missing"
        rows.append(
            {
                "domain": spec["domain"],
                "field": field,
                "endpoint": endpoint,
                "in_inventory": ep_hit,
                "parser": parser or "",
                "status": status,
                "reason": "" if extracted else reason,
                "priority": spec.get("priority", 5),
            }
        )

    discoverable = [r for r in rows if r["reason"] != "golden_rule_forbidden"]
    extracted_n = sum(1 for r in discoverable if r["status"] == "extracted")
    missing_n = len(discoverable) - extracted_n
    coverage_pct = round(100.0 * extracted_n / len(discoverable), 1) if discoverable else 0.0
    reason_counts: dict[str, int] = {}
    for r in discoverable:
        if r["status"] == "missing":
            reason_counts[r["reason"] or "unknown"] = reason_counts.get(r["reason"] or "unknown", 0) + 1

    return {
        "generated_at": _utc(),
        "inventory_endpoints": len(inventory.get("endpoints") or []),
        "discoverable_fields": len(discoverable),
        "extracted_fields": extracted_n,
        "missing_fields": missing_n,
        "coverage_pct": coverage_pct,
        "missing_pct": round(100.0 - coverage_pct, 1),
        "missing_reasons": reason_counts,
        "raw_sample_stats": raw_sample_stats or {},
        "fields": rows,
        "chain": "WebPT → Pages → Endpoints → Fields → Parser → Extracted → Missing → Reason → Priority",
    }


def coverage_to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# WebPT Coverage Report",
        "",
        f"Generated: `{report.get('generated_at', '')}`",
        f"Coverage: **{report.get('coverage_pct')}%** "
        f"(extracted {report.get('extracted_fields')}/"
        f"{report.get('discoverable_fields')}; missing {report.get('missing_pct')}%)",
        "",
        f"Chain: `{report.get('chain')}`",
        "",
        "## Missing reasons",
        "",
    ]
    for reason, n in sorted((report.get("missing_reasons") or {}).items()):
        lines.append(f"- `{reason}`: {n}")
    lines.extend(
        [
            "",
            "| Domain | Field | Endpoint | Parser | Status | Reason | Priority |",
            "|--------|-------|----------|--------|--------|--------|----------|",
        ]
    )
    for r in report.get("fields") or []:
        lines.append(
            f"| {r['domain']} | `{r['field']}` | `{r['endpoint']}` | {r['parser'] or '—'} "
            f"| {r['status']} | {r['reason'] or '—'} | {r['priority']} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_coverage_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    jp = reports_dir / "webpt_coverage_report.json"
    mp = reports_dir / "webpt_coverage_report.md"
    jp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    mp.write_text(coverage_to_markdown(report), encoding="utf-8")
    return jp, mp
