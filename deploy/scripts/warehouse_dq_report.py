#!/usr/bin/env python3
"""Required-field warehouse health report (server-side)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


CHECKS = [
    ("patient_null_webpt_id", "SELECT count(*) FROM core.patient WHERE webpt_patient_id IS NULL OR btrim(webpt_patient_id)=''"),
    ("patient_null_name_key", "SELECT count(*) FROM core.patient WHERE name_key IS NULL OR btrim(name_key)=''"),
    ("visit_null_service_date", "SELECT count(*) FROM core.visit WHERE service_date IS NULL"),
    ("visit_null_patient", "SELECT count(*) FROM core.visit WHERE patient_id IS NULL"),
    ("sched_null_case", "SELECT count(*) FROM core.schedule_appointment WHERE case_pk IS NULL"),
    ("sched_null_dos", "SELECT count(*) FROM core.schedule_appointment WHERE service_date IS NULL"),
    ("sched_null_facility", "SELECT count(*) FROM core.schedule_appointment WHERE facility_id IS NULL"),
    ("payment_null_amount", "SELECT count(*) FROM billing.patient_payment WHERE amount_paid IS NULL AND amount_due IS NULL"),
    ("payment_null_patient", "SELECT count(*) FROM billing.patient_payment WHERE patient_id IS NULL"),
    ("counts_patient", "SELECT count(*) FROM core.patient"),
    ("counts_visit", "SELECT count(*) FROM core.visit"),
    ("counts_schedule", "SELECT count(*) FROM core.schedule_appointment"),
    ("counts_payment", "SELECT count(*) FROM billing.patient_payment"),
    ("counts_eob_check", "SELECT count(*) FROM billing.eob_check"),
    ("counts_claim", "SELECT count(*) FROM billing.claim"),
    ("counts_document", "SELECT count(*) FROM docs.document"),
]


def scalar(sql: str) -> int:
    r = subprocess.run(
        [
            "sudo",
            "-n",
            "docker",
            "exec",
            "cashflow-postgres-1",
            "psql",
            "-U",
            "cashflow",
            "-d",
            "cashflow",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "psql failed").strip())
    return int((r.stdout or "0").strip() or "0")


def orphan_pdf_sample() -> dict:
    script = r"""
import json
from pathlib import Path
cases = Path('/data/exports/side_by_side_case/cases')
pdf_dirs = 0
checked = 0
if cases.is_dir():
    for p in cases.iterdir():
        if not p.is_dir():
            continue
        checked += 1
        if checked > 5000:
            break
        if any(p.rglob('*.pdf')):
            pdf_dirs += 1
print(json.dumps({'checked_dirs': checked, 'dirs_with_pdf': pdf_dirs}))
"""
    r = subprocess.run(["python3", "-c", script], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()}
    try:
        return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"raw": r.stdout}


def main() -> int:
    out: dict = {}
    for name, sql in CHECKS:
        try:
            out[name] = scalar(sql)
        except Exception as exc:  # noqa: BLE001
            out[name] = -1
            out[f"{name}_error"] = str(exc)

    out["orphan_pdf_sample"] = orphan_pdf_sample()

    critical = [
        "patient_null_webpt_id",
        "visit_null_service_date",
        "visit_null_patient",
        "sched_null_case",
        "sched_null_dos",
    ]
    failures = {
        k: out[k]
        for k in critical
        if isinstance(out.get(k), int) and out[k] > 0
    }
    if out.get("counts_patient", 0) <= 0 or out.get("counts_schedule", 0) <= 0:
        failures["empty_core"] = 1

    report = {
        "ok": len(failures) == 0,
        "failures": failures,
        "metrics": out,
        "note": "Optional clinical NULLs expected; failures = required business keys only.",
    }
    candidates = [
        Path("/data/logs/warehouse_dq.json"),
        Path("/tmp/warehouse_dq.json"),
        Path("/data/exports/side_by_side_case/reports/warehouse_dq.json"),
    ]
    report_path = None
    payload = json.dumps(report, indent=2) + "\n"
    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(payload, encoding="utf-8")
            report_path = candidate
            break
        except OSError:
            continue
    print(payload, end="")
    if report_path:
        print(f"wrote {report_path}", file=sys.stderr)
    else:
        print("warn: could not persist report file", file=sys.stderr)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
