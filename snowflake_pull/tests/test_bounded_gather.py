"""Unit tests for parallel in-case PDF helper (single browser)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
sys.path[:0] = [str(ROOT), str(SCRAPER)]

from case_download import bounded_gather  # noqa: E402


def test_bounded_gather_runs_all_factories() -> None:
    async def _run() -> None:
        order: list[int] = []

        async def make(i: int):
            await asyncio.sleep(0.01 * (3 - i))
            order.append(i)
            return i * 10

        results = await bounded_gather([lambda i=i: make(i) for i in range(3)])
        assert sorted(results) == [0, 10, 20]
        assert sorted(order) == [0, 1, 2]

    asyncio.run(_run())


def test_bounded_gather_empty() -> None:
    assert asyncio.run(bounded_gather([])) == []


def test_browser_workers_cli_rejects_multi(tmp_path: Path) -> None:
    """Worker CLI must refuse --browser-workers != 1."""
    script = ROOT / "snowflake_pull" / "scripts" / "run_case_download_worker.py"
    out = tmp_path / "out"
    out.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--out-dir",
            str(out),
            "--browser-workers",
            "2",
            "--dry-run",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    combined = (proc.stdout + proc.stderr).lower()
    assert "single-session" in combined or "browser-workers" in combined
