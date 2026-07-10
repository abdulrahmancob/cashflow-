"""Scrape denial summaries from Waystar Denials Workcenter."""

import argparse
import asyncio
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from auth import create_context, ensure_authenticated, extend_session, save_storage_state
from config import (
    DEFAULT_BATCH_CLAIMS,
    DEFAULT_DENIAL_DATE_FROM_2026,
    DEFAULT_DENIAL_DATE_TO_2026,
    DENIALS_PER_PAGE,
    OUTPUT_DIR,
    WaystarConfig,
)
from denials_list import parse_workcenter_grid
from denials_nav import (
    fetch_workcenter_page,
    parse_workflow_stages,
    resolve_workgroup_id,
    setup_full_search,
    navigate_to_denials_workcenter,
)
from human import HumanSettings, human_pause, human_scroll, maybe_idle_action, should_extend_session
from logging_config import get_logger, setup_logging

log = get_logger("scrape_denials")

MANIFEST_FILENAME = "batch_manifest.json"


@dataclass
class DenialBatchWriter:
    export_dir: Path
    run_id: str
    batch_limit: int
    export_format: str = "both"
    batch_number: int = 0
    checkpoint_number: int = 0
    buffer: list[dict] = field(default_factory=list)
    batch_start_page: int = 1
    total_exported: int = 0
    manifest: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest:
            self.manifest = {
                "run_id": self.run_id,
                "batches": [],
                "checkpoints": [],
                "total_denials": 0,
            }
        self.manifest.setdefault("checkpoints", [])

    @classmethod
    def load(cls, export_dir: Path, run_id: str, batch_limit: int, export_format: str) -> "DenialBatchWriter":
        manifest_path = export_dir / MANIFEST_FILENAME
        manifest = {"run_id": run_id, "batches": [], "checkpoints": [], "total_denials": 0}
        batch_number = 0
        checkpoint_number = 0
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("checkpoints", [])
            batch_number = len(manifest.get("batches", []))
            checkpoint_number = len(manifest.get("checkpoints", []))
        return cls(
            export_dir=export_dir,
            run_id=run_id,
            batch_limit=batch_limit,
            export_format=export_format,
            batch_number=batch_number,
            checkpoint_number=checkpoint_number,
            manifest=manifest,
        )

    def add_rows(self, rows: list[dict]) -> None:
        self.buffer.extend(rows)

    def _write_chunk(
        self,
        rows: list[dict],
        *,
        label_prefix: str,
        end_page: int,
    ) -> int:
        if label_prefix == "batch":
            self.batch_number += 1
            counter = self.batch_number
            manifest_key = "batches"
        else:
            self.checkpoint_number += 1
            counter = self.checkpoint_number
            manifest_key = "checkpoints"

        export_label = f"{label_prefix}_{counter:03d}"
        batch_meta = {
            "export_label": export_label,
            "start_page": self.batch_start_page,
            "end_page": end_page,
            "denials_count": len(rows),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }

        if self.export_format in {"csv", "both"}:
            csv_path = self.export_dir / f"{export_label}.csv"
            _export_csv(rows, csv_path)
            batch_meta["csv"] = csv_path.name

        if self.export_format in {"json", "both"}:
            json_path = self.export_dir / f"{export_label}.json"
            _export_json(
                {
                    "run_id": self.run_id,
                    "export_type": label_prefix,
                    "export_number": counter,
                    "start_page": self.batch_start_page,
                    "end_page": end_page,
                    "denials_count": len(rows),
                    "exported_at": batch_meta["exported_at"],
                    "denials": rows,
                },
                json_path,
            )
            batch_meta["json"] = json_path.name

        self.manifest[manifest_key].append(batch_meta)
        self.total_exported += len(rows)
        self.manifest["total_denials"] = self.total_exported
        (self.export_dir / MANIFEST_FILENAME).write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(
            "%s %03d written: %s denials (pages %s–%s)",
            label_prefix,
            counter,
            len(rows),
            self.batch_start_page,
            end_page,
        )
        self.batch_start_page = end_page + 1
        return counter

    def maybe_flush_batch(self, end_page: int) -> None:
        if len(self.buffer) >= self.batch_limit:
            rows = self.buffer[: self.batch_limit]
            self._write_chunk(rows, label_prefix="batch", end_page=end_page)
            self.buffer = self.buffer[self.batch_limit :]

    def maybe_flush_checkpoint(self, end_page: int, checkpoint_pages: int, pages_fetched: int) -> None:
        if checkpoint_pages and pages_fetched % checkpoint_pages == 0 and self.buffer:
            self._write_chunk(list(self.buffer), label_prefix="checkpoint", end_page=end_page)

    def flush_remaining(self, end_page: int) -> None:
        if self.buffer:
            self._write_chunk(list(self.buffer), label_prefix="batch", end_page=end_page)
            self.buffer = []


