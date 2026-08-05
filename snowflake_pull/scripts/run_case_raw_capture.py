"""Live Raw Capture pass for downloaded cases (same WebPT session — exclusive).

Do NOT run while case drain holds the browser. Pass --i-confirm-exclusive-session.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from auth import SessionState, ensure_logged_in, switch_clinic  # noqa: E402
from case_raw_capture import capture_case_raw  # noqa: E402
from config import STORAGE_STATE_PATH, load_config  # noqa: E402

DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"


def _load_targets(
    sqlite_path: Path,
    *,
    batch_id: str,
    limit: int,
    facility_id: str | None,
) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(str(sqlite_path))
    sql = (
        "SELECT DISTINCT facility_id, case_id, patient_id FROM case_units "
        "WHERE batch_id=? AND state='downloaded'"
    )
    params: list[object] = [batch_id]
    if facility_id:
        sql += " AND facility_id=?"
        params.append(facility_id)
    sql += " ORDER BY facility_id, case_id LIMIT ?"
    params.append(limit)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


async def _run(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    targets = _load_targets(
        args.sqlite,
        batch_id=args.batch_id,
        limit=args.limit,
        facility_id=args.facility_id,
    )
    if not targets:
        print("No downloaded cases to capture.")
        return 0

    config = load_config()
    config.headless = args.headless
    session = SessionState()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            storage_state=str(STORAGE_STATE_PATH)
            if STORAGE_STATE_PATH.exists()
            else None
        )
        page = await context.new_page()
        await ensure_logged_in(page, config, session)

        current_fac = ""
        ok = 0
        for facility_id, case_id, patient_id in targets:
            if facility_id != current_fac:
                await switch_clinic(page, int(facility_id), config, session)
                current_fac = facility_id
            result = await capture_case_raw(
                context,
                base_dir=args.artifacts,
                facility_id=facility_id,
                case_id=int(case_id),
                patient_id=int(patient_id),
                config=config,
                session=session,
                include_scheduler_fetch=not args.skip_scheduler,
            )
            ok += 1
            print(
                f"[{ok}/{len(targets)}] {facility_id}/{case_id} "
                f"raw={result['coverage']['coverage_pct']}% "
                f"errors={len(result['errors'])}"
            )

        await context.storage_state(path=str(STORAGE_STATE_PATH))
        await browser.close()
    print(f"Captured raw for {ok} cases under {args.artifacts / 'cases'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    ap.add_argument(
        "--sqlite",
        type=Path,
        default=DEFAULT_ARTIFACTS / "case_units.sqlite",
    )
    ap.add_argument("--batch-id", default="case_schedule_202601_202608")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--facility-id", default=None)
    ap.add_argument("--skip-scheduler", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--i-confirm-exclusive-session", action="store_true")
    args = ap.parse_args()
    if not args.i_confirm_exclusive_session:
        print(
            "Refusing: stop drain and pass --i-confirm-exclusive-session "
            "(Golden Rule: one WebPT session).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
