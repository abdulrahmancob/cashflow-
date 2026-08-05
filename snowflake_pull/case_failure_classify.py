"""Closed-set failure classification for Case drain (P8)."""

from __future__ import annotations

from typing import Literal

FailureClass = Literal[
    "Network",
    "Timeout",
    "DownloadEmpty",
    "SocketError",
    "AuthenticationExpired",
    "CaseOpenFailed",
    "ManifestFailed",
    "CaseMismatch",
    "PDFMissing",
    "Unknown",
]

RECOVERABLE: frozenset[str] = frozenset(
    {
        "Network",
        "Timeout",
        "DownloadEmpty",
        "SocketError",
        "AuthenticationExpired",
        "CaseOpenFailed",  # often transient open/net; CaseMismatch is separate
        "PDFMissing",
        "Unknown",
    }
)

TERMINAL_ONLY: frozenset[str] = frozenset({"CaseMismatch", "ManifestFailed"})


def classify_failure(
    *,
    error_type: str = "",
    exc_msg: str = "",
) -> FailureClass:
    et = (error_type or "").strip()
    msg = (exc_msg or "").lower()
    if et == "CaseMismatch" or "casemismatch" in msg:
        return "CaseMismatch"
    if et == "DownloadEmpty" or "downloadempty" in msg:
        return "DownloadEmpty"
    if "403" in msg or "429" in msg or "blocked (403)" in msg:
        return "Network"
    if "socket hang" in msg or "connection reset" in msg or "econnreset" in msg:
        return "SocketError"
    if "timeout" in msg or "timed out" in msg:
        return "Timeout"
    if "auth" in msg or "session" in msg and "expired" in msg:
        return "AuthenticationExpired"
    if "manifest" in msg:
        return "ManifestFailed"
    if "pdf" in msg and ("missing" in msg or "not found" in msg):
        return "PDFMissing"
    if et == "CaseOpenFailed" or "caseopenfailed" in msg:
        return "CaseOpenFailed"
    if "net" in msg or "http" in msg or "ssl" in msg:
        return "Network"
    if et:
        return "Unknown"
    return "Unknown"


def is_recoverable(failure_class: str) -> bool:
    if failure_class in TERMINAL_ONLY:
        return False
    if failure_class == "CaseMismatch":
        return False
    return failure_class in RECOVERABLE


def next_retry_state(retry_count: int) -> str:
    """Map attempt count to retry queue state (1-based after first failure)."""
    if retry_count <= 0:
        return "retry_1"
    if retry_count == 1:
        return "retry_2"
    if retry_count == 2:
        return "retry_3"
    return "failed_terminal"
