"""Exploration: Waystar Denials Workcenter list + Review detail pages.

Part 1: Navigate WorkCenter entry → Denials Workcenter, capture HTML + network.
Part 2: Open a sample Review page, capture remit tables and network traffic.
Part 3: Write explore_findings.md summarizing structure for parser implementation.
"""

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, async_playwright

from auth import create_context, ensure_authenticated, extend_session, retry_settings_from_config
from config import (
    DEFAULT_SAMPLE_DENIAL_ID,
    DENIALS_REVIEW_URL_TEMPLATE,
    DENIALS_WORKCENTER_URL,
    OUTPUT_DIR,
    WORKCENTER_ENTRY_URL,
    WaystarConfig,
)
from human import HumanSettings
from logging_config import get_logger, setup_logging

log = get_logger("explore_denials")

EXPLORE_DIR = OUTPUT_DIR / "explore_denials"

KEYWORDS = [
    "denialid",
    "remit",
    "workcenter",
    "adjustment",
    "carc",
    "performsearch",
    "grid",
    "pagination",
    "remitstable",
    "historyremitlink",
]

TEXTUAL_TYPES = ("text/", "application/json", "application/javascript", "application/xml")


def _safe_name(url: str, index: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", url.split("?")[0].split("//")[-1])
    return f"{index:03d}_{cleaned[-120:]}"


def denial_review_url(denial_id: str) -> str:
    return DENIALS_REVIEW_URL_TEMPLATE.format(denial_id=denial_id)


class NetworkCapture:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.manifest: list[dict] = []
        self.hits: list[dict] = []
        self._counter = 0

    async def on_response(self, response) -> None:
        self._counter += 1
        index = self._counter
        url = response.url
        post_data = ""
        try:
            post_data = (response.request.post_data or "")[:2000]
        except (UnicodeDecodeError, PlaywrightError):
            post_data = "(binary)"
        entry = {
            "index": index,
            "url": url,
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
            "method": response.request.method,
            "post_data": post_data,
        }
        self.manifest.append(entry)
        ct = entry["content_type"].lower()
        if not any(t in ct for t in TEXTUAL_TYPES):
            return
        try:
            body = await response.text()
        except PlaywrightError:
            return
        body_lower = body.lower()
        matched = [kw for kw in KEYWORDS if kw in body_lower]
        if matched or "denials.zirmed.com" in url.lower():
            entry["matched_keywords"] = matched
            fname = _safe_name(url, index) + ".txt"
            (self.out_dir / fname).write_text(
                f"URL: {url}\nMETHOD: {entry['method']}\nSTATUS: {entry['status']}\n"
                f"CONTENT-TYPE: {entry['content_type']}\nPOST: {entry['post_data']}\n"
                f"{'=' * 80}\n{body[:500_000]}",
                encoding="utf-8",
            )
            entry["saved_as"] = fname
            self.hits.append(entry)
            log.info(
                "HIT #%s %s %s -> %s (keywords: %s)",
                index,
                entry["method"],
                url[:120],
                fname,
                matched,
            )


def _analyze_workcenter_html(html: str) -> dict:
    lowered = html.lower()
    findings: dict = {
        "html_size": len(html),
        "has_denialid": "denialid" in lowered,
        "has_remit": "remit" in lowered,
        "has_historyremitlink": "historyremitlink" in lowered,
        "has_grid_table": "<table" in lowered,
        "denial_id_patterns": [],
        "review_link_count": len(re.findall(r"Review\?denialID=\d+", html, re.I)),
        "data_denialid_count": len(re.findall(r'data-denialid=["\'](\d+)', html, re.I)),
        "denial_ids_sample": list(dict.fromkeys(
            re.findall(r'data-denialid=["\'](\d+)', html, re.I)
        ))[:10],
        "review_denial_ids_sample": list(dict.fromkeys(
            re.findall(r"denialID=(\d+)", html, re.I)
        ))[:10],
    }
    for pattern in [
        r'id=["\']denialGrid',
        r'class=["\'][^"\']*denial[^"\']*["\']',
        r'data-denialid',
        r'Workcenter',
        r'pagination',
        r'pageSize',
        r'totalRecords',
    ]:
        if re.search(pattern, html, re.I):
            findings["denial_id_patterns"].append(pattern)
    return findings


def _analyze_review_html(html: str) -> dict:
    lowered = html.lower()
    return {
        "html_size": len(html),
        "has_remits_container": "remitscontainer" in lowered,
        "history_remit_links": len(re.findall(r'historyRemitLink', html, re.I)),
        "remit_tables": len(re.findall(r'class=["\']remitTable', html, re.I)),
        "remit_instance_ids": list(dict.fromkeys(
            re.findall(r'data-instanceid=["\'](\d+)', html, re.I)
        ))[:10],
        "carc_codes_sample": list(dict.fromkeys(
            re.findall(r'\b(?:CO|PR|PI|OA)-\d+\b', html)
        ))[:15],
        "remark_codes_sample": list(dict.fromkeys(
            re.findall(r'\bM\d+\b', html)
        ))[:10],
        "adjustment_statuses": list(dict.fromkeys(
            re.findall(r'>\s*(Active|Closed|Inactive)\s*<', html)
        )),
    }


def _write_findings(
    out_dir: Path,
    workcenter: dict,
    review: dict,
    workcenter_url: str,
    review_url: str,
    sample_denial_id: str,
) -> None:
    lines = [
        "# Waystar Denials Exploration Findings",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Navigation",
        "",
        f"1. WorkCenter entry: `{WORKCENTER_ENTRY_URL}`",
        f"2. Denials Workcenter: `{DENIALS_WORKCENTER_URL}`",
        f"3. Review sample: `{review_url}`",
        "",
        "## Workcenter List",
        "",
        f"- Final URL: `{workcenter_url}`",
        f"- HTML size: {workcenter.get('html_size', 0):,} bytes",
        f"- Review links found: {workcenter.get('review_link_count', 0)}",
        f"- data-denialid attributes: {workcenter.get('data_denialid_count', 0)}",
        f"- Sample denial IDs: {', '.join(workcenter.get('denial_ids_sample', [])) or '(none)'}",
        f"- List appears server-rendered HTML: **{'yes' if workcenter.get('has_grid_table') else 'unknown'}**",
        "",
        "## Review Detail",
        "",
        f"- Denial ID: `{sample_denial_id}`",
        f"- Final URL: `{review_url}`",
        f"- Has #remitsContainer: {review.get('has_remits_container')}",
        f"- historyRemitLink count: {review.get('history_remit_links', 0)}",
        f"- remitTable count: {review.get('remit_tables', 0)}",
        f"- CARC codes sample: {', '.join(review.get('carc_codes_sample', []))}",
        f"- Adjustment statuses: {', '.join(review.get('adjustment_statuses', []))}",
        "",
        "## Parser Notes",
        "",
        "- Workcenter: parse denial rows from HTML grid; denial_id from Review links or data-denialid.",
        "- Review: parse `a.historyRemitLink` + `table.remitTable` rows including hidden inactive remits.",
        "- Tooltips: CARC/RARC descriptions are embedded in `.toolTipInner` (no hover needed).",
        "- Resolution: Closed adjustments include WriteOff/Paid + date + Automatic Rule or Manual ID.",
        "",
    ]
    (out_dir / "explore_findings.md").write_text("\n".join(lines), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    setup_logging(level=args.log_level)
    EXPLORE_DIR.mkdir(parents=True, exist_ok=True)

    config = WaystarConfig.from_env(headless=not args.headed)
    human = HumanSettings(screenshots_enabled=True, screenshot_dir=EXPLORE_DIR)
    sample_denial_id = args.denial_id or DEFAULT_SAMPLE_DENIAL_ID
    review_url = denial_review_url(sample_denial_id)

    async with async_playwright() as playwright:
        browser, context = await create_context(playwright, config, reuse_session=True)
        try:
            page = await ensure_authenticated(context, config, human)
            await extend_session(page)

            # ---- Part 1: WorkCenter entry → Denials Workcenter ----
            log.info("PART 1: WorkCenter entry")
            wc_capture = NetworkCapture(EXPLORE_DIR)
            page.on("response", lambda r: asyncio.create_task(wc_capture.on_response(r)))

            await page.goto(WORKCENTER_ENTRY_URL, wait_until="domcontentloaded", timeout=120_000)
            log.info("WorkCenter entry URL: %s", page.url)
            await asyncio.sleep(3)

            log.info("PART 1b: Denials Workcenter")
            await page.goto(DENIALS_WORKCENTER_URL, wait_until="domcontentloaded", timeout=120_000)
            log.info("Denials Workcenter URL: %s", page.url)
            await asyncio.sleep(8)

            workcenter_html = await page.content()
            workcenter_path = EXPLORE_DIR / "workcenter_page1.html"
            workcenter_path.write_text(workcenter_html, encoding="utf-8")
            log.info("Saved workcenter HTML (%s bytes)", len(workcenter_html))

            await page.screenshot(path=str(EXPLORE_DIR / "workcenter_page1.png"), full_page=True)
            workcenter_analysis = _analyze_workcenter_html(workcenter_html)

            # ---- Part 2: Review detail ----
            log.info("PART 2: Review page for denial %s", sample_denial_id)
            review_page = await context.new_page()
            review_capture = NetworkCapture(EXPLORE_DIR)
            review_page.on("response", lambda r: asyncio.create_task(review_capture.on_response(r)))

            await review_page.goto(review_url, wait_until="domcontentloaded", timeout=120_000)
            log.info("Review URL: %s", review_page.url)
            await asyncio.sleep(10)

            review_html = await review_page.content()
            review_path = EXPLORE_DIR / f"review_{sample_denial_id}.html"
            review_path.write_text(review_html, encoding="utf-8")
            log.info("Saved review HTML (%s bytes)", len(review_html))

            await review_page.screenshot(
                path=str(EXPLORE_DIR / f"review_{sample_denial_id}.png"), full_page=True
            )

            frames_info = []
            for i, frame in enumerate(review_page.frames):
                try:
                    content = await frame.content()
                except PlaywrightError:
                    content = "(unavailable)"
                frames_info.append({"index": i, "url": frame.url, "size": len(content)})
                fpath = EXPLORE_DIR / f"review_frame_{i:02d}.html"
                fpath.write_text(f"<!-- {frame.url} -->\n{content}", encoding="utf-8")

            review_analysis = _analyze_review_html(review_html)

            combined_manifest = {
                "workcenter": {
                    "url": page.url,
                    "analysis": workcenter_analysis,
                    "responses": wc_capture.manifest,
                    "hits": wc_capture.hits,
                },
                "review": {
                    "url": review_page.url,
                    "denial_id": sample_denial_id,
                    "analysis": review_analysis,
                    "frames": frames_info,
                    "responses": review_capture.manifest,
                    "hits": review_capture.hits,
                },
            }
            (EXPLORE_DIR / "network_manifest.json").write_text(
                json.dumps(combined_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            _write_findings(
                EXPLORE_DIR,
                workcenter_analysis,
                review_analysis,
                page.url,
                review_page.url,
                sample_denial_id,
            )

            log.info(
                "Exploration complete: workcenter review_links=%s, review remit_tables=%s",
                workcenter_analysis.get("review_link_count"),
                review_analysis.get("remit_tables"),
            )
            await review_page.close()
        finally:
            await context.close()
            await browser.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Explore Waystar Denials UI and network traffic")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    parser.add_argument("--denial-id", default=None, help="Sample denial ID for Review page")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    args = parser.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
