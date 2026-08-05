"""Self-optimizing control plane for Case download (Golden Rule enforced)."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Keys that must never appear / never be true in proposed configs.
FORBIDDEN_OPT_KEYS = frozenset(
    {
        "include_all_cases",
        "skip_s1_verify",
        "skip_open_verify",
        "infer_case_id",
        "disable_case_isolation",
        "collapse_without_case_id",
    }
)

WORKER_SCALE_STEPS = (1, 2, 4, 8, 16)
IMPROVE_THRESHOLD = 0.02  # 2%


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OptimizerConfig:
    pdf_concurrency: int = 2
    worker_scale: int = 1
    facility_strategy: str = "facility_exhaust"
    sticky_facility: bool = True
    require_s1_verify: bool = True
    include_all_cases: bool = False  # must always remain False
    inter_case_delay_sec: float = 0.0
    edoc_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OptimizerConfig":
        return cls(
            pdf_concurrency=min(8, max(1, int(d.get("pdf_concurrency", 4)))),
            worker_scale=1,  # single WebPT session — never scale browsers
            facility_strategy="facility_exhaust",
            sticky_facility=True,
            require_s1_verify=bool(d.get("require_s1_verify", True)),
            include_all_cases=bool(d.get("include_all_cases", False)),
            inter_case_delay_sec=float(d.get("inter_case_delay_sec", 0.0) or 0.0),
            edoc_enabled=bool(d.get("edoc_enabled", True)),
        )


def reject_integrity_weakening(cfg: OptimizerConfig | dict[str, Any]) -> list[str]:
    """Golden Rule: reject any config that weakens S0–S6 / Case isolation."""
    d = cfg.to_dict() if isinstance(cfg, OptimizerConfig) else dict(cfg)
    reasons: list[str] = []
    if d.get("include_all_cases") is True:
        reasons.append("include_all_cases=True forbidden")
    if d.get("require_s1_verify") is False:
        reasons.append("skipping S1 verify forbidden")
    if d.get("skip_s1_verify") or d.get("skip_open_verify"):
        reasons.append("skip open/verify forbidden")
    if d.get("infer_case_id") or d.get("disable_case_isolation"):
        reasons.append("case isolation weakening forbidden")
    if d.get("collapse_without_case_id"):
        reasons.append("collapse_without_case_id forbidden")
    return reasons


def sort_facilities_by_eta(
    remaining_cases: dict[str, int],
    avg_download_sec: dict[str, float],
    *,
    strategy: str = "facility_exhaust",
    default_avg_sec: float = 45.0,
) -> list[tuple[str, float, int]]:
    """Return (facility_id, eta_sec, remaining_cases) ordered by strategy.

    facility_exhaust / longest_remaining_first: drain largest clinics first
    to minimize switch count over the run.
    """
    rows: list[tuple[str, float, int]] = []
    for fid, n in remaining_cases.items():
        avg = float(avg_download_sec.get(fid, default_avg_sec) or default_avg_sec)
        eta = n * avg
        rows.append((fid, eta, n))
    if strategy in ("facility_exhaust", "longest_remaining_first"):
        # Prefer more remaining cases (then longer ETA) — exhaust big clinics first
        rows.sort(key=lambda r: (-r[2], -r[1], r[0]))
    elif strategy == "sticky_only":
        rows.sort(key=lambda r: r[0])
    elif strategy == "shortest_remaining_first":
        rows.sort(key=lambda r: (r[1], r[2], r[0]))
    else:
        rows.sort(key=lambda r: (-r[2], -r[1], r[0]))
    return rows


@dataclass
class TimingBucket:
    facility_switch_sec: float = 0.0
    chart_load_sec: float = 0.0
    pdf_download_sec: float = 0.0
    extract_sec: float = 0.0
    merge_sec: float = 0.0
    other_sec: float = 0.0

    def total(self) -> float:
        return (
            self.facility_switch_sec
            + self.chart_load_sec
            + self.pdf_download_sec
            + self.extract_sec
            + self.merge_sec
            + self.other_sec
        )

    def shares(self) -> dict[str, float]:
        t = self.total() or 1.0
        return {
            "facility_switching": round(100 * self.facility_switch_sec / t, 1),
            "pdf_download": round(100 * self.pdf_download_sec / t, 1),
            "chart_load": round(100 * self.chart_load_sec / t, 1),
            "extraction": round(100 * self.extract_sec / t, 1),
            "merge": round(100 * self.merge_sec / t, 1),
            "other": round(100 * self.other_sec / t, 1),
        }

    def top_bottleneck(self) -> str:
        shares = self.shares()
        return max(shares, key=shares.get)


@dataclass
class RuntimeMetrics:
    cases_done: int = 0
    cases_failed: int = 0
    downloads_ok: int = 0
    retries: int = 0
    case_mismatch: int = 0
    download_empty: int = 0
    case_open_failed: int = 0
    started_at: float = field(default_factory=time.time)
    window_cases: int = 0
    window_started_at: float = field(default_factory=time.time)
    peak_cases_per_hour: float = 0.0
    avg_download_sec_by_facility: dict[str, float] = field(default_factory=dict)
    download_samples_by_facility: dict[str, list[float]] = field(default_factory=dict)
    timings: TimingBucket = field(default_factory=TimingBucket)
    current_facility: str = ""
    current_case: str = ""
    auth_status: str = "unknown"
    worker_scale: int = 1
    pdf_concurrency: int = 2

    def cases_per_hour(self) -> float:
        elapsed = max(time.time() - self.started_at, 1e-3)
        return self.cases_done * 3600.0 / elapsed

    def window_cases_per_hour(self) -> float:
        elapsed = max(time.time() - self.window_started_at, 1e-3)
        return self.window_cases * 3600.0 / elapsed

    def retry_rate(self) -> float:
        denom = max(self.cases_done + self.cases_failed, 1)
        return self.retries / denom

    def record_download(
        self, facility_id: str, elapsed_sec: float, *, ok: bool
    ) -> None:
        samples = self.download_samples_by_facility.setdefault(facility_id, [])
        samples.append(elapsed_sec)
        if len(samples) > 50:
            del samples[:-50]
        self.avg_download_sec_by_facility[facility_id] = sum(samples) / len(samples)
        if ok:
            self.downloads_ok += 1
            self.cases_done += 1
            self.window_cases += 1
        else:
            self.cases_failed += 1
        cph = self.cases_per_hour()
        if cph > self.peak_cases_per_hour:
            self.peak_cases_per_hour = cph

    def reset_window(self) -> None:
        self.window_cases = 0
        self.window_started_at = time.time()


class DynamicThroughputOptimizer:
    """Every 5 minutes propose config; keep if ≥2% gain else rollback."""

    def __init__(self, reports_dir: Path, *, interval_sec: float = 300.0) -> None:
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.interval_sec = interval_sec
        self.config = OptimizerConfig()
        self.best_config = OptimizerConfig()
        self.last_good_config = OptimizerConfig()
        self.last_metric = 0.0
        self.best_metric = 0.0
        self.last_tick = time.time()
        self.history_path = self.reports_dir / "optimizer_history.jsonl"
        self.state_path = self.reports_dir / "optimizer_state.json"
        self.kept_improvements = 0
        self.rollbacks = 0
        self._probe_index = 0

    def load(self) -> None:
        if self.state_path.is_file():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.config = OptimizerConfig.from_dict(data.get("config") or {})
            self.best_config = OptimizerConfig.from_dict(
                data.get("best_config") or self.config.to_dict()
            )
            self.last_good_config = OptimizerConfig.from_dict(
                data.get("last_good_config") or self.config.to_dict()
            )
            self.best_metric = float(data.get("best_metric") or 0)
            self.last_metric = float(data.get("last_metric") or 0)
            self.kept_improvements = int(data.get("kept_improvements") or 0)
            self.rollbacks = int(data.get("rollbacks") or 0)

    def save(self) -> None:
        payload = {
            "updated_at": _utc(),
            "config": self.config.to_dict(),
            "best_config": self.best_config.to_dict(),
            "last_good_config": self.last_good_config.to_dict(),
            "best_metric": self.best_metric,
            "last_metric": self.last_metric,
            "kept_improvements": self.kept_improvements,
            "rollbacks": self.rollbacks,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def maybe_tick(
        self,
        metrics: RuntimeMetrics,
        *,
        force: bool = False,
        throttle: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not force and (time.time() - self.last_tick) < self.interval_sec:
            return None
        self.last_tick = time.time()
        current = metrics.window_cases_per_hour()
        decision = self._evaluate(
            current, metrics, integrity_ok=True, throttle=throttle or {}
        )
        self._append_history(decision)
        self.save()
        metrics.reset_window()
        return decision

    def sync_from_rate_knobs(self, knobs: dict[str, Any]) -> None:
        """Apply AdaptiveRateController knobs without weakening integrity."""
        if "pdf_concurrency" in knobs:
            self.config.pdf_concurrency = min(
                8, max(1, int(knobs["pdf_concurrency"]))
            )
        if "inter_case_delay_sec" in knobs:
            self.config.inter_case_delay_sec = float(knobs["inter_case_delay_sec"])
        if "edoc_enabled" in knobs:
            self.config.edoc_enabled = bool(knobs["edoc_enabled"])
        self.config.require_s1_verify = True
        self.config.include_all_cases = False

    def _evaluate(
        self,
        current_cph: float,
        metrics: RuntimeMetrics,
        *,
        integrity_ok: bool,
        throttle: dict[str, Any],
    ) -> dict[str, Any]:
        prev = self.last_metric
        action = "baseline"
        reason = "baseline"
        throttle_state = str(throttle.get("state") or "healthy")
        throttle_score = float(throttle.get("throttle_score") or 0.0)

        # Long-run policy: never raise concurrency while cooling/throttled
        if throttle_state in ("throttled", "cooling"):
            self.config.sticky_facility = True
            self.config.pdf_concurrency = min(
                self.config.pdf_concurrency,
                int((throttle.get("knobs") or {}).get("pdf_concurrency") or 1),
            )
            if "knobs" in throttle:
                self.sync_from_rate_knobs(throttle["knobs"])
            action = "throttle_backoff"
            reason = "throttle_backoff"
            proposed = deepcopy(self.config)
        elif (
            prev > 0
            and current_cph >= prev * (1.0 + IMPROVE_THRESHOLD)
            and integrity_ok
            and throttle_score < 0.02
        ):
            self.last_good_config = deepcopy(self.config)
            self.best_config = deepcopy(self.config)
            self.best_metric = max(self.best_metric, current_cph)
            self.kept_improvements += 1
            action = "keep"
            reason = "probe_up"
            proposed = self._propose(metrics, direction="up", throttle_state=throttle_state)
        elif prev > 0 and current_cph < prev * (1.0 - IMPROVE_THRESHOLD / 2):
            self.config = deepcopy(self.last_good_config)
            self.rollbacks += 1
            action = "rollback"
            reason = "rollback"
            proposed = self.config
        else:
            proposed = self._propose(
                metrics, direction="probe", throttle_state=throttle_state
            )
            action = "probe"
            reason = "probe"

        reject_reasons = reject_integrity_weakening(proposed)
        if reject_reasons:
            proposed = deepcopy(self.last_good_config)
            action = "reject_integrity"
            reason = "reject_integrity"
        else:
            # Cap long-run concurrency
            proposed.pdf_concurrency = min(8, max(1, proposed.pdf_concurrency))
            proposed.sticky_facility = True
            proposed.facility_strategy = "facility_exhaust"
            proposed.worker_scale = 1
            self.config = proposed

        self.last_metric = current_cph if current_cph > 0 else self.last_metric
        return {
            "at": _utc(),
            "action": action,
            "reason": reason,
            "cases_per_hour": round(current_cph, 2),
            "prev_cases_per_hour": round(prev, 2),
            "config": self.config.to_dict(),
            "reject_reasons": reject_reasons,
            "throttle_state": throttle_state,
            "throttle_score": throttle_score,
            "bottleneck": metrics.timings.top_bottleneck(),
            "bottleneck_shares": metrics.timings.shares(),
        }

    def _propose(
        self,
        metrics: RuntimeMetrics,
        *,
        direction: str,
        throttle_state: str = "healthy",
    ) -> OptimizerConfig:
        cfg = deepcopy(self.config)
        if throttle_state in ("throttled", "cooling"):
            cfg.pdf_concurrency = max(1, min(cfg.pdf_concurrency, 2))
            cfg.sticky_facility = True
            cfg.require_s1_verify = True
            cfg.include_all_cases = False
            return cfg

        bottleneck = metrics.timings.top_bottleneck()
        cfg.sticky_facility = True
        cfg.facility_strategy = "facility_exhaust"
        cfg.worker_scale = 1
        if bottleneck == "pdf_download" or direction in ("up", "probe"):
            if direction == "up" or (
                direction == "probe" and metrics.retry_rate() <= 0.2
            ):
                cfg.pdf_concurrency = min(cfg.pdf_concurrency + 1, 8)
            elif metrics.retry_rate() > 0.15:
                cfg.pdf_concurrency = max(1, cfg.pdf_concurrency - 1)

        cfg.require_s1_verify = True
        cfg.include_all_cases = False
        return cfg

    def _append_history(self, decision: dict[str, Any]) -> None:
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision) + "\n")


def write_bottleneck_report(path: Path, timings: TimingBucket) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shares = timings.shares()
    payload = {
        "updated_at": _utc(),
        "current_bottleneck": timings.top_bottleneck(),
        "shares_pct": shares,
        "seconds": asdict(timings),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_health(
    path: Path,
    *,
    metrics: RuntimeMetrics,
    queue_remaining: int,
    cases_remaining: int,
    errors: dict[str, int],
    facility_strategy: str,
    throttle: dict[str, Any] | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cph = metrics.cases_per_hour()
    avg_sec = 45.0
    if metrics.avg_download_sec_by_facility:
        avg_sec = sum(metrics.avg_download_sec_by_facility.values()) / len(
            metrics.avg_download_sec_by_facility
        )
    eta_sec = cases_remaining * avg_sec
    try:
        import psutil  # type: ignore

        cpu = psutil.cpu_percent(interval=0.0)
        mem = psutil.virtual_memory().percent
    except Exception:
        cpu = -1.0
        mem = -1.0

    throttle = throttle or {}
    knobs = throttle.get("knobs") or {}
    payload = {
        "updated_at": _utc(),
        "health": "ok" if metrics.auth_status != "failed" else "auth_failed",
        "queue_units_remaining": queue_remaining,
        "cases_remaining": cases_remaining,
        "eta_sec": round(eta_sec, 1),
        "eta_hours": round(eta_sec / 3600.0, 2),
        "errors": errors,
        "speed_cases_per_hour": round(cph, 2),
        "peak_cases_per_hour": round(metrics.peak_cases_per_hour, 2),
        "cpu_percent": cpu,
        "memory_percent": mem,
        "auth_status": metrics.auth_status,
        "facility": metrics.current_facility,
        "current_case": metrics.current_case,
        "retry_rate": round(metrics.retry_rate(), 4),
        "worker_scale": metrics.worker_scale,
        "pdf_concurrency": int(
            knobs.get("pdf_concurrency", metrics.pdf_concurrency)
        ),
        "inter_case_delay_sec": float(knobs.get("inter_case_delay_sec", 0.0) or 0.0),
        "edoc_enabled": bool(knobs.get("edoc_enabled", True)),
        "throttle_state": throttle.get("state", "healthy"),
        "throttle_score": throttle.get("throttle_score", 0.0),
        "throttle_window": throttle.get("window"),
        "throttle_events_24h": throttle.get("throttle_events_24h", 0),
        "facility_strategy": facility_strategy,
        "cases_done": metrics.cases_done,
        "cases_failed": metrics.cases_failed,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log_path = path.with_suffix(".log")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            f"{payload['updated_at']} health={payload['health']} "
            f"cph={payload['speed_cases_per_hour']} "
            f"queue={queue_remaining} cases_rem={cases_remaining} "
            f"fac={metrics.current_facility} case={metrics.current_case} "
            f"retry={payload['retry_rate']} "
            f"throttle={payload['throttle_state']} "
            f"score={payload['throttle_score']} "
            f"pdf={payload['pdf_concurrency']} "
            f"delay={payload['inter_case_delay_sec']}\n"
        )
    return path


def append_metrics_jsonl(path: Path, event: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {**event, "at": event.get("at") or _utc()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def write_execution_reports(
    reports_dir: Path,
    *,
    metrics: RuntimeMetrics,
    optimizer: DynamicThroughputOptimizer,
    error_counts: dict[str, int],
    validation: dict[str, Any],
    integrity: dict[str, Any],
    manual_actions: list[str],
) -> dict[str, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    cph = metrics.cases_per_hour()
    total = metrics.cases_done + metrics.cases_failed
    success_rate = (metrics.cases_done / total) if total else 0.0

    perf = reports_dir / "performance_report.md"
    perf.write_text(
        "\n".join(
            [
                "# Performance Report",
                "",
                f"- Cases done: {metrics.cases_done}",
                f"- Cases failed: {metrics.cases_failed}",
                f"- Average cases/hour: {cph:.2f}",
                f"- Peak cases/hour: {metrics.peak_cases_per_hour:.2f}",
                f"- Downloads ok: {metrics.downloads_ok}",
                f"- Worker scale: {metrics.worker_scale}",
                f"- PDF concurrency: {metrics.pdf_concurrency}",
                f"- Retry rate: {metrics.retry_rate():.4f}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths["performance"] = perf

    integ = reports_dir / "integrity_report.md"
    integ.write_text(
        "# Integrity Report\n\n"
        + json.dumps(integrity, indent=2)
        + "\n\nGolden Rule: S0–S6 immutable; no integrity-weakening optimizations kept.\n",
        encoding="utf-8",
    )
    paths["integrity"] = integ

    val = reports_dir / "validation_report.md"
    val.write_text(
        "# Validation Report (S0–S6)\n\n" + json.dumps(validation, indent=2) + "\n",
        encoding="utf-8",
    )
    paths["validation"] = val

    fail = reports_dir / "failure_summary.md"
    fail.write_text(
        "# Failure Summary\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in sorted(error_counts.items()))
        + "\n",
        encoding="utf-8",
    )
    paths["failure"] = fail

    opt = reports_dir / "optimization_summary.md"
    opt.write_text(
        "\n".join(
            [
                "# Optimization Summary",
                "",
                f"- Kept improvements: {optimizer.kept_improvements}",
                f"- Rollbacks: {optimizer.rollbacks}",
                f"- Best metric (cases/hour): {optimizer.best_metric:.2f}",
                f"- Best config: `{json.dumps(optimizer.best_config.to_dict())}`",
                f"- Last good config: `{json.dumps(optimizer.last_good_config.to_dict())}`",
                f"- Current config: `{json.dumps(optimizer.config.to_dict())}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths["optimization"] = opt

    thr = reports_dir / "throughput_stats.json"
    thr.write_text(
        json.dumps(
            {
                "cases_done": metrics.cases_done,
                "cases_failed": metrics.cases_failed,
                "cases_per_hour": cph,
                "peak_cases_per_hour": metrics.peak_cases_per_hour,
                "success_rate": success_rate,
                "retry_rate": metrics.retry_rate(),
                "avg_download_sec_by_facility": metrics.avg_download_sec_by_facility,
                "bottleneck_shares": metrics.timings.shares(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    paths["throughput"] = thr

    audit = reports_dir / "final_audit_report.md"
    audit.write_text(
        "\n".join(
            [
                "# Final Audit Report",
                "",
                f"- Promote blocked: true",
                f"- Success rate: {success_rate:.4f}",
                f"- CaseMismatch: {error_counts.get('CaseMismatch', 0)}",
                f"- DownloadEmpty: {error_counts.get('DownloadEmpty', 0)}",
                f"- CaseOpenFailed: {error_counts.get('CaseOpenFailed', 0)}",
                f"- Validation: {validation.get('all_pass')}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths["audit"] = audit

    manual = reports_dir / "manual_actions.md"
    actions = manual_actions or ["None — autonomous run completed or paused cleanly."]
    if not actions:
        actions = ["None"]
    manual.write_text(
        "# Remaining Manual Actions\n\n" + "\n".join(f"- {a}" for a in actions) + "\n",
        encoding="utf-8",
    )
    paths["manual"] = manual

    complete = reports_dir / "execution_complete.md"
    complete.write_text(
        "\n".join(
            [
                "# Execution Complete",
                "",
                f"Processed Cases: {metrics.cases_done}",
                f"Failed Cases: {metrics.cases_failed}",
                f"Success Rate: {success_rate:.4f}",
                f"Average Cases/hour: {cph:.2f}",
                f"Peak Cases/hour: {metrics.peak_cases_per_hour:.2f}",
                f"Best Concurrency (pdf): {optimizer.best_config.pdf_concurrency}",
                f"Best Worker Scale: {optimizer.best_config.worker_scale}",
                f"Best Facility Strategy: {optimizer.best_config.facility_strategy}",
                f"CaseMismatch: {error_counts.get('CaseMismatch', 0)}",
                f"DownloadEmpty: {error_counts.get('DownloadEmpty', 0)}",
                f"Invariant Failures: {integrity.get('invariant_failures', 0)}",
                "",
                "## Recommended Settings for future runs",
                "",
                "```json",
                json.dumps(optimizer.best_config.to_dict(), indent=2),
                "```",
                "",
                "Promote remains blocked until S0–S6 green across full window.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths["execution_complete"] = complete
    return paths
