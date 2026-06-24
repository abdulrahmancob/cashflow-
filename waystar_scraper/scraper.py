import argparse
import asyncio
import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    extend_session,
    get_verification_token,
    refresh_verification_token,
    resolve_cust_id,
    retry_settings_from_config,
    save_storage_state,
)
from claim_pdf import download_pdfs_for_claims
from claims_search import (
    SearchCriteria,
    build_search_form,
    criteria_from_transaction_days,
    criteria_rejected_calendar,
    perform_search,
)
from config import (
    DEFAULT_BATCH_CLAIMS,
    DEFAULT_NETWORK_RETRY_ATTEMPTS,
    DEFAULT_NETWORK_RETRY_DELAY_SEC,
    DEFAULT_PAGE_DELAY_SEC,
    DEFAULT_PDF_DELAY_SEC,
    DEFAULT_PDF_TIMEOUT_SEC,
    DEFAULT_REJECTED_TRANS_FROM,
    DEFAULT_REJECTED_TRANS_TO,
    OUTPUT_DIR,
    PDFS_DIR,
    REJECTED_STATUS_CODE,
    SCREENSHOTS_DIR,
    SESSION_EXTEND_EVERY_PAGES,
    WaystarConfig,
)
from human import HumanSettings, human_scroll
from logging_config import get_logger, mask_secret, setup_logging
from parser import parse_search_result_html

log = get_logger("scraper")

MANIFEST_FILENAME = "batch_manifest.json"


@dataclass
class PdfOptions:
    enabled: bool = False
    delay_sec: float = DEFAULT_PDF_DELAY_SEC
    timeout_sec: float = DEFAULT_PDF_TIMEOUT_SEC
    form: str = "CMS1500_0212"
    skip_existing: bool = True
    run_id: str = ""


@dataclass
class BatchWriter:
    export_dir: Path
    run_id: str
    batch_claims_limit: int
    export_format: str = "both"
    batch_number: int = 0
    checkpoint_number: int = 0
    batch_buffer: list[dict] = field(default_factory=list)
    batch_start_page: int = 1
    total_claims_exported: int = 0
    manifest: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest:
            self.manifest = {
                "run_id": self.run_id,
                "batches": [],
                "checkpoints": [],
                "total_claims": 0,
            }
        self.manifest.setdefault("checkpoints", [])

    @classmethod
    def load(cls, export_dir: Path, run_id: str, batch_claims_limit: int, export_format: str) -> "BatchWriter":
        manifest_path = export_dir / MANIFEST_FILENAME
        manifest: dict = {
            "run_id": run_id,
            "batches": [],
            "checkpoints": [],
            "total_claims": 0,
        }
        batch_number = 0
        checkpoint_number = 0

        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("checkpoints", [])
            batch_number = len(manifest.get("batches", []))
            checkpoint_number = len(manifest.get("checkpoints", []))
            log.info(
                "Resuming run %s — %s batch(es), %s checkpoint(s) exported (%s claims)",
                run_id,
                batch_number,
                checkpoint_number,
                manifest.get("total_claims", 0),
            )

        writer = cls(
            export_dir=export_dir,
            run_id=run_id,
            batch_claims_limit=batch_claims_limit,
            export_format=export_format,
            batch_number=batch_number,
            checkpoint_number=checkpoint_number,
            manifest=manifest,
        )
        writer.total_claims_exported = manifest.get("total_claims", 0)
        return writer

    def add_claims(self, claims: list[dict], *, current_page: int) -> list[int]:
        """Add claims to buffer; flush full batches. Returns batch numbers written."""
        if not claims:
            return []

        if not self.batch_buffer and not self.manifest["batches"]:
            self.batch_start_page = current_page
        elif not self.batch_buffer and self.manifest["batches"]:
            last = self.manifest["batches"][-1]
            self.batch_start_page = last.get("end_page", current_page - 1) + 1

        self.batch_buffer.extend(claims)
        written: list[int] = []

        while len(self.batch_buffer) >= self.batch_claims_limit:
            chunk = self.batch_buffer[: self.batch_claims_limit]
            self.batch_buffer = self.batch_buffer[self.batch_claims_limit :]
            batch_num = self._write_batch(chunk, end_page=current_page)
            written.append(batch_num)

        return written

    def flush_remaining(self, *, end_page: int) -> int | None:
        if not self.batch_buffer:
            return None
        return self._write_batch(self.batch_buffer, end_page=end_page)

    def flush_checkpoint(self, *, end_page: int) -> int | None:
        if not self.batch_buffer:
            return None
        return self._write_export(
            self.batch_buffer,
            end_page=end_page,
            label_prefix="checkpoint",
            manifest_key="checkpoints",
            counter_attr="checkpoint_number",
        )

    def _write_batch(self, claims: list[dict], *, end_page: int) -> int:
        return self._write_export(
            claims,
            end_page=end_page,
            label_prefix="batch",
            manifest_key="batches",
            counter_attr="batch_number",
        )

    def _write_export(
        self,
        claims: list[dict],
        *,
        end_page: int,
        label_prefix: str,
        manifest_key: str,
        counter_attr: str,
    ) -> int:
        counter = getattr(self, counter_attr) + 1
        setattr(self, counter_attr, counter)
        export_label = f"{label_prefix}_{counter:03d}"

        batch_meta = {
            "export_number": counter,
            "export_type": label_prefix,
            "start_page": self.batch_start_page,
            "end_page": end_page,
            "claims_count": len(claims),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
        if label_prefix == "batch":
            batch_meta["batch_number"] = counter

        if self.export_format in {"csv", "both"}:
            csv_path = self.export_dir / f"{export_label}.csv"
            export_csv(claims, csv_path)
            batch_meta["csv"] = csv_path.name

        if self.export_format in {"json", "both"}:
            json_path = self.export_dir / f"{export_label}.json"
            export_json(
                {
                    "run_id": self.run_id,
                    "export_type": label_prefix,
                    "export_number": counter,
                    "start_page": self.batch_start_page,
                    "end_page": end_page,
                    "claims_count": len(claims),
                    "exported_at": batch_meta["exported_at"],
                    "claims": claims,
                },
                json_path,
            )
            batch_meta["json"] = json_path.name

        self.manifest[manifest_key].append(batch_meta)
        self.total_claims_exported += len(claims)
        self.manifest["total_claims"] = self.total_claims_exported
        self._save_manifest()

        log.info(
            "%s %s written: %s claims (pages %s–%s, run total: %s)",
            label_prefix.capitalize(),
            counter,
            len(claims),
            self.batch_start_page,
            end_page,
            self.total_claims_exported,
        )
        self.batch_buffer = []
        self.batch_start_page = end_page + 1
        return counter

    def _save_manifest(self) -> None:
        path = self.export_dir / MANIFEST_FILENAME
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle, indent=2, ensure_ascii=False)
        log.debug("Updated manifest: %s", path)


