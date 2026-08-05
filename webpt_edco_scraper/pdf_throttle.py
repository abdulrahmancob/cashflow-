"""Global PDF download concurrency limit for parallel workers.

Observer-only: records semaphore wait/hold when snowflake_pull.case_forensics is available.
Does not change semaphore size or acquire semantics.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

_pdf_semaphore: asyncio.Semaphore | None = None

# Local counters (also mirrored in case_forensics when present)
_in_flight = 0
_peak = 0
_wait_total = 0.0
_hold_total = 0.0
_acquires = 0


def set_pdf_semaphore(sem: asyncio.Semaphore | None) -> None:
    global _pdf_semaphore
    _pdf_semaphore = sem


def semaphore_stats() -> dict:
    return {
        "in_flight": _in_flight,
        "peak_in_flight": _peak,
        "wait_total_sec": _wait_total,
        "hold_total_sec": _hold_total,
        "acquires": _acquires,
    }


@asynccontextmanager
async def pdf_download_slot():
    global _in_flight, _peak, _wait_total, _hold_total, _acquires
    if _pdf_semaphore is None:
        yield
        return
    t_wait0 = time.perf_counter()
    await _pdf_semaphore.acquire()
    wait = time.perf_counter() - t_wait0
    _wait_total += wait
    _acquires += 1
    _in_flight += 1
    _peak = max(_peak, _in_flight)
    try:
        from snowflake_pull import case_forensics as _f  # type: ignore

        _f._sem_wait_total += wait  # noqa: SLF001
        with _f._sem_lock:  # noqa: SLF001
            _f._sem_in_flight = _in_flight  # noqa: SLF001
            _f._sem_peak = max(_f._sem_peak, _peak)  # noqa: SLF001
            _f._sem_acquires += 1  # noqa: SLF001
    except Exception:
        pass
    t_hold0 = time.perf_counter()
    try:
        yield
    finally:
        hold = time.perf_counter() - t_hold0
        _hold_total += hold
        _in_flight = max(0, _in_flight - 1)
        try:
            from snowflake_pull import case_forensics as _f  # type: ignore

            _f._sem_hold_total += hold  # noqa: SLF001
            with _f._sem_lock:  # noqa: SLF001
                _f._sem_in_flight = _in_flight  # noqa: SLF001
        except Exception:
            pass
        _pdf_semaphore.release()
