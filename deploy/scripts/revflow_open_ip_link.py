#!/usr/bin/env python3
"""Open a RevFlow ipRegistration URL from the Contabo egress IP (Playwright)."""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

DEFAULT_URL = (
    "https://billing.revflow.com/ipRegistration??e8f47bdbf2264a51b18e18ad5b3e9e70"
)


async def main(url: str) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(5000)
        body = (await page.inner_text("body"))[:800]
        print("final_url=", page.url)
        print("title=", await page.title())
        print("body=", body)
        await browser.close()
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    raise SystemExit(asyncio.run(main(target)))