def build_human_settings(
    config: WaystarConfig,
    *,
    screenshots_enabled: bool,
    screenshot_every_page: bool,
    run_id: str,
    action_delay_min: float | None = None,
    action_delay_max: float | None = None,
) -> HumanSettings:
    screenshot_dir = SCREENSHOTS_DIR / run_id if screenshots_enabled else None
    return HumanSettings(
        action_delay_min=action_delay_min if action_delay_min is not None else config.action_delay_min,
        action_delay_max=action_delay_max if action_delay_max is not None else config.action_delay_max,
        screenshots_enabled=screenshots_enabled,
        screenshot_every_page=screenshot_every_page,
        screenshot_dir=screenshot_dir,
    )


def export_csv(claims: list[dict], path: Path) -> None:
    if not claims:
        path.write_text("", encoding="utf-8")
        log.warning("No claims to write — created empty CSV at %s", path)
        return

    fieldnames = list(claims[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(claims)
    log.info("Exported %s claims to CSV: %s", len(claims), path)


def export_json(payload: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    log.info("Exported JSON: %s", path)


async def _wait_for_browser_close() -> None:
    log.info("Browser left open — press Enter in this terminal to close it...")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, input, "")


async def scrape_claims(
    config: WaystarConfig,
    human: HumanSettings,
    pdf_options: PdfOptions,
    *,
    search_criteria: SearchCriteria | None = None,
    max_pages: int | None = None,
    page_delay_sec: float = DEFAULT_PAGE_DELAY_SEC,
    reuse_session: bool = True,
    start_page: int = 1,
    batch_claims: int | None = None,
    export_dir: Path | None = None,
    export_format: str = "both",
    checkpoint_pages: int | None = None,
    keep_browser_open: bool = False,
) -> dict:
    if search_criteria is None:
        search_criteria = criteria_from_transaction_days(config.transaction_days)

    log.info("=== Scrape started ===")
    log.info(
        "Config: user=%s app_id=%s cust_id=%s search=%s headless=%s "
        "download_pdfs=%s start_page=%s batch_claims=%s checkpoint_pages=%s",
        mask_secret(config.username),
        config.app_id,
        config.cust_id or "(auto)",
        search_criteria.summary(),
        config.headless,
        pdf_options.enabled,
        start_page,
        batch_claims or "(single file)",
        checkpoint_pages or "(off)",
    )
    if human.screenshots_enabled and human.screenshot_dir:
        log.info("Screenshots directory: %s", human.screenshot_dir)
    if pdf_options.enabled:
        log.info(
            "PDF directory: %s (form=%s, delay=%ss)",
            PDFS_DIR / pdf_options.run_id,
            pdf_options.form,
            pdf_options.delay_sec,
        )
    if export_dir is not None:
        log.info("Batch export directory: %s", export_dir)
    if max_pages is not None:
        log.info("Page limit: %s", max_pages)
    if keep_browser_open:
        log.info("Keep-browser-open enabled — session saved after each batch")

    network_retry = retry_settings_from_config(config)
    log.info(
        "Network retries: %s attempts, %.0fs base delay",
        network_retry.max_attempts,
        network_retry.base_delay_sec,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = PDFS_DIR / pdf_options.run_id if pdf_options.enabled else None
    total_pdfs_ok = 0
    total_pdfs_failed = 0

    batch_writer: BatchWriter | None = None
    if batch_claims is not None and export_dir is not None:
        batch_writer = BatchWriter.load(
            export_dir,
            pdf_options.run_id,
            batch_claims,
            export_format,
        )

    all_claims: list[dict] = []
    browser = None
    context = None
    current_page = start_page

    async with async_playwright() as playwright:
        browser, context = await create_context(
            playwright, config, reuse_session=reuse_session
        )

        try:
            log.info("Step 1/5: Authenticate and open claims listing")
            page = await ensure_authenticated(context, config, human)

            log.info("Step 2/5: Prepare claims listing")
            await human.delay()
            await human_scroll(page, human)

            log.info("Step 3/5: Resolve customer ID")
            await human.delay()
            cust_id = await resolve_cust_id(page, config)

            log.info("Step 4/5: Read CSRF token")
            token = await get_verification_token(page)

            log.info("Step 5/5: Search, paginate%s", ", and download PDFs" if pdf_options.enabled else "")
            total_results = None
            total_pages = None
            pages_fetched = 0

            if start_page > 1:
                log.info("Resuming pagination from page %s", start_page)

            while True:
                await human.delay()
                form_body = build_search_form(
                    token=token,
                    cust_id=cust_id,
                    app_id=config.app_id,
                    page_number=current_page,
                    criteria=search_criteria,
                )
                html = await perform_search(
                    page,
                    token,
                    form_body,
                    page_number=current_page,
                    retry=network_retry,
                )
                parsed = parse_search_result_html(html)

                if parsed["total_results"] is not None:
                    total_results = parsed["total_results"]
                if parsed["total_pages"] is not None:
                    total_pages = parsed["total_pages"]

                page_claims = parsed["claims"]

                if pdf_options.enabled and pdf_dir is not None:
                    ok, failed = await download_pdfs_for_claims(
                        page,
                        page_claims,
                        pdf_dir,
                        human,
                        app_id=config.app_id,
                        form=pdf_options.form,
                        pdf_delay_sec=pdf_options.delay_sec,
                        skip_existing=pdf_options.skip_existing,
                        pdf_run_id=pdf_options.run_id,
                        page_number=current_page,
                        pdf_timeout_sec=pdf_options.timeout_sec,
                    )
                    total_pdfs_ok += ok
                    total_pdfs_failed += failed
                else:
                    for claim in page_claims:
                        claim.setdefault("pdf_path", "")
                        claim.setdefault("pdf_downloaded", False)
                        claim.setdefault("pdf_error", None)

                if batch_writer is not None:
                    written_batches = batch_writer.add_claims(page_claims, current_page=current_page)
                    for batch_num in written_batches:
                        await save_storage_state(context)
                        log.info("Session saved after batch %s", batch_num)

                    if checkpoint_pages and current_page % checkpoint_pages == 0:
                        checkpoint_num = batch_writer.flush_checkpoint(end_page=current_page)
                        if checkpoint_num is not None:
                            await save_storage_state(context)
                            log.info("Session saved after checkpoint %s", checkpoint_num)

                    running_total = batch_writer.total_claims_exported + len(batch_writer.batch_buffer)
                else:
                    all_claims.extend(page_claims)
                    running_total = len(all_claims)

                pages_fetched += 1

                if human.screenshots_enabled and (
                    human.screenshot_every_page or current_page == start_page
                ):
                    await human.screenshot(page, f"search_page_{current_page}")

                log.info(
                    "Page %s%s: %s claims parsed (running total: %s, server total: %s)",
                    current_page,
                    f"/{total_pages}" if total_pages else "",
                    len(page_claims),
                    running_total,
                    total_results if total_results is not None else "?",
                )

                if current_page % SESSION_EXTEND_EVERY_PAGES == 0:
                    if await extend_session(page, retry=network_retry):
                        await save_storage_state(context)

                if not page_claims:
                    log.warning("Page %s returned 0 claims — stopping pagination", current_page)
                    break

                if max_pages is not None and pages_fetched >= max_pages:
                    log.info("Reached --max-pages limit (%s)", max_pages)
                    break

                if total_pages is not None and current_page >= total_pages:
                    log.info("Reached last page (%s)", total_pages)
                    break

                current_page += 1
                if page_delay_sec > 0:
                    log.debug("Waiting %.2fs before next page", page_delay_sec)
                    await asyncio.sleep(page_delay_sec)

                token = await refresh_verification_token(page, human, retry=network_retry)

            if batch_writer is not None:
                final_batch = batch_writer.flush_remaining(end_page=current_page)
                if final_batch is not None:
                    await save_storage_state(context)
                    log.info("Session saved after final batch %s", final_batch)
                claims_count = batch_writer.total_claims_exported
            else:
                claims_count = len(all_claims)

            await save_storage_state(context)

            result = {
                "cust_id": cust_id,
                "search_criteria": {
                    "transaction_date_span": search_criteria.transaction_date_span,
                    "trans_from": search_criteria.trans_from,
                    "trans_to": search_criteria.trans_to,
                    "status": search_criteria.status,
                },
                "transaction_days": config.transaction_days,
                "run_id": pdf_options.run_id,
                "total_results": total_results,
                "total_pages": total_pages,
                "pages_fetched": pages_fetched,
                "start_page": start_page,
                "last_page": current_page,
                "claims_count": claims_count,
                "pdfs_downloaded": total_pdfs_ok if pdf_options.enabled else 0,
                "pdfs_failed": total_pdfs_failed if pdf_options.enabled else 0,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "export_dir": str(export_dir) if export_dir else None,
                "batches_written": len(batch_writer.manifest["batches"]) if batch_writer else 0,
                "checkpoints_written": len(batch_writer.manifest.get("checkpoints", [])) if batch_writer else 0,
                "claims": all_claims if batch_writer is None else [],
            }
            log.info(
                "=== Scrape finished OK: %s claims from %s page(s)%s%s%s ===",
                result["claims_count"],
                result["pages_fetched"],
                f", {total_pdfs_ok} PDFs downloaded" if pdf_options.enabled else "",
                f", {result['batches_written']} batches" if batch_writer else "",
                f", {result['checkpoints_written']} checkpoints" if batch_writer and result["checkpoints_written"] else "",
            )

            if keep_browser_open:
                await _wait_for_browser_close()

            return result
        except Exception as exc:
            log.exception("=== Scrape failed at step above: %s ===", exc)
            if batch_writer is not None and batch_writer.batch_buffer:
                try:
                    emergency = batch_writer.flush_checkpoint(end_page=current_page)
                    if emergency is not None:
                        log.info(
                            "Emergency checkpoint %s saved after failure (pages 1–%s)",
                            emergency,
                            current_page,
                        )
                        if context is not None:
                            await save_storage_state(context)
                except Exception as flush_exc:
                    log.error("Could not write emergency checkpoint: %s", flush_exc)
            if context is not None:
                try:
                    await save_storage_state(context)
                    log.info("Session saved after failure (for resume)")
                except PlaywrightError as save_exc:
                    log.debug("Could not save session after failure: %s", save_exc)
            raise
        finally:
            if keep_browser_open:
                log.debug("Skipping browser close (--keep-browser-open was used)")
            else:
                log.debug("Closing browser")
                try:
                    if context is not None:
                        await context.close()
                    if browser is not None:
                        await browser.close()
                except PlaywrightError as exc:
                    log.debug("Browser cleanup note: %s", exc)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Waystar claims listing via PerformSearch"
    )
    parser.add_argument(
        "--transaction-days",
        type=int,
        default=None,
        help="Filter claims by transaction date (default: WAYSTAR_TRANSACTION_DAYS or 30)",
    )
    parser.add_argument(
        "--rejected",
        action="store_true",
        help="Search rejected claims (Status=12345) with calendar date range",
    )
    parser.add_argument(
        "--status",
        default=None,
        help="SearchCriteria.Status value (e.g. 12345 for rejected)",
    )
    parser.add_argument(
        "--trans-from",
        default=None,
        metavar="MM/DD/YYYY",
        help="Transaction date range start (requires --trans-to or --rejected defaults)",
    )
    parser.add_argument(
        "--trans-to",
        default=None,
        metavar="MM/DD/YYYY",
        help="Transaction date range end",
    )
    parser.add_argument(
        "--transaction-span",
        default=None,
        help="Override SearchCriteria.TransactionDateSpan (default: Custom for calendar dates)",
    )
    parser.add_argument(
        "--checkpoint-pages",
        type=int,
        default=None,
        metavar="N",
        help="Flush partial CSV/JSON every N pages (requires --batch-claims)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of pages to fetch (default: all)",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Start pagination at this page (for resume after interruption)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run identifier for output dirs (default: timestamp). Reuse to resume a run.",
    )
    parser.add_argument(
        "--batch-claims",
        type=int,
        default=None,
        metavar="N",
        help=f"Write a batch CSV/JSON every N claims (default: {DEFAULT_BATCH_CLAIMS} when set without value is not used — pass explicit N, e.g. 10000)",
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Do not close the browser until you press Enter (session stays live)",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=DEFAULT_PAGE_DELAY_SEC,
        help=f"Delay between page requests in seconds (default: {DEFAULT_PAGE_DELAY_SEC})",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Output filename prefix for non-batch mode (default: claims_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode (visible window)",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="Ignore saved session and force new login",
    )
    parser.add_argument(
        "--download-pdfs",
        action="store_true",
        help="Download CMS-1500 PDF for each claim in scrape scope",
    )
    parser.add_argument(
        "--pdf-delay",
        type=float,
        default=None,
        help=f"Delay between PDF downloads in seconds (default: {DEFAULT_PDF_DELAY_SEC})",
    )
    parser.add_argument(
        "--pdf-timeout",
        type=float,
        default=None,
        help=f"Per-PDF download timeout in seconds (default: {DEFAULT_PDF_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--network-retries",
        type=int,
        default=None,
        metavar="N",
        help=f"Retry transient network errors up to N times (default: {DEFAULT_NETWORK_RETRY_ATTEMPTS})",
    )
    parser.add_argument(
        "--network-retry-delay",
        type=float,
        default=None,
        metavar="SEC",
        help=f"Base delay between network retries in seconds (default: {DEFAULT_NETWORK_RETRY_DELAY_SEC})",
    )
    parser.add_argument(
        "--pdf-form",
        default=None,
        help="Form parameter for ViewClaimPDF (default: CMS1500_0212)",
    )
    parser.add_argument(
        "--pdf-skip-existing",
        action="store_true",
        default=True,
        help="Skip PDF download if file already exists (default: on)",
    )
    parser.add_argument(
        "--no-pdf-skip-existing",
        action="store_false",
        dest="pdf_skip_existing",
        help="Re-download PDFs even if file exists",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Save screenshots for each step (default: on when --headed)",
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Disable screenshots",
    )
    parser.add_argument(
        "--screenshot-every-page",
        action="store_true",
        help="Screenshot after every claims search page (not just the first)",
    )
    parser.add_argument(
        "--human-delay-min",
        type=float,
        default=None,
        help="Min seconds between human-like actions",
    )
    parser.add_argument(
        "--human-delay-max",
        type=float,
        default=None,
        help="Max seconds between human-like actions",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Console/file log verbosity (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional log file path (default: output/scrape_YYYYMMDD_HHMMSS.log)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Do not write a log file, console only",
    )
    return parser


def build_search_criteria(args: argparse.Namespace, config: WaystarConfig) -> SearchCriteria:
    use_calendar = bool(args.rejected or args.trans_from or args.trans_to or args.status)
    if not use_calendar:
        days = args.transaction_days if args.transaction_days is not None else config.transaction_days
        return criteria_from_transaction_days(days)

    status = args.status or (REJECTED_STATUS_CODE if args.rejected else "-1")
    trans_from = (
        args.trans_from
        or os.getenv("WAYSTAR_TRANS_FROM", "").strip()
        or (DEFAULT_REJECTED_TRANS_FROM if args.rejected else "")
    )
    trans_to = (
        args.trans_to
        or os.getenv("WAYSTAR_TRANS_TO", "").strip()
        or (DEFAULT_REJECTED_TRANS_TO if args.rejected else "")
    )
    if not trans_from or not trans_to:
        raise ValueError(
            "Calendar search requires --trans-from and --trans-to (or use --rejected for defaults)"
        )

    span = args.transaction_span
    if span is None:
        span = "Custom"

    return criteria_rejected_calendar(
        trans_from,
        trans_to,
        status=status,
        transaction_date_span=span,
    )


def export_dir_for_run(*, rejected: bool, run_id: str) -> Path:
    prefix = "claims_rejected" if rejected else "claims_90d"
    return OUTPUT_DIR / f"{prefix}_{run_id}"


async def run_cli(args: argparse.Namespace) -> int:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = args.run_id or timestamp
    log_file = None
    if not args.no_log_file:
        log_file = Path(args.log_file) if args.log_file else OUTPUT_DIR / f"scrape_{run_id}.log"

    setup_logging(level=args.log_level, log_file=log_file)

    config = WaystarConfig.from_env(headless=not args.headed)
    if args.transaction_days is not None:
        config.transaction_days = args.transaction_days
    if args.network_retries is not None:
        config.network_retry_attempts = args.network_retries
    if args.network_retry_delay is not None:
        config.network_retry_delay_sec = args.network_retry_delay

    screenshots_enabled = args.screenshots or (args.headed and not args.no_screenshots)
    human = build_human_settings(
        config,
        screenshots_enabled=screenshots_enabled,
        screenshot_every_page=args.screenshot_every_page,
        run_id=run_id,
        action_delay_min=args.human_delay_min,
        action_delay_max=args.human_delay_max,
    )

    pdf_options = PdfOptions(
        enabled=args.download_pdfs,
        delay_sec=args.pdf_delay if args.pdf_delay is not None else config.pdf_delay_sec,
        timeout_sec=args.pdf_timeout if args.pdf_timeout is not None else DEFAULT_PDF_TIMEOUT_SEC,
        form=args.pdf_form if args.pdf_form is not None else config.claim_form,
        skip_existing=args.pdf_skip_existing,
        run_id=run_id,
    )

    search_criteria = build_search_criteria(args, config)

    batch_claims = args.batch_claims
    export_dir: Path | None = None
    if batch_claims is not None:
        export_dir = export_dir_for_run(rejected=args.rejected, run_id=run_id)
    elif args.checkpoint_pages is not None:
        raise ValueError("--checkpoint-pages requires --batch-claims")

    if log_file:
        log.info("Log file: %s", log_file)

    result = await scrape_claims(
        config,
        human,
        pdf_options,
        search_criteria=search_criteria,
        max_pages=args.max_pages,
        page_delay_sec=args.page_delay,
        reuse_session=not args.fresh_login,
        start_page=args.start_page,
        batch_claims=batch_claims,
        export_dir=export_dir,
        export_format=args.format,
        checkpoint_pages=args.checkpoint_pages,
        keep_browser_open=args.keep_browser_open,
    )

    if batch_claims is None:
        prefix = args.output_prefix or f"claims_{run_id}"
        csv_path = OUTPUT_DIR / f"{prefix}.csv"
        json_path = OUTPUT_DIR / f"{prefix}.json"

        if args.format in {"csv", "both"}:
            export_csv(result["claims"], csv_path)

        if args.format in {"json", "both"}:
            export_json(result, json_path)
    else:
        log.info(
            "Batch export complete — %s batch(es) in %s. Run merge_exports.py to combine.",
            result["batches_written"],
            export_dir,
        )

    return 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_cli(args)))
    except ValueError as exc:
        logging.getLogger("waystar").error("Configuration error: %s", exc)
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        logging.getLogger("waystar").error("Scrape failed: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logging.getLogger("waystar").warning("Interrupted by user (Ctrl+C)")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
