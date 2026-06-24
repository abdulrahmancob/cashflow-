"""Periodic Waystar claims scraper — run via Task Scheduler or cron."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_INTERVAL_HOURS = 6


def run_once(extra_args: list[str]) -> int:
    scraper_path = Path(__file__).resolve().parent / "scraper.py"
    command = [sys.executable, str(scraper_path), *extra_args]
    print(f"Running: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, check=False)
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Waystar scraper on a fixed interval"
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=DEFAULT_INTERVAL_HOURS,
        help=f"Hours between runs (default: {DEFAULT_INTERVAL_HOURS})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scrape and exit",
    )
    parser.add_argument(
        "--transaction-days",
        type=int,
        default=30,
        help="Transaction date window passed to scraper.py",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page limit passed to scraper.py",
    )
    args, extra = parser.parse_known_args()

    scraper_args = [
        "--transaction-days",
        str(args.transaction_days),
        "--format",
        "both",
    ]
    if args.max_pages is not None:
        scraper_args.extend(["--max-pages", str(args.max_pages)])
    scraper_args.extend(extra)

    if args.once:
        raise SystemExit(run_once(scraper_args))

    interval_sec = max(args.interval_hours, 0.1) * 3600
    print(
        f"Scheduler started: every {args.interval_hours}h, "
        f"transaction-days={args.transaction_days}",
        flush=True,
    )

    while True:
        exit_code = run_once(scraper_args)
        if exit_code != 0:
            print(f"Scrape exited with code {exit_code}", file=sys.stderr, flush=True)
        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
