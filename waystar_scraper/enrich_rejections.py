"""Enrich an exported claims CSV with per-claim rejection details from the editor page.

For every rejected claim in the input sheet, fetches the claim editor page and adds:
  - rejection_count             number of rejection messages
  - rejection_messages          all messages, separated by " || "
  - rejection_fix_urls          How-to-Fix knowledge-base links (aligned with messages)
  - rejection_fix_slugs         readable article slugs for the links
  - rejection_original_message  raw payer message with X12 A3:xx codes
  - rejection_fetch_error       error label if the fetch/parse failed

Supports resume: rows already present in the output file are skipped on re-run.

Usage:
    python enrich_rejections.py output/claims_rejected_X/claims_rejected_merged.csv
"""

import argparse
import asyncio
import csv
from pathlib import Path

from playwright.async_api import async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    extend_session,
    save_storage_state,
)
from config import WaystarConfig
from human import HumanSettings
from logging_config import get_logger, setup_logging
from rejection_details import MESSAGE_SEPARATOR, fetch_rejection_details

log = get_logger("enrich")

EXTEND_SESSION_EVERY = 10

NEW_COLUMNS = [
    "rejection_count",
    "rejection_messages",
    "rejection_fix_urls",
    "rejection_fix_slugs",
    "rejection_original_message",
    "rejection_fetch_error",
]


def load_input_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "claim_id" not in fieldnames:
        raise ValueError(f"Input CSV has no claim_id column: {path}")
    return fieldnames, rows


def load_done_claim_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {row.get("claim_id", "") for row in csv.DictReader(handle)}


def details_to_columns(details: dict) -> dict[str, str]:
    messages = details.get("messages", [])
    return {
        "rejection_count": str(details.get("rejection_count", 0)),
        "rejection_messages": MESSAGE_SEPARATOR.join(m["message"] for m in messages),
        "rejection_fix_urls": MESSAGE_SEPARATOR.join(m["fix_url"] or "-" for m in messages),
        "rejection_fix_slugs": MESSAGE_SEPARATOR.join(m["fix_slug"] or "-" for m in messages),
        "rejection_original_message": (details.get("original_message") or "").replace("\n", " | "),
        "rejection_fetch_error": details.get("error") or "",
    }


async def enrich(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_name(
        input_path.stem + "_enriched.csv"
    )

    fieldnames, rows = load_input_rows(input_path)
    out_fields = fieldnames + [c for c in NEW_COLUMNS if c not in fieldnames]

    done = load_done_claim_ids(output_path)
    if done:
        log.info("Resuming: %s claim(s) already in %s", len(done), output_path.name)

    pending = [r for r in rows if r.get("claim_id", "") not in done]
    rejected = [r for r in pending if (r.get("status") or "").lower().startswith("rejected")]
    passthrough = [r for r in pending if r not in rejected]
    if args.limit:
        rejected = rejected[: args.limit]
    log.info(
        "Input: %s rows | pending: %s (%s rejected to fetch, %s pass-through)",
        len(rows), len(pending), len(rejected), len(passthrough),
    )

    write_header = not output_path.exists()
    out_handle = output_path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_handle, fieldnames=out_fields, extrasaction="ignore")
    if write_header:
        writer.writeheader()

    empty_cols = {c: "" for c in NEW_COLUMNS}
    for row in passthrough:
        writer.writerow({**row, **empty_cols})
    out_handle.flush()

    if not rejected:
        out_handle.close()
        log.info("Nothing to fetch — output at %s", output_path)
        return

    config = WaystarConfig.from_env(headless=not args.headed)
    human = HumanSettings(
        action_delay_min=config.action_delay_min,
        action_delay_max=config.action_delay_max,
    )

    fetched = 0
    errors = 0
    async with async_playwright() as playwright:
        browser, context = await create_context(playwright, config, reuse_session=True)
        try:
            page = await ensure_authenticated(context, config, human)

            for index, row in enumerate(rejected, start=1):
                claim_id = row["claim_id"]
                if index > 1 and args.delay > 0:
                    await asyncio.sleep(args.delay)
                if index % EXTEND_SESSION_EVERY == 0:
                    await extend_session(page)

                try:
                    details = await fetch_rejection_details(page, claim_id)
                except Exception as exc:  # keep going on per-claim failures
                    details = {"error": str(exc).split("\n", maxsplit=1)[0],
                               "messages": [], "rejection_count": 0,
                               "original_message": "", "found_grid": False}

                if details.get("error"):
                    errors += 1
                    log.warning("[%s/%s] claim %s: %s",
                                index, len(rejected), claim_id, details["error"])
                    if "session expired" in (details.get("error") or ""):
                        log.error("Session expired mid-run — stopping. Re-run to resume.")
                        break
                else:
                    fetched += 1
                    log.info("[%s/%s] claim %s: %s message(s)",
                             index, len(rejected), claim_id,
                             details.get("rejection_count", 0))

                writer.writerow({**row, **details_to_columns(details)})
                out_handle.flush()

            await save_storage_state(context)
        finally:
            out_handle.close()
            await context.close()
            await browser.close()

    log.info("Done: %s fetched OK, %s errors → %s", fetched, errors, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add rejection reason + How-to-Fix columns to an exported claims CSV"
    )
    parser.add_argument("input", help="Path to exported claims CSV (e.g. claims_rejected_merged.csv)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: <input>_enriched.csv)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between editor fetches (default: 1.5)")
    parser.add_argument("--limit", type=int, default=None, help="Only fetch first N rejected claims (for testing)")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()

    setup_logging(level=args.log_level)
    asyncio.run(enrich(args))


if __name__ == "__main__":
    main()
