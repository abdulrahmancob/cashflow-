"""WebPT-aware adaptive rate control for long-running Case downloads.

Never bypasses throttling — detects 403/429/timeouts and reduces pressure.
Delay ladder (P5): 0 / 5 / 10 / 20 / 40 / 90 — recover gradually, no oscillation.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Literal

ThrottleState = Literal["healthy", "cooling", "throttled"]

WINDOW_SEC_DEFAULT = 15 * 60
HEALTHY_STREAK_SEC = 5 * 60
MIN_UP_PROBE_SEC = 5 * 60
PROBE_INTERVAL_SEC = 5 * 60
PDF_CONCURRENCY_FLOOR = 1
PDF_CONCURRENCY_CEILING = 8
DELAY_LADDER = (0.0, 5.0, 10.0, 20.0, 40.0, 90.0)
THROTTLE_SCORE_THROTTLED = 0.15
THROTTLE_SCORE_COOLING = 0.05
THROTTLE_SCORE_HEALTHY = 0.02
EDOC_STORM_CASES = 3
EDOC_COOLDOWN_SEC = 30 * 60


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ladder_index(delay: float) -> int:
    d = float(delay or 0.0)
    best = 0
    for i, step in enumerate(DELAY_LADDER):
        if d >= step - 1e-9:
            best = i
    return best


def snap_delay_to_ladder(delay: float) -> float:
    """Map any delay onto the nearest ladder step (never invent off-ladder values)."""
    d = max(0.0, float(delay or 0.0))
    best = DELAY_LADDER[0]
    best_dist = abs(d - best)
    for step in DELAY_LADDER:
        dist = abs(d - step)
        if dist < best_dist:
            best, best_dist = step, dist
    return best


def delay_for_pressure(score: float, state: ThrottleState) -> float:
    """Map throttle pressure to delay ladder step."""
    if state == "healthy" and score < THROTTLE_SCORE_HEALTHY:
        return DELAY_LADDER[0]
    if score >= 0.5 or (state == "throttled" and score >= 0.35):
        return DELAY_LADDER[5]  # 90 worst
    if score >= THROTTLE_SCORE_THROTTLED or state == "throttled":
        return DELAY_LADDER[4]  # 40 extreme
    if score >= 0.10:
        return DELAY_LADDER[3]  # 20 heavy
    if score >= THROTTLE_SCORE_COOLING or state == "cooling":
        return DELAY_LADDER[2]  # 10 more cooling
    return DELAY_LADDER[1]  # 5 cooling


@dataclass
class HttpEvent:
    ts: float
    status: int  # 200, 403, 429, 0=timeout/other
    elapsed_sec: float
    kind: str = "request"


@dataclass
class RateKnobs:
    pdf_concurrency: int = 4
    inter_case_delay_sec: float = 0.0
    edoc_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_concurrency": self.pdf_concurrency,
            "inter_case_delay_sec": self.inter_case_delay_sec,
            "edoc_enabled": self.edoc_enabled,
        }


@dataclass
class AdaptiveRateController:
    window_sec: float = WINDOW_SEC_DEFAULT
    events: Deque[HttpEvent] = field(default_factory=deque)
    knobs: RateKnobs = field(default_factory=RateKnobs)
    state: ThrottleState = "healthy"
    last_down_at: float = 0.0
    last_up_probe_at: float = 0.0
    healthy_since: float = field(default_factory=time.time)
    edoc_403_case_streak: int = 0
    edoc_deferred_until: float = 0.0
    auth_renewals: int = 0
    throttle_events_24h: int = 0
    _day_bucket_start: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        status: int,
        elapsed_sec: float = 0.0,
        *,
        kind: str = "request",
    ) -> None:
        now = time.time()
        self.events.append(
            HttpEvent(ts=now, status=int(status), elapsed_sec=float(elapsed_sec), kind=kind)
        )
        self._trim(now)
        bad = status in (403, 429) or status == 0
        if bad:
            self.throttle_events_24h += 1
            if kind == "edoc" and status == 403:
                self.edoc_403_case_streak += 1
        elif status == 200 and kind == "edoc":
            self.edoc_403_case_streak = 0
        prev_state = self.state
        self._recompute_state(now)
        entered_pressure = prev_state == "healthy" and self.state != "healthy"
        self._apply_pressure(now, force_down=bad or entered_pressure)

    def record_success(self, elapsed_sec: float = 0.0, *, kind: str = "request") -> None:
        self.record(200, elapsed_sec, kind=kind)

    def record_blocked(self, status: int = 403, *, kind: str = "request") -> None:
        self.record(status, 0.0, kind=kind)

    def record_timeout(self, *, kind: str = "request") -> None:
        self.record(0, 0.0, kind=kind)

    def record_from_exception(self, exc: BaseException | str, *, kind: str = "request") -> None:
        msg = str(exc).lower()
        if "403" in msg or "blocked (403)" in msg:
            self.record_blocked(403, kind=kind)
        elif "429" in msg:
            self.record_blocked(429, kind=kind)
        elif "timeout" in msg or "timed out" in msg:
            self.record_timeout(kind=kind)
        elif "socket" in msg or "connection reset" in msg or "hang up" in msg:
            self.record_timeout(kind=kind)
        elif "net" in msg or "http" in msg:
            self.record_timeout(kind=kind)

    def note_auth_renewal(self) -> None:
        self.auth_renewals += 1

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self.events and self.events[0].ts < cutoff:
            self.events.popleft()
        if now - self._day_bucket_start >= 86400:
            self.throttle_events_24h = 0
            self._day_bucket_start = now

    def window_counts(self) -> dict[str, int]:
        c200 = c403 = c429 = cto = 0
        for e in self.events:
            if e.status == 200:
                c200 += 1
            elif e.status == 403:
                c403 += 1
            elif e.status == 429:
                c429 += 1
            else:
                cto += 1
        return {
            "http_200": c200,
            "http_403": c403,
            "http_429": c429,
            "timeouts": cto,
            "total": len(self.events),
        }

    def avg_response_sec(self) -> float:
        samples = [e.elapsed_sec for e in self.events if e.elapsed_sec > 0]
        if not samples:
            return 0.0
        return sum(samples) / len(samples)

    def throttle_score(self) -> float:
        counts = self.window_counts()
        total = max(counts["total"], 1)
        bad = counts["http_403"] + counts["http_429"] + counts["timeouts"]
        return bad / total

    def _recompute_state(self, now: float) -> None:
        score = self.throttle_score()
        prev = self.state
        if score >= THROTTLE_SCORE_THROTTLED:
            self.state = "throttled"
            self.healthy_since = now
        elif score >= THROTTLE_SCORE_COOLING:
            self.state = "cooling"
            self.healthy_since = now
        else:
            if prev != "healthy":
                self.healthy_since = now
            self.state = "healthy"

        if (
            self.edoc_403_case_streak >= EDOC_STORM_CASES
            and self.knobs.edoc_enabled
        ):
            self.knobs.edoc_enabled = False
            self.edoc_deferred_until = now + EDOC_COOLDOWN_SEC
            self._log_decision("edoc_defer", now)
        elif (
            not self.knobs.edoc_enabled
            and now >= self.edoc_deferred_until
            and self.state == "healthy"
        ):
            self.knobs.edoc_enabled = True
            self.edoc_403_case_streak = 0
            self._log_decision("edoc_restore", now)

    def _apply_pressure(self, now: float, *, force_down: bool = False) -> None:
        if self.state in ("throttled", "cooling") and force_down:
            self._step_down(now)
        elif self.state == "healthy":
            healthy_for = now - self.healthy_since
            if (
                healthy_for >= HEALTHY_STREAK_SEC
                and self.throttle_score() < THROTTLE_SCORE_HEALTHY
            ):
                self._probe_up(now)

    def _step_down(self, now: float) -> None:
        changed = False
        if self.knobs.pdf_concurrency > PDF_CONCURRENCY_FLOOR:
            self.knobs.pdf_concurrency -= 1
            changed = True
        # Climb delay ladder one step (anti-oscillation: immediate downs OK)
        idx = _ladder_index(self.knobs.inter_case_delay_sec)
        target = delay_for_pressure(self.throttle_score(), self.state)
        target_idx = _ladder_index(target)
        new_idx = min(len(DELAY_LADDER) - 1, max(idx + 1, target_idx))
        new_delay = DELAY_LADDER[new_idx]
        if new_delay != self.knobs.inter_case_delay_sec:
            self.knobs.inter_case_delay_sec = new_delay
            changed = True
        self.last_down_at = now
        if changed:
            self._log_decision("throttle_backoff", now)

    def _probe_up(self, now: float) -> None:
        if now - self.last_up_probe_at < MIN_UP_PROBE_SEC:
            return
        if now - self.last_down_at < MIN_UP_PROBE_SEC:
            return
        if (
            self.last_up_probe_at > 0
            and now - self.last_up_probe_at < PROBE_INTERVAL_SEC
        ):
            return
        changed = False
        if self.knobs.pdf_concurrency < PDF_CONCURRENCY_CEILING:
            self.knobs.pdf_concurrency += 1
            changed = True
        # Descend delay ladder one step toward 0
        idx = _ladder_index(self.knobs.inter_case_delay_sec)
        if idx > 0:
            self.knobs.inter_case_delay_sec = DELAY_LADDER[idx - 1]
            changed = True
        self.last_up_probe_at = now
        if changed:
            self._log_decision("probe_up", now)

    def force_step_down(self, reason: str = "throttle_backoff") -> RateKnobs:
        self._step_down(time.time())
        if self.history:
            self.history[-1]["reason"] = reason
        return self.knobs

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "throttle_score": round(self.throttle_score(), 4),
            "window": self.window_counts(),
            "avg_response_sec": round(self.avg_response_sec(), 3),
            "knobs": self.knobs.to_dict(),
            "auth_renewals": self.auth_renewals,
            "throttle_events_24h": self.throttle_events_24h,
            "edoc_403_case_streak": self.edoc_403_case_streak,
            "healthy_for_sec": round(max(0.0, time.time() - self.healthy_since), 1),
            "delay_ladder": list(DELAY_LADDER),
        }

    def _log_decision(self, reason: str, now: float) -> None:
        self.history.append(
            {
                "at": _utc(),
                "reason": reason,
                "state": self.state,
                "throttle_score": round(self.throttle_score(), 4),
                "knobs": self.knobs.to_dict(),
            }
        )
        if len(self.history) > 200:
            self.history = self.history[-200:]

    def maybe_tick_healthy_probe(self) -> RateKnobs:
        now = time.time()
        self._trim(now)
        self._recompute_state(now)
        if self.state == "healthy":
            self._probe_up(now)
        return self.knobs


def append_daily_snapshot(
    reports_dir: Path,
    *,
    payload: dict[str, Any],
) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    jsonl = reports_dir / "daily_snapshots.jsonl"
    row = {"generated_at": _utc(), **payload}
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")

    md_path = reports_dir / "production_validation_report.md"
    section = [
        "",
        f"## Daily Progress Snapshot — {row['generated_at']}",
        "",
        f"- Remaining queue units: {payload.get('queued_units')}",
        f"- Remaining cases: {payload.get('queued_cases')}",
        f"- Completed cases: {payload.get('completed_cases')}",
        f"- Average throughput (cph): {payload.get('avg_cases_per_hour')}",
        f"- Peak throughput (cph): {payload.get('peak_cases_per_hour')}",
        f"- Retry rate: {payload.get('retry_rate')}",
        f"- Auth renewals: {payload.get('auth_renewals')}",
        f"- Throttling events (24h): {payload.get('throttle_events_24h')}",
        f"- Throttle state: {payload.get('throttle_state')}",
        f"- ETA hours: {payload.get('eta_hours')}",
        f"- Integrity: CaseMismatch={payload.get('case_mismatch')} "
        f"DownloadEmpty={payload.get('download_empty')} "
        f"CaseOpenFailed={payload.get('case_open_failed')}",
        "",
    ]
    if md_path.is_file():
        with md_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(section))
    else:
        md_path.write_text(
            "# Production Validation Report\n\n" + "\n".join(section),
            encoding="utf-8",
        )
    return jsonl


def classify_download_outcome(
    *,
    ok: bool,
    error_type: str,
    exc_msg: str = "",
) -> list[tuple[int, str]]:
    signals: list[tuple[int, str]] = []
    msg = (exc_msg or "").lower()
    if ok:
        signals.append((200, "case"))
        return signals
    if "403" in msg or "blocked (403)" in msg:
        kind = "edoc" if "edoc" in msg or "getdocuments" in msg else "request"
        signals.append((403, kind))
    elif "429" in msg:
        signals.append((429, "request"))
    elif "timeout" in msg or "socket" in msg or "hang up" in msg:
        signals.append((0, "request"))
    elif error_type == "CaseOpenFailed":
        signals.append((0, "request"))
    return signals
