"""CLI: python -m cashflow_ops run|resume|status|backfill|metrics|events|..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from cashflow_ops.config import (
    DEFAULT_LOOKBACK_DAYS,
    DRY_RUN,
    SKIP_SCRAPERS,
    cairo_today,
)
from cashflow_ops.engine import resume_run, run_status, start_run


def _parse_date(value: str | None) -> date:
    if not value:
        return cairo_today()
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cashflow_ops",
        description="RCM Processing Platform - workflow engine",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Start a daily run or date-range backfill")
    p_run.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: Cairo today)")
    p_run.add_argument("--from", dest="from_date", default=None, help="Backfill start YYYY-MM-DD")
    p_run.add_argument("--to", dest="to_date", default=None, help="Backfill end YYYY-MM-DD")
    p_run.add_argument("--continue-on-fail", action="store_true")
    p_run.add_argument("--trigger", default="manual")
    p_run.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--skip-scrapers", action="store_true")
    p_run.add_argument("--notes", default=None)

    p_resume = sub.add_parser("resume", help="Resume an existing pipeline run_id")
    p_resume.add_argument("--run-id", required=True)
    p_resume.add_argument("--dry-run", action="store_true")
    p_resume.add_argument("--skip-scrapers", action="store_true")

    p_bf = sub.add_parser("backfill", help="Backfill control")
    bf_sub = p_bf.add_subparsers(dest="bf_cmd", required=True)
    p_bf_resume = bf_sub.add_parser("resume", help="Resume backfill from first non-success day")
    p_bf_resume.add_argument("--backfill-id", required=True)
    p_bf_resume.add_argument("--continue-on-fail", action="store_true")
    p_bf_resume.add_argument("--dry-run", action="store_true")
    p_bf_resume.add_argument("--skip-scrapers", action="store_true")
    p_bf_resume.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p_bf_status = bf_sub.add_parser("status", help="Show backfill_run + days")
    p_bf_status.add_argument("--backfill-id", required=True)

    p_status = sub.add_parser("status", help="Show run status JSON")
    p_status.add_argument("--run-id", default=None)
    p_status.add_argument("--as-of", default=None)

    p_metrics = sub.add_parser("metrics", help="List monitoring metrics for a run")
    p_metrics.add_argument("--run-id", required=True)

    p_events = sub.add_parser("events", help="List pipeline events for a run")
    p_events.add_argument("--run-id", required=True)
    p_events.add_argument("--limit", type=int, default=500)

    p_retry = sub.add_parser("retry-drain", help="List / mark due retry queue items")
    p_retry.add_argument("--limit", type=int, default=50)
    p_retry.add_argument("--mark-running", action="store_true")

    p_snap = sub.add_parser("snapshot", help="Show daily snapshot for a date")
    p_snap.add_argument("--as-of", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dry = True if getattr(args, "dry_run", False) else (DRY_RUN or None)
    skip = True if getattr(args, "skip_scrapers", False) else (SKIP_SCRAPERS or None)

    if args.cmd == "run":
        if args.from_date or args.to_date:
            if not args.from_date or not args.to_date:
                parser.error("--from and --to are required together")
            from cashflow_ops.backfill import start_date_range_backfill

            result = start_date_range_backfill(
                from_date=_parse_date(args.from_date),
                to_date=_parse_date(args.to_date),
                continue_on_fail=args.continue_on_fail,
                dry_run=dry,
                skip_scrapers=skip,
                lookback_days=args.lookback_days,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("status") in {"success", "partial"} else 2

        run_id = start_run(
            as_of_date=_parse_date(args.as_of),
            trigger_source=args.trigger,
            lookback_days=args.lookback_days,
            dry_run=dry,
            skip_scrapers=skip,
            notes=args.notes,
        )
        print(json.dumps(run_status(run_id), indent=2, default=str))
        status = run_status(run_id)["run"]["status"]
        return 0 if status in {"success", "partial"} else 2

    if args.cmd == "resume":
        run_id = resume_run(args.run_id, dry_run=dry, skip_scrapers=skip)
        print(json.dumps(run_status(run_id), indent=2, default=str))
        status = run_status(run_id)["run"]["status"]
        return 0 if status in {"success", "partial"} else 2

    if args.cmd == "backfill":
        from cashflow_ops import backfill

        if args.bf_cmd == "resume":
            result = backfill.run_backfill(
                args.backfill_id,
                continue_on_fail=args.continue_on_fail,
                dry_run=dry,
                skip_scrapers=skip,
                lookback_days=args.lookback_days,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("status") in {"success", "partial"} else 2
        if args.bf_cmd == "status":
            bf = backfill.get_backfill(args.backfill_id)
            days = backfill.list_backfill_days(args.backfill_id)
            print(json.dumps({"backfill": bf, "days": days}, indent=2, default=str))
            return 0 if bf else 1

    if args.cmd == "status":
        from cashflow_ops import state

        if args.run_id:
            print(json.dumps(run_status(args.run_id), indent=2, default=str))
            return 0
        row = state.latest_pipeline_run(_parse_date(args.as_of) if args.as_of else None)
        if not row:
            print(json.dumps({"error": "no_runs"}, indent=2))
            return 1
        print(json.dumps(run_status(str(row["run_id"])), indent=2, default=str))
        return 0

    if args.cmd == "metrics":
        from cashflow_ops import metrics

        print(
            json.dumps(
                {
                    "metrics": metrics.list_metrics(args.run_id),
                    "runtimes": metrics.list_runtimes(args.run_id),
                },
                indent=2,
                default=str,
            )
        )
        return 0

    if args.cmd == "events":
        from cashflow_ops import events

        print(
            json.dumps(
                {"events": events.list_events(args.run_id, limit=args.limit)},
                indent=2,
                default=str,
            )
        )
        return 0

    if args.cmd == "retry-drain":
        from cashflow_ops import state

        items = state.due_retry_items(limit=args.limit)
        if args.mark_running:
            for item in items:
                state.update_retry_item(
                    item["retry_id"], status="running", bump_attempt=True
                )
        print(json.dumps({"count": len(items), "items": items}, indent=2, default=str))
        return 0

    if args.cmd == "snapshot":
        from cashflow_ops import state

        row = state.get_daily_snapshot(_parse_date(args.as_of))
        if not row:
            print(json.dumps({"error": "not_found"}, indent=2))
            return 1
        print(json.dumps(row, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
