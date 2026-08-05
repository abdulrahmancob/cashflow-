"""Build one master capability report: Field | Status | Why."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case" / "reports"

# Human-facing catalog — capability truth, not just sample enrich hits.
# status: yes | partial | no | forbidden
ROWS: list[dict[str, str]] = [
    # Chart
    {"field": "Diagnosis", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Auth / Ins Visits", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Visits in Case", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Assigned Therapist", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Cancel / No Show", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Copay", "status": "yes", "why": "patientChart (Additional Info)", "domain": "Chart"},
    {"field": "Deductible", "status": "yes", "why": "patientChart (Additional Info)", "domain": "Chart"},
    {"field": "Limit / Year", "status": "yes", "why": "patientChart (Additional Info)", "domain": "Chart"},
    {"field": "Referral Required", "status": "yes", "why": "patientChart (Additional Info)", "domain": "Chart"},
    {"field": "Physician + NPI", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "DOB / Age", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Insurance name / type", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Address / Phone", "status": "yes", "why": "patientChart", "domain": "Chart"},
    {"field": "Return to Dr", "status": "partial", "why": "patientChart label often UI widget, not clean value", "domain": "Chart"},
    # Payments
    {"field": "Transactions (full ledger)", "status": "yes", "why": "patient/transaction/chart (var transactions)", "domain": "Payments"},
    {"field": "Total Charge / Paid / Balance", "status": "yes", "why": "patient/transaction/chart", "domain": "Payments"},
    {"field": "Payment method / check auth", "status": "yes", "why": "patient/transaction/chart", "domain": "Payments"},
    {"field": "Adjustment / write-off class", "status": "partial", "why": "only if type/description says so — no dedicated aging API", "domain": "Payments"},
    {"field": "Aging buckets", "status": "no", "why": "Not present in payments response sampled", "domain": "Payments"},
    # Scheduler
    {"field": "Appointments (dates/status/checkin/out)", "status": "yes", "why": "scheduler/index/data/T/e", "domain": "Scheduler"},
    {"field": "Scheduler copay / auth_visits / apt_type", "status": "partial", "why": "keys parsed when WebPT sends them (often empty)", "domain": "Scheduler"},
    {"field": "Room / resource / color / recurring", "status": "partial", "why": "keys known; clinic-dependent presence", "domain": "Scheduler"},
    # Insurance extras
    {"field": "Eligibility (benefitsstatus)", "status": "partial", "why": "endpoint seen in inventory; response not yet in raw/", "domain": "Insurance"},
    {"field": "Member ID", "status": "no", "why": "Not present in any case-scoped endpoint we capture", "domain": "Insurance"},
    {"field": "Group number", "status": "no", "why": "Not present in patientChart / payments samples", "domain": "Insurance"},
    {"field": "Policy Holder", "status": "no", "why": "WebPT never returns it in observed surfaces", "domain": "Insurance"},
    {"field": "Primary / Secondary insurance split", "status": "partial", "why": "insurance + insurance_type on chart; full eligibility TBD", "domain": "Insurance"},
    # Documents
    {"field": "eDoc list (case-scoped)", "status": "yes", "why": "edoc/getdocumentspercase", "domain": "Documents"},
    {"field": "eDoc metadata (DateFiled/Category/Signed…)", "status": "yes", "why": "edoc_list.json keys (raw capture)", "domain": "Documents"},
    {"field": "getAllDocuments (cross-case)", "status": "forbidden", "why": "Forbidden by design (Golden Rule)", "domain": "Documents"},
    {"field": "Fax History (patient-wide)", "status": "forbidden", "why": "Golden Rule forbidden — case-scoped only if ever used", "domain": "Documents"},
    {"field": "Fax outbound endpoint noise", "status": "partial", "why": "seen in http log during chart; not parsed as field store", "domain": "Documents"},
    # PDF / OCR
    {"field": "Goals", "status": "yes", "why": "OCR / PDF text (edoc_ocr + POC parsers)", "domain": "PDF/OCR"},
    {"field": "ICD codes from PDF", "status": "yes", "why": "OCR / daily-note extract", "domain": "PDF/OCR"},
    {"field": "Physician / NPI from PDF", "status": "yes", "why": "OCR extended fields", "domain": "PDF/OCR"},
    {"field": "Frequency / POC date / Certification", "status": "yes", "why": "OCR / Plan of Care parsers", "domain": "PDF/OCR"},
    {"field": "ROM / Pain / Precautions / Signature", "status": "partial", "why": "OCR patterns exist; only when text is readable", "domain": "PDF/OCR"},
    {"field": "CPT / billing lines", "status": "yes", "why": "daily note PDF extract (case_extract)", "domain": "PDF/OCR"},
    # Other surfaces
    {"field": "GraphQL payloads", "status": "partial", "why": "observed; store under raw/graphql/ — not fully field-mapped", "domain": "Other"},
    {"field": "Flowsheet", "status": "no", "why": "Not seen in live drain inventory yet", "domain": "Other"},
    {"field": "ClinicActions menu", "status": "no", "why": "Endpoint exists; not a patient data source we parse", "domain": "Other"},
]


def _icon(status: str) -> str:
    return {
        "yes": "✅",
        "partial": "⚠️",
        "no": "❌",
        "forbidden": "🚫",
    }.get(status, "?")


def _label(status: str) -> str:
    return {
        "yes": "Can get",
        "partial": "Partial",
        "no": "Cannot get",
        "forbidden": "Forbidden",
    }.get(status, status)


def build() -> tuple[dict, str]:
    inv_path = REPORTS / "webpt_inventory.json"
    cov_path = REPORTS / "webpt_coverage_report.json"
    gate_path = REPORTS / "promote_gate.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8")) if inv_path.is_file() else {}
    cov = json.loads(cov_path.read_text(encoding="utf-8")) if cov_path.is_file() else {}
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.is_file() else {}

    counts = {"yes": 0, "partial": 0, "no": 0, "forbidden": 0}
    for r in ROWS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    generated = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_at": generated,
        "summary": {
            "can_get": counts["yes"],
            "partial": counts["partial"],
            "cannot_get": counts["no"],
            "forbidden": counts["forbidden"],
            "total": len(ROWS),
            "inventory_endpoints": inv.get("endpoint_count"),
            "sample_parse_coverage_pct": cov.get("coverage_pct"),
            "promote_allowed": gate.get("promote_allowed"),
            "cases_remaining_note": (gate.get("metrics") or {}).get("queued"),
        },
        "fields": ROWS,
        "endpoints_seen": [
            {
                "method": e.get("method"),
                "endpoint": e.get("endpoint"),
                "hits": e.get("hit_count"),
                "parsed_by": e.get("currently_parsed_by"),
                "forbidden": e.get("golden_rule_forbidden"),
            }
            for e in (inv.get("endpoints") or [])
        ],
    }

    lines = [
        "# WebPT Master Capability Report",
        "",
        f"Generated: `{generated}`",
        "",
        "## الخلاصة",
        "",
        f"- ✅ نقدر نجيبها: **{counts['yes']}**",
        f"- ⚠️ جزئي / مش دايم: **{counts['partial']}**",
        f"- ❌ مش موجودة في WebPT (حسب الرصد): **{counts['no']}**",
        f"- 🚫 ممنوعة (Golden Rule): **{counts['forbidden']}**",
        f"- Endpoints مرصودة في الجلسة: **{inv.get('endpoint_count', '—')}**",
        f"- Sample parse coverage: **{cov.get('coverage_pct', '—')}%**",
        f"- Promote allowed: **{gate.get('promote_allowed', '—')}**",
        "",
        "> Download (PDFs) ≠ Enrich. الـ drain يحفظ الملفات؛ الـ enrich يقرأ من `raw/` + PDFs بدون رجوع لـ 25k case.",
        "",
        "## Field × Status × Why",
        "",
        "| Field | Status | Why |",
        "|-------|--------|-----|",
    ]
    for r in ROWS:
        lines.append(
            f"| {r['field']} | {_icon(r['status'])} {_label(r['status'])} | {r['why']} |"
        )

    # Grouped sections
    lines.extend(["", "## حسب المصدر", ""])
    by_domain: dict[str, list[dict[str, str]]] = {}
    for r in ROWS:
        by_domain.setdefault(r["domain"], []).append(r)
    for domain, items in by_domain.items():
        lines.append(f"### {domain}")
        lines.append("")
        lines.append("| Field | Status | Why |")
        lines.append("|-------|--------|-----|")
        for r in items:
            lines.append(
                f"| {r['field']} | {_icon(r['status'])} | {r['why']} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Endpoints المرصودة (من الجلسة الحية)",
            "",
            "| Method | Endpoint | Hits | Parser | Forbidden |",
            "|--------|----------|------|--------|-----------|",
        ]
    )
    for e in payload["endpoints_seen"]:
        lines.append(
            "| {m} | `{ep}` | {h} | {p} | {f} |".format(
                m=e.get("method") or "",
                ep=e.get("endpoint") or "",
                h=e.get("hits") or 0,
                p=e.get("parsed_by") or "—",
                f="yes" if e.get("forbidden") else "",
            )
        )

    lines.extend(
        [
            "",
            "## قواعد ثابتة",
            "",
            "- جلسة WebPT واحدة فقط",
            "- S1 verify + case-scoped eDocs فقط",
            "- لا `getalldocuments` / لا patient-wide fax dump",
            "- أي حقل جديد بعد كده = parser على `raw/` و PDFs، مش إعادة scrape",
            "",
            "## ملفات مرتبطة",
            "",
            "- `webpt_inventory.json` — سطح الشبكة",
            "- `webpt_coverage_report.md` — قياس extracted vs missing على عيّنة",
            "- `extractability_matrix.md` — سهل/صعب/مستحيل",
            "- `unknown_fields_report.md` — فجوة HTML/JSON vs parser",
            "- `case_export_*.csv` — ناتج الـ enrich المجمّع",
            "",
        ]
    )
    return payload, "\n".join(lines)


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    payload, md = build()
    jp = REPORTS / "webpt_master_capability_report.json"
    mp = REPORTS / "webpt_master_capability_report.md"
    jp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    mp.write_text(md, encoding="utf-8")
    print(mp)
    s = payload["summary"]
    print(
        f"can={s['can_get']} partial={s['partial']} "
        f"no={s['cannot_get']} forbidden={s['forbidden']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
