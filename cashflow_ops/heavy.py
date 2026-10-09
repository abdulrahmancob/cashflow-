"""Caps on expensive downloads, so a few people cannot hold every worker thread.

At most three heavy requests run at once across the process, and one per person.
Extra requests get 429 straight away instead of queueing behind the others.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

from fastapi import Depends, HTTPException

from cashflow_ops.security import AuthUser, get_current_user

MAX_HEAVY = 3
BUSY_DETAIL = "The server is busy with other downloads. Try again in a minute."
OWN_DETAIL = "Your previous download is still being prepared. Wait for it to finish."

_slots = threading.BoundedSemaphore(MAX_HEAVY)
_lock = threading.Lock()
_running: set[str] = set()


def acquire(user_id: str) -> None:
    with _lock:
        if user_id in _running:
            raise HTTPException(status_code=429, detail=OWN_DETAIL)
        if not _slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail=BUSY_DETAIL)
        _running.add(user_id)


def release(user_id: str) -> None:
    with _lock:
        if user_id not in _running:
            return
        _running.discard(user_id)
        _slots.release()


def heavy_guard(user: AuthUser = Depends(get_current_user)) -> Iterator[None]:
    """Route dependency: hold one heavy slot while the handler builds its file."""
    acquire(user.user_id)
    try:
        yield
    finally:
        release(user.user_id)
