"""Exploration: find where detailed rejection messages (SMARTEDIT / How to Fix) come from.

Part 1: dump raw PerformSearch HTML for rejected claims — check if details are embedded.
Part 2: open the claim editor page, capture all network responses, and flag the ones
        containing rejection-detail keywords.
"""

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import Error as PlaywrightError, async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    get_verification_token,
    resolve_cust_id,
    retry_settings_from_config,
)
from claims_search import build_search_form, criteria_rejected_calendar, perform_search
from config import OUTPUT_DIR, WaystarConfig
from human import HumanSettings
from logging_config import get_logger, setup_logging

log = get_logger("explore")

EXPLORE_DIR = OUTPUT_DIR / "explore_rejection"
EDITOR_CLAIM_ID = "11908500435"
EDITOR_URL = (
    "https://claims.zirmed.com/Editor/V5010/Professional/Main.aspx"
    f"?&sec=0&draft=N&editclaimid={EDITOR_CLAIM_ID}&origin=ClaimListingNew&workgroupName=undefined"
)

KEYWORDS = [
    "smartedit",
    "how to fix",
    "howtofix",
    "p4999",
    "duplicate of a previously",
    "fu98587139",
]

TEXTUAL_TYPES = ("text/", "application/json", "application/javascript", "application/xml")


def _safe_name(url: str, index: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", url.split("?")[0].split("//")[-1])
    return f"{index:03d}_{cleaned[-120:]}"


async def main() -> None:
    setup_logging(level="INFO")
    EXPLORE_DIR.mkdir(parents=True, exist_ok=True)

    config = WaystarConfig.from_env(headless=True)
    human = HumanSettings(screenshots_enabled=True, screenshot_dir=EXPLORE_DIR)

    async with async_playwright() as playwright:
        browser, context = await create_context(playwright, config, reuse_session=True)
        try:
            page = await ensure_authenticated(context, config, human)
            retry = retry_settings_from_config(config)

            # ---- Part 1: raw rejected-search HTML ----
            log.info("PART 1: raw PerformSearch HTML for rejected claims")
            cust_id = await resolve_cust_id(page, config)
            token = await get_verification_token(page)
            criteria = criteria_rejected_calendar("01/01/2026", "12/31/2026")
            form_body = build_search_form(
                token=token, cust_id=cust_id, app_id=config.app_id,
                page_number=1, criteria=criteria,
            )
            html = await perform_search(page, token, form_body, page_number=1, retry=retry)
            raw_path = EXPLORE_DIR / "perform_search_rejected_page1.html"
            raw_path.write_text(html, encoding="utf-8")
            log.info("Saved raw search HTML (%s bytes) to %s", len(html), raw_path)
            lowered = html.lower()
            for kw in KEYWORDS:
                log.info("  keyword %-28r in search HTML: %s", kw, kw in lowered)

            # ---- Part 2: editor page network capture ----
            log.info("PART 2: opening editor for claim %s and capturing traffic", EDITOR_CLAIM_ID)
            editor_page = await context.new_page()
            manifest: list[dict] = []
            hits: list[dict] = []
            counter = {"n": 0}

            async def on_response(response):
                counter["n"] += 1
                index = counter["n"]
                url = response.url
                entry = {
                    "index": index,
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "method": response.request.method,
                    "post_data": (response.request.post_data or "")[:1500],
                }
                manifest.append(entry)
                ct = entry["content_type"].lower()
                if not any(t in ct for t in TEXTUAL_TYPES):
                    return
                try:
                    body = await response.text()
                except PlaywrightError:
                    return
                body_lower = body.lower()
                matched = [kw for kw in KEYWORDS if kw in body_lower]
                if matched:
                    entry["matched_keywords"] = matched
                    fname = _safe_name(url, index) + ".txt"
                    (EXPLORE_DIR / fname).write_text(
                        f"URL: {url}\nMETHOD: {entry['method']}\nSTATUS: {entry['status']}\n"
                        f"CONTENT-TYPE: {entry['content_type']}\nPOST: {entry['post_data']}\n"
                        f"{'=' * 80}\n{body}",
                        encoding="utf-8",
                    )
                    entry["saved_as"] = fname
                    hits.append(entry)
                    log.info("HIT #%s %s %s -> %s (keywords: %s)",
                             index, entry["method"], url[:120], fname, matched)

            editor_page.on("response", lambda r: asyncio.create_task(on_response(r)))

            await editor_page.goto(EDITOR_URL, wait_until="domcontentloaded", timeout=120_000)
            log.info("Editor page loaded: %s", editor_page.url)
            await asyncio.sleep(25)  # let all XHRs/iframes finish

            # Dump every frame's URL and content
            frames_info = []
            for i, frame in enumerate(editor_page.frames):
                try:
                    content = await frame.content()
                except PlaywrightError:
                    content = "(unavailable)"
                frames_info.append({"index": i, "url": frame.url, "size": len(content)})
                fpath = EXPLORE_DIR / f"frame_{i:02d}.html"
                fpath.write_text(f"<!-- {frame.url} -->\n{content}", encoding="utf-8")
                content_lower = content.lower()
                matched = [kw for kw in KEYWORDS if kw in content_lower]
                if matched:
                    log.info("FRAME HIT: frame %s url=%s keywords=%s -> %s",
                             i, frame.url[:120], matched, fpath.name)
                    frames_info[-1]["matched_keywords"] = matched

            await editor_page.screenshot(
                path=str(EXPLORE_DIR / "editor_page.png"), full_page=True
            )

            (EXPLORE_DIR / "network_manifest.json").write_text(
                json.dumps({"frames": frames_info, "responses": manifest, "hits": hits},
                           indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Captured %s responses, %s keyword hits, %s frames",
                     len(manifest), len(hits), len(frames_info))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
