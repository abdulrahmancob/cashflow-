import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from playwright.async_api import Error as PlaywrightError

from config import DEFAULT_NETWORK_RETRY_ATTEMPTS, DEFAULT_NETWORK_RETRY_DELAY_SEC
from logging_config import get_logger

log = get_logger("network")

T = TypeVar("T")

RATE_LIMIT_STATUSES = {403, 429, 503}
DEFAULT_RATE_LIMIT_BASE_DELAY_SEC = 15.0

TRANSIENT_MARKERS = (
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ECONNRESET",
    "ETIMEDOUT",
    "net::ERR_",
)


@dataclass
class RetrySettings:
    max_attempts: int = DEFAULT_NETWORK_RETRY_ATTEMPTS
    base_delay_sec: float = DEFAULT_NETWORK_RETRY_DELAY_SEC


def is_transient_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_MARKERS)


class RateLimitError(Exception):
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}")


def is_rate_limited_status(status: int) -> bool:
    return status in RATE_LIMIT_STATUSES


async def retry_rate_limited(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    label: str,
    max_attempts: int = 3,
    base_delay_sec: float = DEFAULT_RATE_LIMIT_BASE_DELAY_SEC,
) -> T:
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except RateLimitError as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            delay = base_delay_sec * (2 ** (attempt - 1)) + random.uniform(0, 5)
            log.warning(
                "%s rate-limited (%s) attempt %s/%s — retrying in %.0fs",
                label,
                exc.status,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label} failed with no exception recorded")


async def retry_transient(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    label: str,
    max_attempts: int = DEFAULT_NETWORK_RETRY_ATTEMPTS,
    base_delay_sec: float = DEFAULT_NETWORK_RETRY_DELAY_SEC,
) -> T:
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except (PlaywrightError, asyncio.TimeoutError, OSError) as exc:
            last_exc = exc
            if not is_transient_error(exc) or attempt >= max_attempts:
                raise
            delay = base_delay_sec * (2 ** (attempt - 1))
            log.warning(
                "%s failed (attempt %s/%s): %s — retrying in %.0fs",
                label,
                attempt,
                max_attempts,
                str(exc).split("\n", maxsplit=1)[0],
                delay,
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label} failed with no exception recorded")
