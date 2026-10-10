"""Official Waystar Claims Screen CSV parsing + completeness (no live login)."""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[2] / "waystar_scraper"
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from claims_export import (  # noqa: E402
    CompletenessError,
    bisect_date_windows,
    choose_download_strategy,
    choose_recent_window,
    completeness_gate,
    compact_check_ref,
    months_back_window,
    parse_claims_screen_csv,
    parse_remit_numbers,
    strip_pv4,
    unwrap_excel_formula,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "waystar_claims_screen.csv"


def test_unwrap_and_strip_pv4() -> None:
    assert unwrap_excel_formula('="PV410582026"') == "PV410582026"
    assert strip_pv4('="PV410582026"') == "10582026"
    assert strip_pv4("pv410582026") == "10582026"
    assert strip_pv4("010582026") == "10582026"


def test_parse_remit_numbers() -> None:
    assert parse_remit_numbers("111222333") == ["111222333"]
    assert parse_remit_numbers("AAA; BBB | CCC") == ["AAA", "BBB", "CCC"]
    assert parse_remit_numbers("") == []


def test_parse_official_csv_fixture() -> None:
    rows = parse_claims_screen_csv(FIXTURE)
    assert len(rows) == 2
    first = rows[0]
    assert first["instance_id"] == "12690343133"
    assert first["claim_number"] == "PV410582026"
    assert first["claim_key"] == "10582026"
    assert first["from_date"] == date(2026, 8, 14)
    assert first["total_remit_amount"] == 180.5
    assert first["remit_numbers"] == ["111222333"]
    second = rows[1]
    assert second["claim_key"] == "10669938"
    assert second["total_remit_amount"] is None
    assert second["from_date"] == date(2026, 8, 17)


def test_completeness_gate() -> None:
    rows = parse_claims_screen_csv(FIXTURE)
    report = completeness_gate(rows, expected_total=2)
    assert report["ok"] is True
    try:
        completeness_gate(rows, expected_total=10)
        raise AssertionError("expected CompletenessError")
    except CompletenessError:
        pass


def test_choose_strategy_prefers_offset_not_date_split() -> None:
    assert choose_download_strategy({"offset_works": True, "csv_row_cap": 10000, "ui_total": 167498}) == "offset_csv"
    assert choose_download_strategy({"full_csv_under_cap": True, "ui_total": 100}) == "single_csv"
    assert (
        choose_download_strategy({"csv_row_cap": 10000, "ui_total": 167498, "offset_works": False})
        == "date_windows"
    )


def test_bisect_date_windows() -> None:
    left, right = bisect_date_windows(date(2026, 1, 1), date(2026, 8, 19))
    assert left[0] == date(2026, 1, 1)
    assert right[1] == date(2026, 8, 19)
    assert left[1] < right[0]


def test_compact_check_ref() -> None:
    assert compact_check_ref("1234-5678") == "12345678"
    assert compact_check_ref("00123") == "123"


def test_choose_recent_window_shrinks_when_over_cap() -> None:
    end = date(2026, 8, 24)

    def count_fn(start: date, _end: date) -> int:
        days = (end - start).days
        return days * 200

    start, resolved = choose_recent_window(
        end, csv_cap=10_000, lookback_days=60, count_fn=count_fn
    )
    assert resolved == end
    # 60 * 200 = 12000 over cap; 50 * 200 = 10000 exactly
    assert (end - start).days == 50


def test_choose_recent_window_expands_when_under_cap() -> None:
    end = date(2026, 8, 24)

    def count_fn(start: date, _end: date) -> int:
        days = (end - start).days
        return days * 100

    start, resolved = choose_recent_window(
        end,
        csv_cap=10_000,
        lookback_days=60,
        max_lookback_days=200,
        count_fn=count_fn,
    )
    assert resolved == end
    assert (end - start).days == 100


def test_choose_recent_window_without_counts() -> None:
    end = date(2026, 8, 24)
    start, resolved = choose_recent_window(end, lookback_days=60)
    assert resolved == end
    assert start == date(2026, 6, 25)


def test_months_back_window_current_plus_two_prior() -> None:
    start, end = months_back_window(date(2026, 8, 24), 2)
    assert start == date(2026, 6, 1)
    assert end == date(2026, 8, 24)


def test_months_back_window_year_wrap() -> None:
    start, end = months_back_window(date(2026, 1, 15), 2)
    assert start == date(2025, 11, 1)
    assert end == date(2026, 1, 15)


def test_months_back_window_zero_is_month_start() -> None:
    start, end = months_back_window(date(2026, 8, 24), 0)
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 24)


def test_pull_windows_downloads_fresh_when_reuse_disabled(tmp_path, monkeypatch) -> None:
    pytest.importorskip("playwright")
    import download_claims_listing as dcl
    from human import HumanSettings

    day = date(2026, 3, 2)
    dest = tmp_path / "claims_03022026_03022026.csv"
    dest.write_text("x" * 2000, encoding="utf-8")
    downloads: list[Path] = []

    async def fake_search(*_args, **_kwargs):
        return {"total_results": 1}

    async def fake_get_csv(_page, _app_id, path):
        downloads.append(path)
        return [{"instance_id": "fresh"}]

    async def no_pause(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dcl, "_search", fake_search)
    monkeypatch.setattr(dcl, "_get_csv", fake_get_csv)
    monkeypatch.setattr(dcl, "human_pause", no_pause)
    monkeypatch.setattr(dcl, "parse_claims_screen_csv", lambda _path: [{"instance_id": "stale"}])

    def pull(reuse_existing: bool) -> list:
        batches: list = []
        asyncio.run(
            dcl._pull_windows(
                None,
                token="t",
                cust_id="c",
                app_id="1",
                status="-1",
                start=day,
                end=day,
                csv_cap=10_000,
                out_dir=tmp_path,
                human=HumanSettings(),
                batches=batches,
                files=[],
                max_parts=None,
                reuse_existing=reuse_existing,
            )
        )
        return batches

    assert pull(True) == [[{"instance_id": "stale"}]]
    assert downloads == []
    assert pull(False) == [[{"instance_id": "fresh"}]]
    assert downloads == [dest]