def _export_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _export_json(payload: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


async def scrape_denials(args: argparse.Namespace) -> dict:
    config = WaystarConfig.from_env(headless=not args.headed)
    human = HumanSettings(
        action_delay_min=args.human_delay_min if args.human_delay_min is not None else config.action_delay_min,
        action_delay_max=args.human_delay_max if args.human_delay_max is not None else config.action_delay_max,
    )

    run_id = args.run_id or datetime.now(timezone.utc).strftime("denials_%Y%m%d_%H%M%S")
    export_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR / f"denials_{run_id}"
    page_size = args.page_size or DENIALS_PER_PAGE

    batch_writer: DenialBatchWriter | None = None
    if args.batch_denials:
        batch_writer = DenialBatchWriter.load(export_dir, run_id, args.batch_denials, args.format)

    all_rows: list[dict] = []
    seen_denial_ids: set[str] = set()

    async with async_playwright() as playwright:
        browser, context = await create_context(
            playwright, config, reuse_session=not args.fresh_login
        )
        try:
            page = await ensure_authenticated(context, config, human)
            await navigate_to_denials_workcenter(page, human, config=config)
            await human_pause(human)
            await human_scroll(page, human)

            workgroup_id = resolve_workgroup_id(args.workgroup)
            workflow_stages = parse_workflow_stages(args.workflow_stages)
            grid_html, count_info = await setup_full_search(
                page,
                workgroup_id=workgroup_id,
                denial_date_from=args.denial_date_from,
                denial_date_to=args.denial_date_to,
                workflow_stages=workflow_stages,
                reminder_due_only=args.reminder_due_only,
                page_size=page_size,
                clear_first=args.clear_filters,
            )

            record_count = int(count_info.get("RecordCount", 0))
            page_count = int(count_info.get("PageCount", 1))
            total_pages = page_count or max(1, math.ceil(record_count / page_size))
            if args.max_pages:
                total_pages = min(total_pages, args.max_pages)

            log.info(
                "Denials grid: %s records, %s pages (page_size=%s, start_page=%s)",
                record_count,
                total_pages,
                page_size,
                args.start_page,
            )

            for page_index in range(args.start_page, total_pages + 1):
                if page_index > args.start_page:
                    grid_html = await fetch_workcenter_page(
                        page,
                        page_index=page_index,
                        page_size=page_size,
                    )
                rows = parse_workcenter_grid(grid_html)
                new_rows = []
                for row in rows:
                    denial_id = row.get("denial_id", "")
                    if denial_id and denial_id not in seen_denial_ids:
                        seen_denial_ids.add(denial_id)
                        new_rows.append(row)
                all_rows.extend(new_rows)

                if batch_writer:
                    batch_writer.add_rows(new_rows)
                    batch_writer.maybe_flush_batch(page_index)
                    batch_writer.maybe_flush_checkpoint(
                        page_index,
                        args.checkpoint_pages or 0,
                        page_index - args.start_page + 1,
                    )

                if should_extend_session(human, base_every=10):
                    await extend_session(page)

                log.info("Page %s/%s: %s denials (%s new)", page_index, total_pages, len(rows), len(new_rows))
                await maybe_idle_action(page, human)
                if args.page_delay > 0:
                    await human_pause(human, args.page_delay)

            if batch_writer:
                batch_writer.flush_remaining(total_pages)
                from merge_exports import merge_exports

                merge_summary = merge_exports(export_dir)
                log.info(
                    "Merged %s unique denials from %s batch/checkpoint file(s) → %s",
                    merge_summary["unique_rows"],
                    merge_summary["files_merged"],
                    merge_summary["output_csv"],
                )

            if not batch_writer and all_rows:
                export_dir.mkdir(parents=True, exist_ok=True)
                _export_csv(all_rows, export_dir / "denials_merged.csv")
                _export_json(
                    {"run_id": run_id, "denials_count": len(all_rows), "denials": all_rows},
                    export_dir / "denials_merged.json",
                )

            await save_storage_state(context)
        finally:
            await context.close()
            await browser.close()

    summary = {
        "run_id": run_id,
        "export_dir": str(export_dir),
        "unique_denials": len(seen_denial_ids),
        "record_count": record_count,
        "total_pages": total_pages,
    }
    log.info("Scrape complete: %s unique denials → %s", summary["unique_denials"], export_dir)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Waystar Denials Workcenter list")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=DENIALS_PER_PAGE)
    parser.add_argument("--page-delay", type=float, default=2.0)
    parser.add_argument("--human-delay-min", type=float, default=None)
    parser.add_argument("--human-delay-max", type=float, default=None)
    parser.add_argument("--fresh-login", action="store_true", help="Ignore saved session and force new login")
    parser.add_argument(
        "--workgroup",
        default="catch-all",
        help="Workgroup: catch-all (default), current, or numeric ID",
    )
    parser.add_argument(
        "--denial-date-from",
        default=DEFAULT_DENIAL_DATE_FROM_2026,
        help="Denial date range start (default: 1/1/2026)",
    )
    parser.add_argument(
        "--denial-date-to",
        default=DEFAULT_DENIAL_DATE_TO_2026,
        help="Denial date range end (default: 12/31/2026)",
    )
    parser.add_argument(
        "--workflow-stages",
        default="all",
        help="Workflow stages: all (default) or comma-separated IDs like 1,8",
    )
    parser.add_argument(
        "--reminder-due-only",
        action="store_true",
        help="Filter to reminder due or past due only",
    )
    parser.add_argument("--batch-denials", type=int, default=None)
    parser.add_argument("--checkpoint-pages", type=int, default=None)
    parser.add_argument("--format", choices=["csv", "json", "both"], default="both")
    parser.add_argument(
        "--no-clear-filters",
        dest="clear_filters",
        action="store_false",
        help="Skip ClearFilters before applying search args (default: clear first)",
    )
    parser.set_defaults(clear_filters=True)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()
    setup_logging(level=args.log_level)
    asyncio.run(scrape_denials(args))


if __name__ == "__main__":
    main()
