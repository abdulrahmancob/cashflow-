"""Debug: dump editor HTML for claims that returned 0 rejection messages."""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from auth import create_context, ensure_authenticated
from config import OUTPUT_DIR, WaystarConfig
from human import HumanSettings
from logging_config import get_logger, setup_logging
from rejection_details import editor_url, parse_rejection_details

log = get_logger("debug")


async def main(claim_ids: list[str]) -> None:
    setup_logging(level="INFO")
    out_dir = OUTPUT_DIR / "explore_rejection"
    out_dir.mkdir(parents=True, exist_ok=True)

    config = WaystarConfig.from_env(headless=True)
    human = HumanSettings()

    async with async_playwright() as playwright:
        browser, context = await create_context(playwright, config, reuse_session=True)
        try:
            page = await ensure_authenticated(context, config, human)
            for claim_id in claim_ids:
                response = await page.request.get(
                    editor_url(claim_id),
                    headers={"Referer": "https://claims.zirmed.com/Claims/Listing/Index?appid=1"},
                )
                html = await response.text()
                path = out_dir / f"debug_editor_{claim_id}.html"
                path.write_text(html, encoding="utf-8")
                details = parse_rejection_details(html)
                lowered = html.lower()
                log.info(
                    "claim %s: HTTP %s size=%s grid=%s count=%s | markers: rejectiongrid=%s "
                    "log_off=%s login=%s readonly=%s locked=%s",
                    claim_id, response.status, len(html),
                    details["found_grid"], details["rejection_count"],
                    "rejectiongrid" in lowered,
                    "waystar log off" in lowered,
                    "login.zirmed.com" in lowered,
                    "readonly" in lowered,
                    "locked" in lowered,
                )
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["11193626736", "11900885472"]))
