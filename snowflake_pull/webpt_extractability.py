"""Phase 1 — Extractability Matrix from inventory + parser knowledge."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowflake_pull.webpt_inventory import load_inventory


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


MATRIX_ROWS: list[dict[str, str]] = [
    {
        "need": "Diagnosis",
        "in_webpt": "yes",
        "easy": "patientChart label + PDF ICD",
        "hard": "",
        "impossible": "",
    },
    {
        "need": "Chart demographics (DOB, phone, address, physician/NPI)",
        "in_webpt": "yes",
        "easy": "patientChart labels (now parsed)",
        "hard": "",
        "impossible": "",
    },
    {
        "need": "Insurance benefits (copay/deductible/limit/referral)",
        "in_webpt": "yes",
        "easy": "Additional Info block on patientChart",
        "hard": "",
        "impossible": "",
    },
    {
        "need": "Eligibility / benefitsstatus",
        "in_webpt": "observed (/patient/chart/benefitsstatus)",
        "easy": "",
        "hard": "response schema not yet captured in raw/",
        "impossible": "",
    },
    {
        "need": "Authorization visits remaining",
        "in_webpt": "yes",
        "easy": "Auth/Ins Visits on chart",
        "hard": "",
        "impossible": "",
    },
    {
        "need": "Payments ledger",
        "in_webpt": "yes",
        "easy": "var transactions + totals",
        "hard": "aging not always in response",
        "impossible": "",
    },
    {
        "need": "Scheduler extras (copay, apt_type, length, room…)",
        "in_webpt": "partial",
        "easy": "keys present on events when WebPT sends them",
        "hard": "many keys empty / clinic-dependent",
        "impossible": "",
    },
    {
        "need": "eDoc list metadata",
        "in_webpt": "yes",
        "easy": "getdocumentspercase JSON (case-scoped)",
        "hard": "",
        "impossible": "",
    },
    {
        "need": "Cross-case eDocs (getalldocuments)",
        "in_webpt": "yes",
        "easy": "",
        "hard": "",
        "impossible": "golden_rule_forbidden for case drain",
    },
    {
        "need": "Fax outbound",
        "in_webpt": "observed (/patient/outbounddocument/faxoutbound)",
        "easy": "",
        "hard": "page-side noise during chart; schema TBD in probe_extra",
        "impossible": "must stay case-scoped; never patient-wide dump",
    },
    {
        "need": "Flowsheet",
        "in_webpt": "unknown until probe sees it",
        "easy": "",
        "hard": "not in drain http log yet",
        "impossible": "",
    },
    {
        "need": "PDF clinical content (Goals/ROM/Pain/NPI…)",
        "in_webpt": "yes (files)",
        "easy": "native text extract when present",
        "hard": "OCR quality / scanned docs",
        "impossible": "",
    },
    {
        "need": "GraphQL payloads",
        "in_webpt": "yes",
        "easy": "",
        "hard": "operation-dependent; store under raw/graphql/",
        "impossible": "",
    },
]


def build_extractability_matrix(inventory_path: Path | None = None) -> dict[str, Any]:
    inv_eps: list[str] = []
    if inventory_path and Path(inventory_path).is_file():
        inv = load_inventory(inventory_path)
        inv_eps = [str(e.get("endpoint") or "") for e in inv.get("endpoints") or []]
    rows = []
    for row in MATRIX_ROWS:
        annotated = dict(row)
        # light annotation from inventory presence
        need_l = row["need"].lower()
        if "flowsheet" in need_l:
            annotated["in_webpt"] = (
                "yes" if any("flowsheet" in e for e in inv_eps) else row["in_webpt"]
            )
        if "fax" in need_l:
            annotated["in_webpt"] = (
                "yes" if any("fax" in e for e in inv_eps) else row["in_webpt"]
            )
        if "eligibility" in need_l:
            annotated["in_webpt"] = (
                "yes" if any("benefitsstatus" in e for e in inv_eps) else row["in_webpt"]
            )
        rows.append(annotated)
    return {
        "generated_at": _utc(),
        "inventory_endpoints": len(inv_eps),
        "rows": rows,
    }


def matrix_to_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# Extractability Matrix",
        "",
        f"Generated: `{matrix.get('generated_at', '')}`",
        f"Inventory endpoints: {matrix.get('inventory_endpoints', 0)}",
        "",
        "| Need | In WebPT? | Easy | Hard | Impossible / Forbidden |",
        "|------|-----------|------|------|------------------------|",
    ]
    for r in matrix.get("rows") or []:
        lines.append(
            f"| {r['need']} | {r['in_webpt']} | {r['easy'] or '—'} | "
            f"{r['hard'] or '—'} | {r['impossible'] or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_extractability_matrix(matrix: dict[str, Any], reports_dir: Path) -> Path:
    import json

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / "extractability_matrix.md"
    js = reports_dir / "extractability_matrix.json"
    md.write_text(matrix_to_markdown(matrix), encoding="utf-8")
    js.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    return md
