"""One-off: dump scheduler event keys + candidate status field values."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
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
    target = date.today().replace()  # overridden below
    target = (date.now(tz) if hasattr(date, "now") else __import__("datetime").datetime.now(tz).date()) - timedelta(days=1)

    from datetime import datetime as dt

    target = dt.now(tz).date() - timedelta(days=1)

    async with async_playwright() as playwright:
        context = await create_context(playwright, config)
        page = await context.new_page()
        try:
            session = await ensure_authenticated(page, context, config)
            clinics = await list_clinics(page, config.company_id)
            if not clinics:
                raise SystemExit("No clinics found")
            clinic = clinics[0]
            print(f"Clinic: {clinic.name} ({clinic.facility_id})")
            print(f"Date window: {target} .. {target}")

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

            events = await fetch_scheduler_events(
                context,
                facility_id=clinic.facility_id,
                start_date=target,
                end_date=target,
                session=session,
                config=config,
            )
            print(f"Total events: {len(events)}")
            patient_events = [e for e in events if is_patient_appointment(e)]
            print(f"Patient events: {len(patient_events)}")
            if not patient_events:
                # broaden to today if yesterday empty
                target2 = target + timedelta(days=1)
                events = await fetch_scheduler_events(
                    context,
                    facility_id=clinic.facility_id,
                    start_date=target,
                    end_date=target2,
                    session=session,
                    config=config,
                )
                patient_events = [e for e in events if is_patient_appointment(e)]
                print(f"Retry {target}..{target2}: patient events={len(patient_events)}")

            if not patient_events:
                print("No patient events to inspect")
                return

            sample = patient_events[0]
            keys = sorted(sample.keys())
            print("Sample keys:", keys)
            print("Sample event:")
            print(json.dumps(sample, indent=2, default=str)[:4000])

            candidates = [
                k
                for k in keys
                if any(
                    tok in k.lower()
                    for tok in (
                        "status",
                        "check",
                        "out",
                        "in",
                        "cancel",
                        "show",
                        "arrive",
                        "visit",
                        "appt",
                        "state",
                        "type",
                        "color",
                        "cls",
                        "class",
                        "flag",
                    )
                )
            ]
            print("Candidate keys:", candidates)

            value_sets: dict[str, Counter] = defaultdict(Counter)
            for e in patient_events:
                for k in candidates or keys:
                    value_sets[k][repr(e.get(k))] += 1

            for k, ctr in value_sets.items():
                top = ctr.most_common(15)
                print(f"\n=== {k} ({sum(ctr.values())} values, {len(ctr)} unique) ===")
                for val, n in top:
                    print(f"  {n:4d}  {val}")

            out = ROOT / "output" / "probe_scheduler_status.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "facility_id": clinic.facility_id,
                        "facility_name": clinic.name,
                        "sample_keys": keys,
                        "sample_event": sample,
                        "candidate_value_counts": {
                            k: dict(ctr.most_common(30)) for k, ctr in value_sets.items()
                        },
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print(f"\nWrote {out}")
        finally:
            await safe_close_context(context)


if __name__ == "__main__":
    asyncio.run(main())
