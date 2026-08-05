"""Structured JSONL logging, metrics, and heartbeat/stall monitoring."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def correlation_id(
    *,
    emr_id: str = "",
    dos: str = "",
    facility_id: str = "",
    sf_id_or_hash: str = "",
) -> str:
    return f"{emr_id}|{dos}|{facility_id}|{sf_id_or_hash}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
    except Exception:
        try:
            # Windows-friendly fallback via resource unavailable; use ctypes-less approx
            return None
        except Exception:
            return None


@dataclass
class MetricsSink:
    stage: str
    counters: dict[str, float] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)
    by_error_type: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def incr(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0.0) + amount

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            self.latencies_ms.append(ms)

    def incr_error(self, error_type: str) -> None:
        with self._lock:
            self.by_error_type[error_type] = self.by_error_type.get(error_type, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lats = list(self.latencies_ms)
            counters = dict(self.counters)
            errors = dict(self.by_error_type)
        avg = sum(lats) / len(lats) if lats else 0.0
        p95 = 0.0
        if lats:
            ordered = sorted(lats)
            p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))]
        return {
            "stage": self.stage,
            "counters": counters,
            "avg_latency_ms": round(avg, 2),
            "p95_latency_ms": round(p95, 2),
            "latency_samples": len(lats),
            "by_error_type": errors,
        }

    def write(self, path: Path) -> None:
        _atomic_write_json(path, self.snapshot())


class ObsContext:
    """Run-scoped observability: JSONL logs, metrics, heartbeat, stall watchdog."""

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        script: str,
        stage: str = "init",
        heartbeat_interval_s: float = 30.0,
        stall_seconds: float = 120.0,
        stall_abort_seconds: float = 600.0,
        online: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.script = script
        self.stage = stage
        self.batch_id = ""
        self.worker_id = ""
        self.online = online
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stall_seconds = stall_seconds
        self.stall_abort_seconds = stall_abort_seconds

        self.logs_dir = self.run_dir / "logs"
        self.metrics_dir = self.run_dir / "metrics"
        self.monitoring_dir = self.run_dir / "monitoring"
        self.summaries_dir = self.run_dir / "summaries"
        self.errors_dir = self.run_dir / "errors"
        for d in (
            self.logs_dir,
            self.metrics_dir,
            self.monitoring_dir,
            self.summaries_dir,
            self.errors_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.metrics = MetricsSink(stage=stage)
        self._lock = threading.Lock()
        self._last_success: dict[str, Any] = {}
        self._last_unit_success_mono = time.monotonic()
        self._last_checkpoint_ts = ""
        self._completed_units = 0
        self._remaining_units = 0
        self._workers_total = 0
        self._workers_busy = 0
        self._auth_healthy = True
        self._browser_healthy = True
        self._stall_emitted = False
        self._stop = threading.Event()
        self._abort_requested = False
        self._hb_thread: threading.Thread | None = None
        self._stage_started_mono = time.monotonic()
        self._success_timestamps: list[float] = []

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.metrics.stage = stage
        self._stage_started_mono = time.monotonic()
        self._stall_emitted = False
        self._last_unit_success_mono = time.monotonic()

    def set_batch(self, batch_id: str) -> None:
        self.batch_id = batch_id

    def set_progress(self, *, completed: int, remaining: int) -> None:
        with self._lock:
            self._completed_units = completed
            self._remaining_units = remaining

    def set_workers(self, *, total: int, busy: int) -> None:
        with self._lock:
            self._workers_total = total
            self._workers_busy = busy

    def set_auth_healthy(self, healthy: bool) -> None:
        self._auth_healthy = healthy
        self.emit(
            "auth_health",
            level="INFO" if healthy else "ERROR",
            outcome="success" if healthy else "fail",
            error_type=None if healthy else "AuthExpired",
            error_expected=not healthy,
        )

    def set_browser_healthy(self, healthy: bool) -> None:
        self._browser_healthy = healthy
        self.emit(
            "browser_health",
            level="INFO" if healthy else "ERROR",
            outcome="success" if healthy else "fail",
            error_type=None if healthy else "Unexpected",
            error_expected=False,
        )

    def mark_checkpoint(self) -> None:
        self._last_checkpoint_ts = utc_now_iso()

    def mark_success(self, **fields: Any) -> None:
        now = utc_now_iso()
        payload = {"ts": now, **{k: v for k, v in fields.items() if v is not None}}
        with self._lock:
            self._last_success = payload
            self._last_unit_success_mono = time.monotonic()
            self._success_timestamps.append(time.monotonic())
            # keep recent window for throughput
            cutoff = time.monotonic() - 600
            self._success_timestamps = [t for t in self._success_timestamps if t >= cutoff]
            self._stall_emitted = False

    def abort_requested(self) -> bool:
        return self._abort_requested

    def start_heartbeat(self) -> None:
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="obs-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=5)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            self._write_heartbeat()
            self._check_stall()

    def _throughput_per_min(self) -> float:
        with self._lock:
            stamps = list(self._success_timestamps)
        if len(stamps) < 2:
            return 0.0
        span = max(stamps[-1] - stamps[0], 1e-6)
        return round((len(stamps) - 1) * 60.0 / span, 3)

    def _write_heartbeat(self) -> None:
        with self._lock:
            completed = self._completed_units
            remaining = self._remaining_units
            last_success = dict(self._last_success)
            workers_total = self._workers_total
            workers_busy = self._workers_busy
            stalled = 1 if self._stall_emitted else 0
        tpm = self._throughput_per_min()
        eta = round(remaining / tpm, 1) if tpm > 0 else None
        snap = self.metrics.snapshot()
        attempted = snap["counters"].get("attempted", 0) or 0
        failed = snap["counters"].get("failed", 0) or 0
        skipped = snap["counters"].get("skipped", 0) or 0
        retries = snap["counters"].get("retries", 0) or 0
        payload = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "stage": self.stage,
            "batch_id": self.batch_id,
            "completed_units": completed,
            "remaining_units": remaining,
            "throughput_units_per_min": tpm,
            "avg_latency_ms": snap["avg_latency_ms"],
            "eta_minutes": eta,
            "retry_rate": round(retries / attempted, 4) if attempted else 0.0,
            "failure_rate": round(failed / attempted, 4) if attempted else 0.0,
            "skip_rate": round(skipped / attempted, 4) if attempted else 0.0,
            "workers_total": workers_total,
            "workers_busy": workers_busy,
            "workers_stalled": stalled,
            "auth_healthy": self._auth_healthy,
            "browser_healthy": self._browser_healthy,
            "last_success": last_success,
            "last_checkpoint_ts": self._last_checkpoint_ts,
            "rss_mb": _rss_mb(),
            "online": self.online,
        }
        path = self.monitoring_dir / "heartbeat.json"
        _atomic_write_json(path, payload)
        self._append_jsonl(self.logs_dir / "heartbeat.jsonl", {"event": "heartbeat", **payload})

    def _check_stall(self) -> None:
        if not self.online:
            return
        idle = time.monotonic() - self._last_unit_success_mono
        if idle >= self.stall_seconds and not self._stall_emitted:
            self._stall_emitted = True
            with self._lock:
                last = dict(self._last_success)
            self.emit(
                "stall",
                level="ERROR",
                outcome="fail",
                error_type="Unexpected",
                error_expected=False,
                decision="stall_detected",
                decision_reason="no_unit_success_within_stall_seconds",
                extra={"idle_seconds": round(idle, 1), "last_success": last},
            )
        if idle >= self.stall_abort_seconds:
            self._abort_requested = True
            self.emit(
                "stall",
                level="ERROR",
                outcome="fail",
                error_type="Unexpected",
                error_expected=False,
                decision="stall_abort",
                decision_reason="idle_exceeded_stall_abort_seconds",
                extra={"idle_seconds": round(idle, 1)},
            )

    def _append_jsonl(self, path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def emit(
        self,
        event: str,
        *,
        level: str = "INFO",
        operation: str = "",
        correlation_id: str | None = None,
        outcome: str | None = None,
        decision: str | None = None,
        decision_reason: str | None = None,
        error_type: str | None = None,
        error_expected: bool | None = None,
        exception: BaseException | str | None = None,
        execution_ms: float | None = None,
        unit_state_from: str | None = None,
        unit_state_to: str | None = None,
        retry_count: int | None = None,
        patient_name: str | None = None,
        webpt_patient_id: str | None = None,
        emr_id: str | None = None,
        dos: str | None = None,
        facility_id: str | None = None,
        facility_name: str | None = None,
        visit_status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        exc_text = None
        stack = None
        if isinstance(exception, BaseException):
            exc_text = f"{type(exception).__name__}: {exception}"
            stack = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
        elif isinstance(exception, str):
            exc_text = exception

        payload: dict[str, Any] = {
            "ts": utc_now_iso(),
            "level": level,
            "event": event,
            "run_id": self.run_id,
            "correlation_id": correlation_id,
            "stage": self.stage,
            "script": self.script,
            "operation": operation,
            "batch_id": self.batch_id or None,
            "worker_id": self.worker_id or None,
            "facility_id": facility_id,
            "facility_name": facility_name,
            "patient_name": patient_name,
            "webpt_patient_id": webpt_patient_id,
            "emr_id": emr_id,
            "dos": dos,
            "visit_status": visit_status,
            "retry_count": retry_count,
            "execution_ms": execution_ms,
            "outcome": outcome,
            "decision": decision,
            "decision_reason": decision_reason,
            "error_type": error_type,
            "error_expected": error_expected,
            "exception": exc_text,
            "stack_trace": stack,
            "unit_state_from": unit_state_from,
            "unit_state_to": unit_state_to,
            "extra": extra or {},
        }
        stage_log = self.logs_dir / f"{self.stage or 'run'}.jsonl"
        self._append_jsonl(stage_log, payload)
        if level in {"ERROR", "WARN"} and error_type:
            self.metrics.incr_error(error_type)
        if level == "ERROR" and (error_expected is False or error_expected is None):
            # collect unexpected for errors.json later
            self.metrics.incr("unexpected_errors")

        # human one-liner for ERROR
        if level == "ERROR":
            print(
                f"[ERROR] {self.stage}/{operation or event} "
                f"corr={correlation_id} type={error_type} "
                f"reason={decision_reason or ''} {exc_text or ''}",
                flush=True,
            )

    def stage_start(self, stage: str, **extra: Any) -> None:
        self.set_stage(stage)
        self.emit("stage_start", operation="stage_start", extra=extra)

    def stage_end(self, stage: str | None = None, **extra: Any) -> Path:
        st = stage or self.stage
        snap = self.metrics.snapshot()
        snap["extra"] = extra
        snap["duration_s"] = round(time.monotonic() - self._stage_started_mono, 2)
        out = self.metrics_dir / f"{st}_metrics.json"
        _atomic_write_json(out, snap)
        summary = {
            "run_id": self.run_id,
            "stage": st,
            "ts": utc_now_iso(),
            "metrics": snap,
            **extra,
        }
        # Prefix stage_ to avoid colliding with pilot_P*_summary.json on
        # case-insensitive filesystems (Windows).
        summary_path = self.summaries_dir / f"stage_{st}_summary.json"
        _atomic_write_json(summary_path, summary)
        self.emit("stage_end", operation="stage_end", extra={"summary_path": str(summary_path)})
        return summary_path

    def write_errors_rollup(self) -> Path:
        errors: list[dict[str, Any]] = []
        for path in sorted(self.logs_dir.glob("*.jsonl")):
            if path.name == "heartbeat.jsonl":
                continue
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("level") == "ERROR":
                        errors.append(ev)
        errors.sort(key=lambda e: (bool(e.get("error_expected")), e.get("ts") or ""))
        out = self.errors_dir / "errors.json"
        # also root alias
        _atomic_write_json(out, {"run_id": self.run_id, "count": len(errors), "errors": errors})
        _atomic_write_json(self.run_dir / "errors.json", {"run_id": self.run_id, "count": len(errors), "errors": errors})
        return out

    def write_retry_summary(self) -> Path:
        snap = self.metrics.snapshot()
        payload = {
            "run_id": self.run_id,
            "stage": self.stage,
            "retries": snap["counters"].get("retries", 0),
            "by_error_type": snap["by_error_type"],
        }
        path = self.summaries_dir / "retry_summary.json"
        _atomic_write_json(path, payload)
        _atomic_write_json(self.run_dir / "retry_summary.json", payload)
        return path


# Optional global for scraper instrumentation without plumbing every call.
_GLOBAL_OBS: ObsContext | None = None
_GLOBAL_LOCK = threading.Lock()


def set_global_obs(obs: ObsContext | None) -> None:
    global _GLOBAL_OBS
    with _GLOBAL_LOCK:
        _GLOBAL_OBS = obs


def get_global_obs() -> ObsContext | None:
    with _GLOBAL_LOCK:
        return _GLOBAL_OBS
