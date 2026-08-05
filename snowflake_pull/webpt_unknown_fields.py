"""Phase 2–3: compare full HTML/JSON samples vs current parsers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def analyze_chart_html(html: str, *, scraper_path: Path) -> dict[str, Any]:
    import sys

    if str(scraper_path) not in sys.path:
        sys.path.insert(0, str(scraper_path))
    from patient_chart_api import (
        chart_info_to_export_fields,
        discover_chart_labels,
        parse_patient_chart_html,
    )

    labels = discover_chart_labels(html)
    info = parse_patient_chart_html(html)
    parsed = chart_info_to_export_fields(info)
    parsed_nonempty = {k: v for k, v in parsed.items() if v}
    # Labels mapped by parser
    mapped_labels = {
        "auth/ins visits",
        "cancel/no show",
        "visits in case",
        "assigned therapist",
        "diagnosis",
        "additional info",
        "dob",
        "age",
        "physician",
        "insurance",
        "insurance_type",
        "address",
        "phone",
        "return to dr",
    }
    missing_labels = [lab for lab in labels if lab not in mapped_labels]
    return {
        "page": "patientChart.php",
        "labels_total": len(labels),
        "labels": labels,
        "parser_fields_nonempty": len(parsed_nonempty),
        "parser_fields": parsed_nonempty,
        "missing_labels": missing_labels,
        "missing_count": len(missing_labels),
    }


def analyze_payments_html(html: str, *, scraper_path: Path) -> dict[str, Any]:
    import sys

    if str(scraper_path) not in sys.path:
        sys.path.insert(0, str(scraper_path))
    from patient_payments_api import parse_patient_payments_html

    m = re.search(r"var\s+transactions\s*=\s*(\[.*?\]);", html or "", re.DOTALL)
    keys: list[str] = []
    if m:
        try:
            arr = json.loads(m.group(1))
            if arr and isinstance(arr[0], dict):
                keys = sorted(arr[0].keys())
        except json.JSONDecodeError:
            keys = []
    txns, totals = parse_patient_payments_html(html)
    known = {
        "dateOfService",
        "dateOfTransaction",
        "type",
        "description",
        "amountDue",
        "amountPaid",
        "paidMethodType",
        "creditType",
        "checkAuthorizationNumber",
        "transactionId",
        "status",
        "caseId",
        "facilityId",
        "patientId",
        "paymentDate",
        "collectorInitials",
        "emvPaymentType",
        "paid",
        "typeId",
    }
    missing = [k for k in keys if k not in known]
    return {
        "page": "patient/transaction/chart",
        "json_keys_total": len(keys),
        "json_keys": keys,
        "txn_count": len(txns),
        "totals": totals,
        "missing_keys": missing,
        "missing_count": len(missing),
        # extras still captured on PaymentTxn.extras
        "extras_captured": True,
    }


def build_unknown_fields_report(
    *,
    chart_html_paths: list[Path],
    payments_html_paths: list[Path],
    scraper_path: Path,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for path in chart_html_paths:
        html = path.read_text(encoding="utf-8", errors="replace")
        # skip shell/search pages without classic labels
        analysis = analyze_chart_html(html, scraper_path=scraper_path)
        if analysis["labels_total"] == 0:
            continue
        analysis["sample_path"] = str(path)
        pages.append(analysis)
        break  # one rich sample enough for report body

    for path in payments_html_paths:
        html = path.read_text(encoding="utf-8", errors="replace")
        if "var transactions" not in html:
            continue
        analysis = analyze_payments_html(html, scraper_path=scraper_path)
        analysis["sample_path"] = str(path)
        pages.append(analysis)
        break

    return {
        "generated_at": _utc(),
        "pages": pages,
        "summary": {
            "pages_analyzed": len(pages),
            "total_missing": sum(int(p.get("missing_count") or 0) for p in pages),
        },
    }


def unknown_fields_to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Unknown Fields Report",
        "",
        f"Generated: `{report.get('generated_at', '')}`",
        "",
    ]
    for page in report.get("pages") or []:
        n = page.get("labels_total") or page.get("json_keys_total") or 0
        k = page.get("parser_fields_nonempty") or page.get("txn_count") or 0
        missing = page.get("missing_count", 0)
        lines.append(f"## `{page.get('page')}`")
        lines.append("")
        lines.append(f"Sample: `{page.get('sample_path', '')}`")
        lines.append("")
        lines.append(f"page contains **{n}** labels/keys; parser extracts **{k}**; missing **{missing}**")
        lines.append("")
        miss = page.get("missing_labels") or page.get("missing_keys") or []
        if miss:
            lines.append("Missing / unmapped:")
            for item in miss:
                lines.append(f"- `{item}`")
        else:
            lines.append("No unmapped keys (extras may still live on PaymentTxn.extras).")
        lines.append("")
    return "\n".join(lines)


def write_unknown_fields_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    jp = reports_dir / "unknown_fields_report.json"
    mp = reports_dir / "unknown_fields_report.md"
    jp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    mp.write_text(unknown_fields_to_markdown(report), encoding="utf-8")
    return jp, mp
