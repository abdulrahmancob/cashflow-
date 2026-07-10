"""Enrich denials list CSV with per-denial remit line detail from Review pages."""

import argparse
import asyncio
import csv
from pathlib import Path

from playwright.async_api import async_playwright

from auth import create_context, ensure_authenticated, extend_session, save_storage_state
from config import WaystarConfig
from denial_details import fetch_denial_review
from denials_nav import navigate_to_denials_workcenter
from human import HumanSettings, human_pause, maybe_idle_action, should_extend_session
from logging_config import get_logger, setup_logging

log = get_logger("enrich_denials")

LINE_COLUMNS = [
    "denial_id",
    "overview_active_remit_count",
    "remit_id",
    "remit_number",
    "remit_instance_id",
    "remit_active",
    "remit_received_date",
    "remit_payer",
    "line_num",
    "proc_code",
    "modifiers",
    "remark_codes",
    "remark_descriptions",
    "billed_amount",
    "allowed_amount",
    "paid_amount",
    "carc_codes",
    "carc_descriptions",
    "adjustment_amount",
    "adjustment_status",
    "resolution_action",
    "resolution_date",
    "resolution_rule",
    "resolution_payer",
    "detail_fetch_error",
    "scraped_at",
]


def load_input_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "denial_id" not in fieldnames:
        raise ValueError(f"Input CSV has no denial_id column: {path}")
    return fieldnames, rows


def load_done_denial_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {row.get("denial_id", "") for row in csv.DictReader(handle) if row.get("denial_id")}


async def enrich(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(
        input_path.stem.replace("_merged", "") + "_lines.csv"
        if "_merged" in input_path.stem
        else input_path.stem + "_lines.csv"
    )

    list_fields, list_rows = load_input_rows(input_path)
    done = load_done_denial_ids(output_path)
    if done:
        log.info("Resuming: %s denial(s) already in %s", len(done), output_path.name)

    pending = [r for r in list_rows if r.get("denial_id", "") not in done]
    if args.limit:
        pending = pending[: args.limit]
    log.info("Input: %s rows | pending: %s", len(list_rows), len(pending))

    out_fields = list(dict.fromkeys([*list_fields, *LINE_COLUMNS]))
    write_header = not output_path.exists()
    out_handle = output_path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_handle, fieldnames=out_fields, extrasaction="ignore")
    if write_header:
        writer.writeheader()

    if not pending:
        out_handle.close()
        log.info("Nothing to fetch — output at %s", output_path)
        return

    config = WaystarConfig.from_env(headless=not args.headed)
    human = HumanSettings(
        action_delay_min=args.human_delay_min if args.human_delay_min is not None else config.action_delay_min,
        action_delay_max=args.human_delay_max if args.human_delay_max is not None else config.action_delay_max,
    )

    fetched = 0
    errors = 0
    async with async_playwright() as playwright:
        browser, context = await create_context(
            playwright, config, reuse_session=not args.fresh_login
        )
        try:
            page = await ensure_authenticated(context, config, human)
            await navigate_to_denials_workcenter(page, human, config=config)

            for index, row in enumerate(pending, start=1):
                denial_id = row["denial_id"]
                if index > 1 and args.delay > 0:
                    await human_pause(human, args.delay)
                if should_extend_session(human, base_every=10):
                    await extend_session(page)

                try:
                    details = await fetch_denial_review(page, denial_id, human=human)
                except Exception as exc:
                    details = {
                        "denial_id": denial_id,
                        "error": str(exc).split("\n", maxsplit=1)[0],
                        "lines": [],
                        "line_count": 0,
                    }

                if details.get("error"):
                    errors += 1
                    log.warning("[%s/%s] denial %s: %s", index, len(pending), denial_id, details["error"])
                    if "session expired" in (details.get("error") or "").lower():
                        log.error("Session expired mid-run — stopping. Re-run to resume.")
                        break
                else:
                    fetched += 1
                    log.info("[%s/%s] denial %s: %s line(s)", index, len(pending), denial_id, details.get("line_count", 0))

                summary = {**row, "detail_fetch_error": details.get("error", "")}
                for line in details.get("lines", []):
                    writer.writerow({**summary, **line, "detail_fetch_error": details.get("error", "")})
                if not details.get("lines"):
                    writer.writerow(summary)
                out_handle.flush()
                await maybe_idle_action(page, human)

            await save_storage_state(context)
        finally:
            out_handle.close()
            await context.close()
            await browser.close()

    log.info("Done: %s fetched OK, %s errors → %s", fetched, errors, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Add remit line detail to denials CSV")
    parser.add_argument("input", help="Path to denials_merged.csv")
    parser.add_argument("--output", default=None, help="Output long-format CSV path")
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh-login", action="store_true", help="Ignore saved session and force new login")
    parser.add_argument("--human-delay-min", type=float, default=None)
    parser.add_argument("--human-delay-max", type=float, default=None)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()
    setup_logging(level=args.log_level)
    asyncio.run(enrich(args))


if __name__ == "__main__":
    main()
