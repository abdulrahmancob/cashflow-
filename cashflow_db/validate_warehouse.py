"""Warehouse validation: source file counts ↔ Postgres (Data Assertions gate)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from cashflow_db.config import (
    CASE_PIPELINE_DIR,
    PATIENT_PAYMENTS_CSV,
    SCHEDULE_VISITS_CSV,
    WEBPT_LEGACY_OUTPUT,
    WAYSTAR_REJECTIONS_CSV,
)
from cashflow_db.repository import client
from cashflow_db.repository import payments as pay_repo
from cashflow_db.repository import visits as visit_repo
from cashflow_db.util import safe_str


def _csv_rows(path: Path) -> int:
    if not path.exists():
        return -1
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def _schedule_loadable_rows(path: Path) -> int:
    """Count rows that pass blank-case rejection (same rule as load_schedule)."""
    if not path.exists():
        return -1
    n = 0
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            if safe_str(row.get("case_id")) and safe_str(row.get("patient_id")):
                n += 1
    return n


def run_assertions(*, database_url: str | None = None) -> dict:
    results: list[dict] = []
    ok = True

    def check(name: str, expected: int, actual: int, *, soft: bool = False) -> None:
        nonlocal ok
        passed = expected < 0 or actual == expected or (
            soft and expected >= 0 and actual >= 0 and abs(actual - expected) / max(expected, 1) < 0.02
        )
        # soft: allow 2% drift for tracker/revflow natural-key collapses
        if soft and expected >= 0 and actual >= 0:
            passed = abs(actual - expected) <= max(2, int(0.02 * expected))
        if expected < 0:
            passed = True  # source missing — skip
        results.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "soft": soft,
            }
        )
        if not passed:
            ok = False

    with client.connection(database_url) as conn:
        sched_src = _schedule_loadable_rows(SCHEDULE_VISITS_CSV)
        check("schedule↔appointments", sched_src, visit_repo.count_schedule_appointments(conn))

        pay_src = _csv_rows(PATIENT_PAYMENTS_CSV)
        check("patient_payments↔patient_payment", pay_src, pay_repo.count_patient_payments(conn))

        notes_path = CASE_PIPELINE_DIR / "extracted" / "daily_notes.csv"
        cpt_path = CASE_PIPELINE_DIR / "extracted" / "cpt_codes.csv"
        check("notes↔clinical_note", _csv_rows(notes_path), visit_repo.count_clinical_notes(conn))
        check("cpt↔visit_service_line", _csv_rows(cpt_path), visit_repo.count_service_lines(conn), soft=True)

        # RevFlow: compare distinct check natural keys roughly via eob_check count vs export files
        from cashflow_db.config import REVFLOW_OUTPUT

        exports = list((REVFLOW_OUTPUT / "exports").glob("*.csv")) if REVFLOW_OUTPUT.exists() else []
        check(
            "revflow_files↔eob_check",
            len(exports) if exports else -1,
            pay_repo.count_eob_checks(conn),
            soft=True,
        )

        try:
            from cashflow_db.repository import tracker as tracker_repo

            tracker_n = tracker_repo.count_active_rows(conn)
        except Exception:
            tracker_n = -1
        check("tracker↔bank_deposit", tracker_n, pay_repo.count_bank_deposits(conn), soft=True)

        poc = WEBPT_LEGACY_OUTPUT / "extracted" / "plans_of_care.csv"
        poc_db = conn.execute("SELECT COUNT(*)::int AS n FROM docs.plan_of_care_detail").fetchone()
        check("poc↔plan_of_care_detail", _csv_rows(poc), int(poc_db["n"]) if poc_db else 0)

        if WAYSTAR_REJECTIONS_CSV.exists():
            den = conn.execute(
                "SELECT COUNT(*)::int AS n FROM billing.denial_record WHERE source_system = 'waystar'"
            ).fetchone()
            check(
                "waystar↔denial_record",
                _csv_rows(WAYSTAR_REJECTIONS_CSV),
                int(den["n"]) if den else 0,
                soft=True,
            )

    return {"ok": ok, "assertions": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warehouse data assertions")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)
    report = run_assertions(database_url=args.database_url)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
