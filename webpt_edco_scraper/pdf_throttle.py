"""Global PDF download concurrency limit for parallel workers."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

_pdf_semaphore: asyncio.Semaphore | None = None


def set_pdf_semaphore(sem: asyncio.Semaphore | None) -> None:
    global _pdf_semaphore
    _pdf_semaphore = sem


@asynccontextmanager
async def pdf_download_slot():
    if _pdf_semaphore is None:
        yield
        return
    await _pdf_semaphore.acquire()
    try:
        yield
    finally:
        _pdf_semaphore.release()
