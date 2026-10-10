"""Pull Waystar All-Claims listing using the probed export strategy.

Proven endpoint (2026-08-19 reverse-engineer):
  POST PerformSearch (stores session filters)
  GET  /Claims/Listing/DownloadCsv?AppID=1&excelFriendly=true
Offset is ignored. CSV is hard-capped at 10,000 rows, so date windows are used
only after that evidence — not as an assumption.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from auth import (
    create_context,
    ensure_authenticated,
    extend_session,
    refresh_verification_token,
    resolve_cust_id,
    save_storage_state,
)
from claims_export import (
    DATE_WINDOW_FALLBACK,
    CompletenessError,
    DEFAULT_RECENT_MONTHS_BACK,
    RECENT_LOOKBACK_DAYS,
    RECENT_MAX_LOOKBACK_DAYS,
    bisect_date_windows,
    completeness_gate,
    format_mdy,
    load_probe_report,
    merge_claim_rows,
    months_back_window,
    parse_claims_screen_csv,
    write_probe_report,
)
from claims_search import SearchCriteria, build_search_form, perform_search
from config import DOWNLOAD_CSV_URL, OUTPUT_DIR, WaystarConfig
from human import HumanSettings, human_pause
from logging_config import get_logger, setup_logging
from parser import parse_search_result_html

log = get_logger("download_claims")

CSV_CAP = 10_000


def _criteria(trans_from: str, trans_to: str, status: str) -> SearchCriteria:
    return SearchCriteria(
        transaction_date_span="Custom",
        trans_from=trans_from,
        trans_to=trans_to,
        transaction_label="custom",
        status=status,
        allow_download_csv=True,
    )


def _download_url(app_id: str, excel_friendly: bool = True) -> str:
    flag = "true" if excel_friendly else "false"
    return f"{DOWNLOAD_CSV_URL}?AppID={app_id}&excelFriendly={flag}"


async def _search(
    page,
    token: str,
    cust_id: str,
    app_id: str,
    criteria: SearchCriteria,
) -> dict:
    form = build_search_form(
        token=token,
        cust_id=cust_id,
        app_id=app_id,
        page_number=1,
        criteria=criteria,
        allow_download_csv=True,
    )
    html = await perform_search(page, token, form, page_number=1)
    parsed = parse_search_result_html(html)
    return parsed


async def _get_csv(page, app_id: str, dest: Path) -> list[dict]:
    url = _download_url(app_id)
    log.info("GET %s → %s", url, dest)
    response = await page.request.get(url, timeout=180_000)
    body = await response.body()
    content_type = response.headers.get("content-type", "")
    if response.status != 200 or (b"Trans Date" not in body[:4000] and "csv" not in content_type.lower()):
        preview = body[:200].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"DownloadCsv failed HTTP {response.status} type={content_type} preview={preview!r}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return parse_claims_screen_csv(dest)


async def _resolve_recent_window(
    page,
    *,
    token: str,
    cust_id: str,
    app_id: str,
    status: str,
    csv_cap: int,
    lookback_days: int,
    human: HumanSettings,
) -> tuple[date, date, dict]:
    """Live UI-count binary search matching choose_recent_window."""
    end = datetime.now(ZoneInfo("Africa/Cairo")).date()
    cache: dict[int, int | None] = {}

    async def count_days(days: int) -> int | None:
        days = max(int(days), 0)
        if days in cache:
            return cache[days]
        start = end - timedelta(days=days)
        parsed = await _search(
            page,
            token,
            cust_id,
            app_id,
            _criteria(format_mdy(start), format_mdy(end), status),
        )
        total = parsed.get("total_results")
        cache[days] = int(total) if total is not None else None
        log.info("recent probe lookback=%s ui_total=%s", days, cache[days])
        await human_pause(human, 1.0)
        return cache[days]

    n = await count_days(lookback_days)
    if n is None:
        start = end - timedelta(days=lookback_days)
        return start, end, {"lookback_days": lookback_days, "ui_total": None, "probes": cache}

    if n <= csv_cap:
        lo, hi = lookback_days, RECENT_MAX_LOOKBACK_DAYS
        best = lookback_days
        while lo <= hi:
            mid = (lo + hi) // 2
            mid_n = await count_days(mid)
            if mid_n is None or mid_n > csv_cap:
                hi = mid - 1
            else:
                best = mid
                lo = mid + 1
        start = end - timedelta(days=best)
        return start, end, {
            "lookback_days": best,
            "ui_total": cache.get(best),
            "probes": cache,
        }

    lo, hi = 0, lookback_days
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        mid_n = await count_days(mid)
        if mid_n is None or mid_n > csv_cap:
            hi = mid - 1
        else:
            best = mid
            lo = mid + 1
    start = end - timedelta(days=best)
    return start, end, {
        "lookback_days": best,
        "ui_total": cache.get(best),
        "probes": cache,
    }


async def pull_claims(args: argparse.Namespace) -> dict:
    setup_logging(level=args.log_level)
    probe_path = Path(args.probe)
    probe = load_probe_report(probe_path) if probe_path.is_file() else {}
    csv_cap = int(probe.get("csv_row_cap") or CSV_CAP)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    strategy = args.strategy or probe.get("strategy") or DATE_WINDOW_FALLBACK
    recent = bool(getattr(args, "recent", False))

    config = WaystarConfig.from_env(headless=not args.headed)
    human = HumanSettings(
        action_delay_min=config.action_delay_min,
        action_delay_max=config.action_delay_max,
    )
    trans_from = args.trans_from
    trans_to = args.trans_to
    summary: dict = {
        "strategy": strategy,
        "download_url": _download_url(config.app_id),
        "trans_from": trans_from,
        "trans_to": trans_to,
        "recent": recent,
        "csv_cap": csv_cap,
        "files": [],
        "ui_total": probe.get("ui_total"),
    }

    async with async_playwright() as playwright:
        browser, context = await create_context(
            playwright, config, reuse_session=not args.fresh_login
        )
        try:
            page = await ensure_authenticated(context, config, human)
            await human_pause(human)
            cust_id = await resolve_cust_id(page, config)
            token = await refresh_verification_token(page, human)
            months_back = getattr(args, "months_back", None)
            since = getattr(args, "since", None)
            if recent and since:
                start = date.fromisoformat(since)
                end = datetime.now(ZoneInfo("Africa/Cairo")).date()
                trans_from = format_mdy(start)
                trans_to = format_mdy(end)
                summary["trans_from"] = trans_from
                summary["trans_to"] = trans_to
                summary["recent_meta"] = {
                    "since": str(start),
                    "lookback_days": (end - start).days,
                    "start": str(start),
                    "end": str(end),
                }
                log.info("recent since=%s window %s .. %s", since, start, end)
            elif recent and months_back is not None:
                today = datetime.now(ZoneInfo("Africa/Cairo")).date()
                start, end = months_back_window(today, int(months_back))
                trans_from = format_mdy(start)
                trans_to = format_mdy(end)
                summary["trans_from"] = trans_from
                summary["trans_to"] = trans_to
                summary["recent_meta"] = {
                    "months_back": int(months_back),
                    "lookback_days": (end - start).days,
                    "start": str(start),
                    "end": str(end),
                }
                log.info(
                    "recent months-back=%s window %s .. %s",
                    months_back,
                    start,
                    end,
                )
            elif recent:
                start, end, recent_meta = await _resolve_recent_window(
                    page,
                    token=token,
                    cust_id=cust_id,
                    app_id=config.app_id,
                    status=args.status,
                    csv_cap=csv_cap,
                    lookback_days=int(getattr(args, "lookback_days", RECENT_LOOKBACK_DAYS)),
                    human=human,
                )
                trans_from = format_mdy(start)
                trans_to = format_mdy(end)
                summary["trans_from"] = trans_from
                summary["trans_to"] = trans_to
                summary["ui_total"] = recent_meta.get("ui_total")
                summary["recent_meta"] = {
                    "lookback_days": recent_meta.get("lookback_days"),
                    "ui_total": recent_meta.get("ui_total"),
                    "probes": {str(k): v for k, v in (recent_meta.get("probes") or {}).items()},
                }
            else:
                start = datetime.strptime(trans_from, "%m/%d/%Y").date()
                end = datetime.strptime(trans_to, "%m/%d/%Y").date()
            batches: list[list[dict]] = []
            await _pull_windows(
                page,
                token=token,
                cust_id=cust_id,
                app_id=config.app_id,
                status=args.status,
                start=start,
                end=end,
                csv_cap=csv_cap,
                out_dir=out_dir,
                human=human,
                batches=batches,
                files=summary["files"],
                max_parts=args.max_parts,
                reuse_existing=not recent,
            )
            merged = merge_claim_rows(batches)
            merged_path = out_dir / "claims_merged.json"
            merged_path.write_text(json.dumps(merged, indent=2, default=str), encoding="utf-8")
            summary["merged_rows"] = len(merged)
            summary["merged_json"] = str(merged_path)
            if recent:
                recent_path = out_dir / "claims_recent.json"
                recent_path.write_text(json.dumps(merged, indent=2, default=str), encoding="utf-8")
                summary["recent_json"] = str(recent_path)
            summary["partial"] = bool(args.max_parts)
            if args.max_parts:
                summary["completeness"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "max_parts truncated pull",
                    "pulled_rows": len(merged),
                }
            else:
                expected = summary.get("ui_total")
                if not expected:
                    overall = await _search(
                        page,
                        token,
                        cust_id,
                        config.app_id,
                        _criteria(trans_from, trans_to, args.status),
                    )
                    expected = overall.get("total_results")
                    summary["ui_total"] = expected
                gate = completeness_gate(merged, expected, max_missing=args.max_missing)
                summary["completeness"] = gate
            await save_storage_state(context)
        finally:
            await context.close()
            await browser.close()

    write_probe_report(out_dir / "pull_summary.json", summary)
    return summary


async def _pull_windows(
    page,
    *,
    token: str,
    cust_id: str,
    app_id: str,
    status: str,
    start: date,
    end: date,
    csv_cap: int,
    out_dir: Path,
    human: HumanSettings,
    batches: list,
    files: list,
    max_parts: int | None,
    reuse_existing: bool = True,
) -> None:
    """Pull [start, end] in windows under the CSV cap.

    ``reuse_existing`` re-parses a window CSV already on disk instead of downloading
    it (resume for one-off bulk pulls). Nightly pulls must download every window:
    window names repeat across days, so a reused file carries old remit amounts.
    """
    stack = [(start, end)]
    searches = 0
    while stack:
        if max_parts and len(files) >= max_parts:
            return
        win_from, win_to = stack.pop()
        criteria = _criteria(format_mdy(win_from), format_mdy(win_to), status)
        if searches and searches % 8 == 0:
            await extend_session(page)
            token = await refresh_verification_token(page, human)
        parsed = await _search(page, token, cust_id, app_id, criteria)
        searches += 1
        ui_total = parsed.get("total_results")
        log.info("window %s..%s ui_total=%s", win_from, win_to, ui_total)
        over_cap = (ui_total or 0) > csv_cap or (ui_total is None and win_from < win_to)
        if over_cap and win_from < win_to:
            left, right = bisect_date_windows(win_from, win_to)
            if left[1] >= left[0] and right[1] >= right[0] and left != (win_from, win_to):
                stack.append(right)
                stack.append(left)
                await human_pause(human, 1.0)
                continue
        dest = out_dir / (
            f"claims_{format_mdy(win_from).replace('/', '')}_"
            f"{format_mdy(win_to).replace('/', '')}.csv"
        )
        if reuse_existing and dest.is_file() and dest.stat().st_size > 1000:
            rows = parse_claims_screen_csv(dest)
            log.info("resume skip existing %s (%s rows)", dest.name, len(rows))
        else:
            rows = await _get_csv(page, app_id, dest)
        if len(rows) >= csv_cap and win_from < win_to:
            log.warning(
                "CSV still capped at %s for %s..%s — splitting",
                len(rows),
                win_from,
                win_to,
            )
            left, right = bisect_date_windows(win_from, win_to)
            stack.append(right)
            stack.append(left)
            continue
        batches.append(rows)
        files.append(
            {
                "from": str(win_from),
                "to": str(win_to),
                "ui_total": ui_total,
                "rows": len(rows),
                "path": str(dest),
            }
        )
        await human_pause(human, 2.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trans-from", default="01/01/2026")
    parser.add_argument("--trans-to", default="08/19/2026")
    parser.add_argument(
        "--recent",
        action="store_true",
        help="Pull recent claims (self-sized window, or --months-back calendar span)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=RECENT_LOOKBACK_DAYS,
        help="Initial lookback for --recent without --months-back (default 60)",
    )
    parser.add_argument(
        "--months-back",
        type=int,
        default=None,
        metavar="N",
        help=(
            "With --recent: pull from the 1st of (current month minus N) through today. "
            f"Splits automatically under the {CSV_CAP} CSV cap. "
            f"(ops default {DEFAULT_RECENT_MONTHS_BACK} = current + 2 prior months)"
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "With --recent: pull Trans Date from this day through today (Africa/Cairo). "
            "Takes precedence over --months-back."
        ),
    )
    parser.add_argument("--status", default="-1")
    parser.add_argument("--probe", default=str(OUTPUT_DIR / "probe_csv_export.json"))
    parser.add_argument("--out-dir", default=str(OUTPUT_DIR / "claims_listing_2026"))
    parser.add_argument("--strategy", default=DATE_WINDOW_FALLBACK)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--fresh-login", action="store_true")
    parser.add_argument("--max-parts", type=int, default=None)
    parser.add_argument("--max-missing", type=int, default=50)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(pull_claims(args))
    except CompletenessError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("completeness", {}).get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
