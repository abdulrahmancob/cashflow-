"""Observer-only Case drain forensics — correlation, timers, appenders, counters.

Never changes scheduling, concurrency, delays, or download behavior.
All emit paths are best-effort (never raise into the download path).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

# --- Correlation context -------------------------------------------------
# ContextVars for in-task phases; also mirror to _CURRENT so Playwright
# network callbacks (may run without context copy) still correlate.

_facility_id: ContextVar[str] = ContextVar("forensics_facility_id", default="")
_case_id: ContextVar[str] = ContextVar("forensics_case_id", default="")
_patient_id: ContextVar[str] = ContextVar("forensics_patient_id", default="")
_worker_id: ContextVar[str] = ContextVar("forensics_worker_id", default="")
_session_id: ContextVar[str] = ContextVar("forensics_session_id", default="")
_phase_id: ContextVar[str] = ContextVar("forensics_phase_id", default="")
_phase_name: ContextVar[str] = ContextVar("forensics_phase_name", default="")
_retry_n: ContextVar[int] = ContextVar("forensics_retry_n", default=0)

_CURRENT: dict[str, Any] = {
    "facility_id": "",
    "case_id": "",
    "patient_id": "",
    "worker_id": "",
    "session_id": "",
    "phase_id": "",
    "phase_name": "",
    "retry_n": 0,
}
_current_lock = threading.Lock()

_reports_dir: Path | None = None
_lock = threading.Lock()
_writer_lock = threading.Lock()

# PDF semaphore counters (process-wide)
_sem_in_flight = 0
_sem_peak = 0
_sem_wait_total = 0.0
_sem_hold_total = 0.0
_sem_acquires = 0
_sem_lock = threading.Lock()

# Pending HTTP request starts: request_id -> (t0, meta)
_http_pending: dict[str, dict[str, Any]] = {}
_http_pending_lock = threading.Lock()

# Session health counters
_session: dict[str, Any] = {
    "started_at": time.time(),
    "http_403": 0,
    "http_429": 0,
    "http_timeout": 0,
    "reconnects": 0,
    "storage_refreshes": 0,
    "login_renewals": 0,
}

# Optional hook: (url, elapsed_sec, nbytes) -> None  (e.g. telemetry observe)
_http_finished_hook: Any = None

# App API endpoints used for Zero Duplicate analysis
APP_API_SUBSTRINGS = (
    "patientChart",
    "patientChartNote",
    "/graphql",
    "getDocuments",
    "GetDocuments",
    "clinicactions",
    "ClinicActions",
    "printPDF",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure(reports_dir: Path, *, worker_id: str = "", session_id: str = "") -> None:
    global _reports_dir
    _reports_dir = Path(reports_dir)
    _reports_dir.mkdir(parents=True, exist_ok=True)
    if worker_id:
        _worker_id.set(worker_id)
        with _current_lock:
            _CURRENT["worker_id"] = worker_id
    if session_id:
        _session_id.set(session_id)
        with _current_lock:
            _CURRENT["session_id"] = session_id
    _session["started_at"] = time.time()


def set_http_finished_hook(hook: Any) -> None:
    """Register observer callback invoked on each finished HTTP request."""
    global _http_finished_hook
    _http_finished_hook = hook


def is_app_api_url(url: str) -> bool:
    u = url or ""
    return any(s in u for s in APP_API_SUBSTRINGS)


def correlation() -> dict[str, Any]:
    # Prefer ContextVar; fall back to process-wide mirror for network callbacks
    with _current_lock:
        cur = dict(_CURRENT)
    return {
        "facility_id": _facility_id.get() or cur.get("facility_id", ""),
        "case_id": _case_id.get() or cur.get("case_id", ""),
        "patient_id": _patient_id.get() or cur.get("patient_id", ""),
        "worker_id": _worker_id.get() or cur.get("worker_id", ""),
        "session_id": _session_id.get() or cur.get("session_id", ""),
        "phase_id": _phase_id.get() or cur.get("phase_id", ""),
        "phase_name": _phase_name.get() or cur.get("phase_name", ""),
        "retry_n": _retry_n.get() if _retry_n.get() else cur.get("retry_n", 0),
    }


def bind_case(
    *,
    facility_id: str,
    case_id: str,
    patient_id: str = "",
    retry_n: int = 0,
) -> list[Any]:
    """Set case correlation; returns tokens for reset."""
    tokens = [
        _facility_id.set(str(facility_id)),
        _case_id.set(str(case_id)),
        _patient_id.set(str(patient_id or "")),
        _retry_n.set(int(retry_n)),
    ]
    with _current_lock:
        _CURRENT["facility_id"] = str(facility_id)
        _CURRENT["case_id"] = str(case_id)
        _CURRENT["patient_id"] = str(patient_id or "")
        _CURRENT["retry_n"] = int(retry_n)
    return tokens


def clear_phase() -> None:
    _phase_id.set("")
    _phase_name.set("")
    with _current_lock:
        _CURRENT["phase_id"] = ""
        _CURRENT["phase_name"] = ""


def set_retry_n(n: int) -> None:
    _retry_n.set(int(n))
    with _current_lock:
        _CURRENT["retry_n"] = int(n)


def note_storage_refresh() -> None:
    _session["storage_refreshes"] = int(_session.get("storage_refreshes", 0)) + 1


def note_login_renewal() -> None:
    _session["login_renewals"] = int(_session.get("login_renewals", 0)) + 1


def note_reconnect() -> None:
    _session["reconnects"] = int(_session.get("reconnects", 0)) + 1


def _append_jsonl(name: str, row: dict[str, Any]) -> None:
    if _reports_dir is None:
        return
    path = _reports_dir / name
    try:
        with _writer_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _append_csv_row(name: str, fieldnames: list[str], row: dict[str, Any]) -> None:
    if _reports_dir is None:
        return
    import csv

    path = _reports_dir / name
    try:
        with _writer_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                w.writerow({k: row.get(k, "") for k in fieldnames})
    except Exception:
        pass


# --- Timeline / phases ---------------------------------------------------


@dataclass
class TimelineEvent:
    event_id: str
    name: str
    start: float
    end: float = 0.0
    duration: float = 0.0
    parent_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class CaseTimeline:
    """Per-case timeline with DAG edges for critical-path analysis."""

    def __init__(self) -> None:
        self.case_start = time.perf_counter()
        self.events: list[TimelineEvent] = []
        self._open: dict[str, TimelineEvent] = {}
        self.pdf_rows: list[dict[str, Any]] = []
        self.browser: dict[str, Any] = {}
        self.idle: dict[str, float] = {}
        self.queue_latency_sec: float = 0.0
        self.queued_at: str = ""
        self.claimed_at: str = ""
        self.bytes_total: int = 0
        self.pdf_count: int = 0
        self.sem_wait_sec: float = 0.0
        self.sem_hold_sec: float = 0.0
        self.sem_peak: int = 0
        self.io_sec: dict[str, float] = {}

    def begin(
        self,
        name: str,
        *,
        parent_id: str = "",
        depends_on: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        eid = str(uuid.uuid4())
        ev = TimelineEvent(
            event_id=eid,
            name=name,
            start=time.perf_counter(),
            parent_id=parent_id,
            depends_on=list(depends_on or []),
            meta=dict(meta or {}),
        )
        self._open[eid] = ev
        _phase_id.set(eid)
        _phase_name.set(name)
        with _current_lock:
            _CURRENT["phase_id"] = eid
            _CURRENT["phase_name"] = name
        return eid

    def end(self, event_id: str, **meta: Any) -> float:
        ev = self._open.pop(event_id, None)
        if ev is None:
            return 0.0
        ev.end = time.perf_counter()
        ev.duration = max(0.0, ev.end - ev.start)
        if meta:
            ev.meta.update(meta)
        self.events.append(ev)
        if _phase_id.get() == event_id:
            clear_phase()
        return ev.duration

    @contextmanager
    def phase(
        self,
        name: str,
        *,
        parent_id: str = "",
        depends_on: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        eid = self.begin(name, parent_id=parent_id, depends_on=depends_on, meta=meta)
        try:
            yield eid
        finally:
            self.end(eid)

    def add_idle(self, reason: str, seconds: float) -> None:
        if seconds <= 0:
            return
        self.idle[reason] = self.idle.get(reason, 0.0) + float(seconds)

    def wall_sec(self) -> float:
        return max(0.0, time.perf_counter() - self.case_start)

    def emit(self, *, ok: bool = True, error_type: str = "") -> None:
        corr = correlation()
        wall = self.wall_sec()
        phases = {
            e.name: round(e.duration, 6)
            for e in self.events
            if e.name
            not in (
                # keep all
            )
        }
        # Prefer last occurrence sum by name
        phase_sums: dict[str, float] = {}
        for e in self.events:
            phase_sums[e.name] = phase_sums.get(e.name, 0.0) + e.duration
        row = {
            "at": _utc(),
            **corr,
            "ok": ok,
            "error_type": error_type,
            "wall_sec": round(wall, 6),
            "phases": {k: round(v, 6) for k, v in phase_sums.items()},
            "events": [
                {
                    "event_id": e.event_id,
                    "name": e.name,
                    "start_rel": round(e.start - self.case_start, 6),
                    "end_rel": round((e.end or e.start) - self.case_start, 6),
                    "duration": round(e.duration, 6),
                    "parent_id": e.parent_id,
                    "depends_on": e.depends_on,
                    "meta": e.meta,
                }
                for e in self.events
            ],
            "idle": {k: round(v, 6) for k, v in self.idle.items()},
            "queue_latency_sec": round(self.queue_latency_sec, 6),
            "queued_at": self.queued_at,
            "claimed_at": self.claimed_at,
            "pdf_count": self.pdf_count,
            "bytes_total": self.bytes_total,
            "sem_wait_sec": round(self.sem_wait_sec, 6),
            "sem_hold_sec": round(self.sem_hold_sec, 6),
            "sem_peak": self.sem_peak,
            "browser": self.browser,
            "io_sec": {k: round(v, 6) for k, v in self.io_sec.items()},
        }
        _append_jsonl("case_phases.jsonl", row)
        for e in self.events:
            _append_jsonl(
                "case_timeline.jsonl",
                {
                    "at": _utc(),
                    **corr,
                    "event_id": e.event_id,
                    "name": e.name,
                    "start": e.start,
                    "end": e.end,
                    "duration": round(e.duration, 6),
                    "parent_id": e.parent_id,
                    "depends_on": e.depends_on,
                    "meta": e.meta,
                    "wall_sec": round(wall, 6),
                },
            )
        if self.queued_at or self.queue_latency_sec:
            _append_csv_row(
                "queue_latency.csv",
                [
                    "facility_id",
                    "case_id",
                    "patient_id",
                    "queued_at",
                    "claimed_at",
                    "queue_latency_sec",
                ],
                {
                    **corr,
                    "queued_at": self.queued_at,
                    "claimed_at": self.claimed_at,
                    "queue_latency_sec": round(self.queue_latency_sec, 6),
                },
            )
        for pr in self.pdf_rows:
            _append_csv_row(
                "pdf_downloads.csv",
                [
                    "facility_id",
                    "case_id",
                    "patient_id",
                    "filename",
                    "pdf_type",
                    "size",
                    "elapsed_sec",
                    "retries",
                    "status",
                    "http_status",
                ],
                {**corr, **pr},
            )
        if self.browser:
            _append_csv_row(
                "browser_timing.csv",
                [
                    "facility_id",
                    "case_id",
                    "navigation_sec",
                    "dom_loaded_sec",
                    "network_idle_sec",
                    "page_ready_sec",
                    "click_latency_sec",
                    "case_verification_sec",
                ],
                {**corr, **self.browser},
            )


# --- IO spans ------------------------------------------------------------


@contextmanager
def io_span(kind: str, timeline: CaseTimeline | None = None) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - t0
        try:
            if timeline is not None:
                timeline.io_sec[kind] = timeline.io_sec.get(kind, 0.0) + dt
            _append_jsonl(
                "io_events.jsonl",
                {
                    "at": _utc(),
                    **correlation(),
                    "kind": kind,
                    "duration_sec": round(dt, 6),
                },
            )
        except Exception:
            pass


# --- PDF semaphore instrumentation --------------------------------------


@asynccontextmanager
async def instrumented_pdf_slot(timeline: CaseTimeline | None = None):
    """Wrap acquire/hold; call from pdf_throttle when forensics enabled."""
    global _sem_in_flight, _sem_peak, _sem_wait_total, _sem_hold_total, _sem_acquires
    from pdf_throttle import _pdf_semaphore  # type: ignore

    if _pdf_semaphore is None:
        yield
        return
    t_wait0 = time.perf_counter()
    await _pdf_semaphore.acquire()
    wait = time.perf_counter() - t_wait0
    with _sem_lock:
        _sem_in_flight += 1
        _sem_peak = max(_sem_peak, _sem_in_flight)
        _sem_wait_total += wait
        _sem_acquires += 1
        peak = _sem_peak
    if timeline is not None:
        timeline.sem_wait_sec += wait
        timeline.sem_peak = max(timeline.sem_peak, peak)
        timeline.add_idle("semaphore", wait)
    t_hold0 = time.perf_counter()
    try:
        yield
    finally:
        hold = time.perf_counter() - t_hold0
        with _sem_lock:
            _sem_in_flight = max(0, _sem_in_flight - 1)
            _sem_hold_total += hold
        if timeline is not None:
            timeline.sem_hold_sec += hold
        _pdf_semaphore.release()


def semaphore_snapshot() -> dict[str, Any]:
    with _sem_lock:
        return {
            "in_flight": _sem_in_flight,
            "peak_in_flight": _sem_peak,
            "wait_total_sec": round(_sem_wait_total, 6),
            "hold_total_sec": round(_sem_hold_total, 6),
            "acquires": _sem_acquires,
        }


# --- HTTP observer helpers -----------------------------------------------

_ID_RE = re.compile(r"/\d{4,}(?=/|$)")
_QUERY_ID_RE = re.compile(r"=\d+")


def normalize_endpoint(url: str) -> str:
    try:
        p = urlparse(url)
        path = _ID_RE.sub("/{id}", p.path or "")
        # Keep path only for aggregation
        return path or p.netloc or url[:120]
    except Exception:
        return (url or "")[:120]


def weak_fingerprint(status: int, nbytes: int, body_prefix: bytes | str = b"") -> str:
    h = hashlib.sha1()
    h.update(str(status).encode())
    h.update(b"|")
    h.update(str(nbytes).encode())
    h.update(b"|")
    if isinstance(body_prefix, str):
        body_prefix = body_prefix.encode("utf-8", errors="ignore")
    h.update(body_prefix[:256])
    return h.hexdigest()[:16]


def http_request_started(request_obj: Any) -> None:
    """Playwright request event handler."""
    try:
        rid = str(getattr(request_obj, "url", "")) + "|" + str(id(request_obj))
        corr = correlation()
        with _http_pending_lock:
            _http_pending[rid] = {
                "t0": time.perf_counter(),
                "ts": _utc(),
                "url": getattr(request_obj, "url", ""),
                "method": getattr(request_obj, "method", "GET"),
                **corr,
                "request_id": str(uuid.uuid4()),
            }
    except Exception:
        pass


def http_request_finished(response_obj: Any) -> None:
    try:
        req = response_obj.request
        rid = str(getattr(req, "url", "")) + "|" + str(id(req))
        with _http_pending_lock:
            meta = _http_pending.pop(rid, None)
        if meta is None:
            meta = {
                "t0": time.perf_counter(),
                "ts": _utc(),
                "url": getattr(req, "url", ""),
                "method": getattr(req, "method", "GET"),
                **correlation(),
                "request_id": str(uuid.uuid4()),
            }
        elapsed = time.perf_counter() - float(meta["t0"])
        status = int(getattr(response_obj, "status", 0) or 0)
        headers = {}
        try:
            headers = dict(response_obj.headers or {})
        except Exception:
            pass
        nbytes = 0
        cl = headers.get("content-length") or headers.get("Content-Length")
        if cl:
            try:
                nbytes = int(cl)
            except ValueError:
                pass
        url = meta.get("url") or ""
        endpoint = normalize_endpoint(url)
        fp = weak_fingerprint(status, nbytes)
        if status == 403:
            _session["http_403"] = int(_session.get("http_403", 0)) + 1
        elif status == 429:
            _session["http_429"] = int(_session.get("http_429", 0)) + 1
        row = {
            "timestamp": meta.get("ts") or _utc(),
            "facility_id": meta.get("facility_id", ""),
            "case_id": meta.get("case_id", ""),
            "patient_id": meta.get("patient_id", ""),
            "worker_id": meta.get("worker_id", ""),
            "session_id": meta.get("session_id", ""),
            "phase_id": meta.get("phase_id", ""),
            "phase_name": meta.get("phase_name", ""),
            "request_id": meta.get("request_id", ""),
            "endpoint": endpoint,
            "url": url[:500],
            "method": meta.get("method", "GET"),
            "status": status,
            "elapsed_sec": round(elapsed, 6),
            "bytes": nbytes,
            "retry_n": meta.get("retry_n", 0),
            "exception": "",
            "fingerprint": fp,
            "is_app_api": is_app_api_url(url),
        }
        _append_jsonl("http_requests.jsonl", row)
        hook = _http_finished_hook
        if hook is not None:
            try:
                hook(url, elapsed_sec=elapsed, nbytes=nbytes)
            except Exception:
                pass
    except Exception:
        pass


def http_request_failed(request_obj: Any) -> None:
    try:
        rid = str(getattr(request_obj, "url", "")) + "|" + str(id(request_obj))
        with _http_pending_lock:
            meta = _http_pending.pop(rid, None)
        if meta is None:
            meta = {
                "t0": time.perf_counter(),
                "ts": _utc(),
                "url": getattr(request_obj, "url", ""),
                "method": getattr(request_obj, "method", "GET"),
                **correlation(),
                "request_id": str(uuid.uuid4()),
            }
        elapsed = time.perf_counter() - float(meta["t0"])
        fail = getattr(request_obj, "failure", None)
        exc = ""
        if callable(fail):
            try:
                exc = str(fail() or "")
            except Exception:
                exc = "failed"
        elif fail:
            exc = str(fail)
        if "timeout" in exc.lower() or "timed out" in exc.lower():
            _session["http_timeout"] = int(_session.get("http_timeout", 0)) + 1
        url = meta.get("url") or ""
        _append_jsonl(
            "http_requests.jsonl",
            {
                "timestamp": meta.get("ts") or _utc(),
                "facility_id": meta.get("facility_id", ""),
                "case_id": meta.get("case_id", ""),
                "patient_id": meta.get("patient_id", ""),
                "worker_id": meta.get("worker_id", ""),
                "session_id": meta.get("session_id", ""),
                "phase_id": meta.get("phase_id", ""),
                "phase_name": meta.get("phase_name", ""),
                "request_id": meta.get("request_id", ""),
                "endpoint": normalize_endpoint(url),
                "url": url[:500],
                "method": meta.get("method", "GET"),
                "status": 0,
                "elapsed_sec": round(elapsed, 6),
                "bytes": 0,
                "retry_n": meta.get("retry_n", 0),
                "exception": exc[:500],
                "fingerprint": "",
            },
        )
    except Exception:
        pass


def attach_http_observers(context: Any) -> None:
    """Attach Playwright network listeners (observer only)."""
    try:
        context.on("request", http_request_started)
        context.on("response", http_request_finished)
        context.on("requestfailed", http_request_failed)
    except Exception:
        pass


# --- Facility switch / retry lifecycle -----------------------------------

FACILITY_SWITCH_FIELDS = [
    "old_facility",
    "new_facility",
    "reason",
    "start",
    "end",
    "elapsed_sec",
]


def emit_facility_switch(
    *,
    old_facility: str,
    new_facility: str,
    reason: str,
    start: float,
    end: float,
) -> None:
    _append_csv_row(
        "facility_switch_history.csv",
        FACILITY_SWITCH_FIELDS,
        {
            "old_facility": old_facility,
            "new_facility": new_facility,
            "reason": reason,
            "start": _utc() if False else datetime.fromtimestamp(start, timezone.utc).isoformat()
            if start > 1e9
            else _utc(),
            "end": _utc(),
            "elapsed_sec": round(max(0.0, end - start), 6),
        },
    )


RETRY_FIELDS = [
    "facility_id",
    "case_id",
    "patient_id",
    "attempt",
    "failure_reason",
    "wait_sec",
    "outcome",
    "at",
]


def emit_retry_attempt(
    *,
    facility_id: str,
    case_id: str,
    patient_id: str,
    attempt: int,
    failure_reason: str,
    wait_sec: float = 0.0,
    outcome: str = "retry",
) -> None:
    _append_csv_row(
        "retry_lifecycle.csv",
        RETRY_FIELDS,
        {
            "facility_id": facility_id,
            "case_id": case_id,
            "patient_id": patient_id,
            "attempt": attempt,
            "failure_reason": failure_reason,
            "wait_sec": round(wait_sec, 6),
            "outcome": outcome,
            "at": _utc(),
        },
    )


# --- Resource / session sampling -----------------------------------------


def sample_resources(*, browser_pid: int | None = None, open_pages: int = 0) -> None:
    row: dict[str, Any] = {
        "at": _utc(),
        "open_pages": open_pages,
        "cpu_percent": -1,
        "ram_mb": -1,
        "browser_rss_mb": -1,
        "thread_count": -1,
        "open_files": -1,
        "sqlite_connections": 1,
    }
    try:
        import psutil  # type: ignore

        proc = psutil.Process()
        row["cpu_percent"] = proc.cpu_percent(interval=0.0)
        row["ram_mb"] = round(proc.memory_info().rss / (1024 * 1024), 2)
        row["thread_count"] = proc.num_threads()
        try:
            row["open_files"] = len(proc.open_files())
        except Exception:
            pass
        if browser_pid:
            try:
                bp = psutil.Process(browser_pid)
                row["browser_rss_mb"] = round(bp.memory_info().rss / (1024 * 1024), 2)
            except Exception:
                pass
    except Exception:
        pass
    _append_jsonl("resource_usage.jsonl", row)


def sample_session_health(*, cookie_count: int = 0) -> None:
    age = time.time() - float(_session.get("started_at", time.time()))
    _append_jsonl(
        "session_health.jsonl",
        {
            "at": _utc(),
            "session_age_sec": round(age, 1),
            "cookie_count": cookie_count,
            "http_403": _session.get("http_403", 0),
            "http_429": _session.get("http_429", 0),
            "http_timeout": _session.get("http_timeout", 0),
            "reconnects": _session.get("reconnects", 0),
            "storage_refreshes": _session.get("storage_refreshes", 0),
            "login_renewals": _session.get("login_renewals", 0),
            "session_id": _session_id.get(),
        },
    )


# --- Stats helpers (shared with analyzer / tests) ------------------------


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "count": 0,
            "avg": 0.0,
            "median": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }
    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "median": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def critical_path_sec(events: list[dict[str, Any]]) -> dict[str, float]:
    """Compute wall / critical path / parallelizable / idle from timeline events."""
    if not events:
        return {
            "wall_sec": 0.0,
            "critical_path_sec": 0.0,
            "parallelizable_sec": 0.0,
            "idle_sec": 0.0,
        }
    by_id = {e["event_id"]: e for e in events if e.get("event_id")}
    # longest path via DP on DAG (depends_on + parent serial)
    memo: dict[str, float] = {}

    def longest(eid: str) -> float:
        if eid in memo:
            return memo[eid]
        e = by_id.get(eid)
        if not e:
            return 0.0
        deps = list(e.get("depends_on") or [])
        if e.get("parent_id"):
            deps.append(e["parent_id"])
        pred = max((longest(d) for d in deps), default=0.0)
        # Sibling PDFs under same parent: critical path takes max, not sum
        memo[eid] = pred + float(e.get("duration") or 0.0)
        return memo[eid]

    # For parallel PDF children: critical path through pdf group = parent + max(child)
    pdf_by_parent: dict[str, list[float]] = {}
    for e in events:
        name = str(e.get("name") or "")
        if name.startswith("pdf_") or name == "pdf_job":
            pdf_by_parent.setdefault(str(e.get("parent_id") or ""), []).append(
                float(e.get("duration") or 0.0)
            )

    wall = 0.0
    for e in events:
        wall = max(wall, float(e.get("end_rel") or 0.0), float(e.get("start_rel") or 0.0) + float(e.get("duration") or 0.0))

    # Serial phases + max PDF wave
    serial_names = {
        "claim",
        "facility_acquire",
        "open_s1",
        "open_nav",
        "s1_verify",
        "discovery",
        "build_plan",
        "manifest",
        "fsm",
        "release",
        "inter_case_delay",
    }
    serial = sum(
        float(e.get("duration") or 0.0)
        for e in events
        if e.get("name") in serial_names
    )
    pdf_durs = [
        float(e.get("duration") or 0.0)
        for e in events
        if e.get("name") == "pdf_job"
    ]
    pdf_wave = [
        float(e.get("duration") or 0.0)
        for e in events
        if e.get("name") == "pdf_wave"
    ]
    max_pdf = max(pdf_durs) if pdf_durs else 0.0
    sum_pdf = sum(pdf_durs)
    wave = pdf_wave[0] if pdf_wave else max_pdf
    cpath = serial - (
        # if pdf_wave in serial_names we didn't include it; add max pdf
        0.0
    )
    # Recompute serial excluding pdf_wave if present
    serial_no_wave = sum(
        float(e.get("duration") or 0.0)
        for e in events
        if e.get("name") in serial_names and e.get("name") != "pdf_wave"
    )
    # Add discovery etc. already in serial_names; pdf critical = max
    cpath = serial_no_wave + max(wave, max_pdf)
    parallelizable = max(0.0, sum_pdf - max_pdf)
    idle = max(0.0, wall - cpath) if wall else 0.0
    # Prefer explicit wall from events
    if wall <= 0 and events:
        wall = cpath
    return {
        "wall_sec": wall,
        "critical_path_sec": cpath,
        "parallelizable_sec": parallelizable,
        "idle_sec": idle,
    }


MIN_CASES_FOR_RECS = 200
MIN_HTTP_FOR_RECS = 5000


def sample_gate_ok(n_cases: int, n_http: int) -> bool:
    return n_cases >= MIN_CASES_FOR_RECS and n_http >= MIN_HTTP_FOR_RECS
