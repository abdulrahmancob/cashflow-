"""CLI: migrate schema and run ETL loaders."""

from __future__ import annotations

import argparse
import json
import sys

from cashflow_db.db import migrate
from cashflow_db.loaders import (
    load_forecast_from_csv,
    load_mail,
    load_patient_payments,
    load_revflow,
    load_rules,
    load_schedule,
    load_snowflake_kpi,
    load_tracker,
    load_waystar,
    load_webpt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cashflow_db", description="Cashflow RCM database")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="Apply SQL migrations + seed ref data")

    p_all = sub.add_parser("load-all", help="Run case-centric ETL loaders in order")
    p_all.add_argument("--limit", type=int, default=None, help="Limit rows/files for smoke loads")
    p_all.add_argument(
        "--with-forecast-csv",
        action="store_true",
        help="Also import outcome_stages.csv (escape hatch; prefer forecast --from-db write)",
    )

    p_sched = sub.add_parser("load-schedule", help="Load raw schedule → appointments + visits")
    p_sched.add_argument("--limit", type=int, default=None)

    p_webpt = sub.add_parser("load-webpt")
    p_webpt.add_argument("--limit", type=int, default=None)

    p_pay = sub.add_parser("load-patient-payments")
    p_pay.add_argument("--limit", type=int, default=None)

    p_rf = sub.add_parser("load-revflow")
    p_rf.add_argument("--limit-files", type=int, default=None)

    sub.add_parser("load-tracker")
    sub.add_parser("load-mail")
    sub.add_parser("load-rules")
    sub.add_parser("load-forecast")

    p_sf = sub.add_parser("load-snowflake-kpi")
    p_sf.add_argument("--limit", type=int, default=None)

    p_ws = sub.add_parser("load-waystar", help="Load Waystar rejections/denials")
    p_ws.add_argument("--limit", type=int, default=None)

    sub.add_parser("validate", help="Warehouse data assertions (source ↔ DB)")

    p_enrich = sub.add_parser(
        "enrich-case-extracts",
        help="Join legacy notes/CPT to schedule case_id (writes CASE_PIPELINE extracted)",
    )

    sub.add_parser(
        "bootstrap-admin",
        help="Seed portal users (admin/finance/posting) if missing (env CASHFLOW_SEED_*)",
    )
    p_import_tracker = sub.add_parser(
        "import-tracker-xlsx",
        help="Seed billing.transaction_tracker_row from Transaction Tracker xlsx",
    )
    p_import_tracker.add_argument(
        "--path",
        default=None,
        help="Override TRACKER_XLSX path",
    )
    p_elig = sub.add_parser(
        "generate-eligibility",
        help="Upsert eligibility work items from reconciliation visits",
    )
    p_elig.add_argument("--csv-dir", default=None, help="Override reconciliation CSV dir")
    p_elig.add_argument("--from-csv", action="store_true", help="Force CSV source")
    p_elig.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        applied = migrate()
        seed = None
        try:
            from cashflow_ops.security import seed_portal_users

            seed = seed_portal_users()
        except Exception as exc:  # noqa: BLE001
            seed = f"skipped: {exc}"
        print(json.dumps({"applied": applied, "seed_portal_users": seed}, indent=2, default=str))
        return 0

    if args.cmd == "bootstrap-admin":
        from cashflow_ops.security import seed_portal_users

        result = seed_portal_users()
        print(json.dumps({"seed_portal_users": result}, indent=2))
        return 0

    if args.cmd == "import-tracker-xlsx":
        from pathlib import Path

        from cashflow_db.config import TRACKER_XLSX
        from cashflow_db.loaders.tracker_xlsx import parse_tracker_workbook
        from cashflow_db.repository import connection, tracker

        path = Path(args.path) if args.path else TRACKER_XLSX
        parsed = parse_tracker_workbook(path)
        with connection() as conn:
            counts = tracker.import_parsed_rows(
                conn,
                [r.to_dict() for r in parsed.rows],
                actor_user_id=None,
            )
        print(
            json.dumps(
                {
                    "path": str(path),
                    "parsed_rows": len(parsed.rows),
                    "errors": [
                        {"sheet": e.sheet, "row": e.row, "message": e.message}
                        for e in parsed.errors[:50]
                    ],
                    "error_count": len(parsed.errors),
                    "skipped_sheets": parsed.skipped_sheets,
                    **counts,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if len(parsed.errors) == 0 or counts.get("inserted", 0) + counts.get("updated", 0) > 0 else 2

    if args.cmd == "generate-eligibility":
        from pathlib import Path

        from cashflow_db.services.eligibility_generator import generate_eligibility_work_items

        result = generate_eligibility_work_items(
            recon_dir=Path(args.csv_dir) if args.csv_dir else None,
            from_db=not args.from_csv,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 2

    if args.cmd == "validate":
        from cashflow_db.validate_warehouse import run_assertions

        report = run_assertions()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 2

    if args.cmd == "enrich-case-extracts":
        from cashflow_db.scripts.enrich_case_extracts import main as enrich_main

        return enrich_main([])

    if args.cmd == "load-schedule":
        print(json.dumps(load_schedule(limit=args.limit), indent=2))
        return 0
    if args.cmd == "load-webpt":
        print(json.dumps(load_webpt(limit=args.limit), indent=2))
        return 0
    if args.cmd == "load-patient-payments":
        print(json.dumps(load_patient_payments(limit=args.limit), indent=2))
        return 0
    if args.cmd == "load-revflow":
        print(json.dumps(load_revflow(limit_files=args.limit_files), indent=2))
        return 0
    if args.cmd == "load-tracker":
        print(json.dumps(load_tracker(), indent=2))
        return 0
    if args.cmd == "load-mail":
        print(json.dumps(load_mail(), indent=2))
        return 0
    if args.cmd == "load-rules":
        print(json.dumps(load_rules(), indent=2))
        return 0
    if args.cmd == "load-forecast":
        print(json.dumps(load_forecast_from_csv(), indent=2))
        return 0
    if args.cmd == "load-snowflake-kpi":
        print(json.dumps(load_snowflake_kpi(limit=args.limit), indent=2))
        return 0
    if args.cmd == "load-waystar":
        print(json.dumps(load_waystar(limit=args.limit), indent=2))
        return 0

    if args.cmd == "load-all":
        limit = args.limit
        results: dict = {
            "rules": load_rules(),
            "schedule": load_schedule(limit=limit),
            "webpt": load_webpt(limit=limit),
            "patient_payments": load_patient_payments(limit=limit),
            "revflow": load_revflow(limit_files=limit),
            "tracker": load_tracker(),
        }
        # Optional for reconcile: missing mail soft-skips inside load_mail
        results["mail"] = load_mail()
        # Optional staging loaders — do not fail the whole warehouse for probe/env gaps
        for name, fn in (
            ("snowflake_kpi", lambda: load_snowflake_kpi(limit=limit)),
            ("waystar", lambda: load_waystar(limit=limit)),
        ):
            try:
                results[name] = fn()
            except Exception as exc:
                results[name] = {"skipped": True, "error": str(exc)[:500]}
        if args.with_forecast_csv:
            results["forecast"] = load_forecast_from_csv()
        print(json.dumps(results, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
