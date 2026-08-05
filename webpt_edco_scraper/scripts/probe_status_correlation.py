"""Correlate status codes with checkin/checkout_time from one facility day."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    list_clinics,
    safe_close_context,
    switch_clinic,
)
from config import SCHEDULER_INDEX_URL, WebPTConfig
from scheduler_api import fetch_scheduler_events, is_patient_appointment


async def main() -> None:
    config = WebPTConfig.from_env()
    config.headless = True
    tz = ZoneInfo(config.timezone)
    target = datetime.now(tz).date() - timedelta(days=1)

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            clinics = await list_clinics(page, config.company_id)
            clinic = clinics[0]
            await switch_clinic(
                page,
                company_id=clinic.company_id,
                facility_id=clinic.facility_id,
            )
            await page.goto(
                SCHEDULER_INDEX_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            session = await ensure_authenticated(page, context, config)
            # yesterday + today for status mix
            events = await fetch_scheduler_events(
                context,
                facility_id=clinic.facility_id,
                start_date=target,
                end_date=target + timedelta(days=1),
                session=session,
                config=config,
            )
            pe = [e for e in events if is_patient_appointment(e)]
            by_status: dict[object, Counter] = defaultdict(Counter)
            for e in pe:
                st = e.get("status")
                ci = bool(e.get("checkin_time"))
                co = bool(e.get("checkout_time"))
                by_status[st][f"checkin={ci} checkout={co}"] += 1

            print(f"facility={clinic.name} n={len(pe)}")
            for st, ctr in sorted(by_status.items(), key=lambda x: str(x[0])):
                print(f"status={st}")
                for k, n in ctr.most_common():
                    print(f"  {n:3d} {k}")

            out = ROOT / "output" / "probe_status_correlation.json"
            out.write_text(
                json.dumps(
                    {
                        "facility": clinic.name,
                        "n": len(pe),
                        "by_status": {str(k): dict(v) for k, v in by_status.items()},
                        "events": pe,
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print(f"wrote {out}")
        finally:
            await safe_close_context(context)


if __name__ == "__main__":
    asyncio.run(main())
