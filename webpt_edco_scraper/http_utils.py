_TRANSIENT_NETWORK_MARKERS = (
    "ECONNRESET",
    "ETIMEDOUT",
    "EPIPE",
    "ECONNABORTED",
    "TIMEOUT",
    "TIMED OUT",
    "socket hang up",
    "network",
    "connection reset",
    "connection closed",
)


def is_transient_network_error(exc: BaseException) -> bool:
    message = str(exc).upper()
    return any(marker.upper() in message for marker in _TRANSIENT_NETWORK_MARKERS)


def retry_delay_sec(attempt: int, *, base: float = 5.0) -> float:
    return base * (2**attempt)
