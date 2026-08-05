"""Speed-control knobs for single-session Case drain (measurement-gated).

- discovery_parallel_ok: only True after independence proof; auto-fallback
- Facility cache helpers
- Telemetry observe-first → abort only if share >= 8%
- Useful Seconds KPI + discovery auto-rollback (>= +0.5s)
- Adaptive batch: OFF by default (proof-only)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Adaptive cross-case batching stays disabled until WebPT+Golden Rule proof.
ADAPTIVE_BATCH_ENABLED = False
ADAPTIVE_BATCH_SIZE = 20

TELEMETRY_PATTERNS = (
    r"pendo\.io",
    r"ruxitagent",
    r"__utm\.gif",
    r"cdn\.pendo\.io",
    r"google-analytics",
    r"googletagmanager",
    r"/rb_bf[a-z0-9]+",
)
TELEMETRY_ABORT_MIN_WALL_SHARE = 0.08  # 8%
TELEMETRY_IGNORE_MAX_WALL_SHARE = 0.005  # 0.5%
DISCOVERY_ROLLBACK_DELTA_SEC = 0.5
OPEN_S1_ROLLBACK_DELTA_SEC = 0.5

# Resource types aborted during open_s1 navigation (CaseID comes from URL/HTML doc).
S1_ABORT_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "media", "script"})
# Never abort these URL substrings during S1 light nav (app APIs / document).
S1_KEEP_URL_SUBSTR = (
    "patientChart",
    "patientChartNote",
    "/graphql",
    "printPDF",
    "getDocument",
    "GetDocument",
    "getdocuments",
    "edoc",
    "login.webpt",
    "auth.webpt",
    "auth0",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


_TELEMETRY_RE = [re.compile(p, re.I) for p in TELEMETRY_PATTERNS]


def s1_should_abort_request(url: str, resource_type: str) -> bool:
    """Return True if this request is noise safe to abort during S1 navigation."""
    u = url or ""
    if any(s in u for s in S1_KEEP_URL_SUBSTR):
        return False
    # Telemetry / RUM always abort during S1 (and overlaps observe-first abort)
    if any(r.search(u) for r in _TELEMETRY_RE):
        return True
    rt = (resource_type or "").lower()
    if rt in S1_ABORT_RESOURCE_TYPES:
        return True
    # Extra path heuristics when resource_type missing/opaque
    lower = u.lower()
    if any(
        lower.endswith(ext)
        for ext in (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".ico",
            ".css",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".js",
        )
    ):
        return True
    if "/images/" in lower or "font-awesome" in lower:
        return True
    return False


@dataclass
class FacilityCacheEntry:
    clinic_id: str
    jwt_remaining_sec: float | None = None
    csrf: str = ""
    permissions: str = ""
    last_refresh: float = 0.0
    expires_at: float = 0.0

    def is_valid(self, *, now: float | None = None, min_ttl_sec: float = 120.0) -> bool:
        t = now if now is not None else time.time()
        if self.expires_at > 0 and t >= self.expires_at:
            return False
        if self.jwt_remaining_sec is not None and self.jwt_remaining_sec <= min_ttl_sec:
            return False
        if self.last_refresh <= 0:
            return False
        # Soft max age 30 minutes even if JWT claims longer
        if t - self.last_refresh > 30 * 60:
            return False
        return True


@dataclass
class SpeedControlState:
    discovery_parallel_ok: bool = False
    discovery_parallel_failures: int = 0
    discovery_baseline_sec: float | None = None
    discovery_window: list[float] = field(default_factory=list)
    open_s1_light_nav_ok: bool = True
    open_s1_baseline_sec: float | None = None
    open_s1_window: list[float] = field(default_factory=list)
    useful_seconds_saved_open_s1: float = 0.0
    telemetry_observe_sec: float = 0.0
    telemetry_bytes: int = 0
    telemetry_requests: int = 0
    telemetry_abort_enabled: bool = False
    telemetry_observed_cases: int = 0
    useful_seconds_saved_discovery: float = 0.0
    rollbacks: list[dict[str, Any]] = field(default_factory=list)
    last_opt_name: str = ""
    adaptive_batch_enabled: bool = ADAPTIVE_BATCH_ENABLED

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpeedControlState":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        return cls(**{k: v for k, v in data.items() if k in known})


class SpeedController:
    """Persisted speed knobs + KPI under reports/speed_control_state.json."""

    def __init__(self, reports_dir: Path) -> None:
        self.reports_dir = Path(reports_dir)
        self.path = self.reports_dir / "speed_control_state.json"
        self.state = SpeedControlState()
        self.facility_cache: dict[str, FacilityCacheEntry] = {}
        self._telem_re = [re.compile(p, re.I) for p in TELEMETRY_PATTERNS]
        self._wall_sum_sec = 0.0
        self.load()
        self.save()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = SpeedControlState.from_dict(data.get("state") or data)
            for cid, row in (data.get("facility_cache") or {}).items():
                self.facility_cache[str(cid)] = FacilityCacheEntry(**row)
        except Exception:
            pass

    def save(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc(),
            "state": self.state.to_dict(),
            "facility_cache": {
                k: asdict(v) for k, v in self.facility_cache.items()
            },
            "adaptive_batch_note": (
                "OFF by default — requires WebPT + Golden Rule proof before enable"
            ),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # --- Facility cache --------------------------------------------------

    def get_facility_cache(self, clinic_id: str) -> FacilityCacheEntry | None:
        ent = self.facility_cache.get(str(clinic_id))
        if ent is None:
            return None
        if not ent.is_valid():
            return None
        return ent

    def put_facility_cache(
        self,
        clinic_id: str,
        *,
        jwt_remaining_sec: float | None,
        csrf: str = "",
        permissions: str = "",
        ttl_sec: float = 25 * 60,
    ) -> None:
        now = time.time()
        self.facility_cache[str(clinic_id)] = FacilityCacheEntry(
            clinic_id=str(clinic_id),
            jwt_remaining_sec=jwt_remaining_sec,
            csrf=csrf or "",
            permissions=permissions or "",
            last_refresh=now,
            expires_at=now + ttl_sec,
        )
        self.save()

    def invalidate_facility(self, clinic_id: str) -> None:
        self.facility_cache.pop(str(clinic_id), None)
        self.save()

    # --- Discovery parallel / rollback -----------------------------------

    def note_discovery_sec(self, sec: float, *, opt_name: str = "") -> dict[str, Any]:
        """Record discovery duration; auto-rollback parallel if regression >= 0.5s."""
        s = float(sec)
        self.state.discovery_window.append(s)
        if len(self.state.discovery_window) > 50:
            self.state.discovery_window = self.state.discovery_window[-50:]
        window = self.state.discovery_window
        avg = sum(window) / len(window)
        decision: dict[str, Any] = {
            "discovery_sec": round(s, 4),
            "window_avg": round(avg, 4),
            "baseline": self.state.discovery_baseline_sec,
            "rolled_back": False,
        }
        if self.state.discovery_baseline_sec is None and len(window) >= 5:
            self.state.discovery_baseline_sec = avg
            decision["baseline_set"] = True
        elif (
            self.state.discovery_baseline_sec is not None
            and len(window) >= 8
            and avg >= self.state.discovery_baseline_sec + DISCOVERY_ROLLBACK_DELTA_SEC
        ):
            # Regression — disable parallel discovery
            if self.state.discovery_parallel_ok:
                self.state.discovery_parallel_ok = False
                self.state.discovery_parallel_failures += 1
                self.state.rollbacks.append(
                    {
                        "at": _utc(),
                        "reason": "discovery_regression",
                        "baseline": self.state.discovery_baseline_sec,
                        "window_avg": avg,
                        "opt_name": opt_name or self.state.last_opt_name,
                    }
                )
                decision["rolled_back"] = True
                decision["action"] = "disable_discovery_parallel"
            # Reset baseline to current so we don't thrash
            self.state.discovery_baseline_sec = avg
            self.state.discovery_window = []
        elif (
            self.state.discovery_baseline_sec is not None
            and avg < self.state.discovery_baseline_sec
        ):
            saved = self.state.discovery_baseline_sec - avg
            self.state.useful_seconds_saved_discovery += saved / max(len(window), 1)
            decision["useful_seconds_vs_baseline"] = round(saved, 4)
        if opt_name:
            self.state.last_opt_name = opt_name
        self.save()
        self._append_kpi(decision)
        return decision

    def mark_parallel_probe_ok(self) -> None:
        self.state.discovery_parallel_ok = True
        self.state.last_opt_name = "parallel_discovery"
        # Fresh baseline after enabling
        self.state.discovery_baseline_sec = None
        self.state.discovery_window = []
        self.save()

    def mark_parallel_probe_failed(self) -> None:
        self.state.discovery_parallel_ok = False
        self.state.discovery_parallel_failures += 1
        self.state.rollbacks.append(
            {
                "at": _utc(),
                "reason": "parallel_independence_failed",
            }
        )
        self.save()

    def _append_kpi(self, row: dict[str, Any]) -> None:
        path = self.reports_dir / "useful_seconds.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps({"at": _utc(), **row, **self.kpi_snapshot()}) + "\n"
                )
        except Exception:
            pass

    def kpi_snapshot(self) -> dict[str, Any]:
        return {
            "discovery_parallel_ok": self.state.discovery_parallel_ok,
            "open_s1_light_nav_ok": self.state.open_s1_light_nav_ok,
            "useful_seconds_saved_discovery": round(
                self.state.useful_seconds_saved_discovery, 4
            ),
            "useful_seconds_saved_open_s1": round(
                self.state.useful_seconds_saved_open_s1, 4
            ),
            "telemetry_abort_enabled": self.state.telemetry_abort_enabled,
            "adaptive_batch_enabled": bool(
                self.state.adaptive_batch_enabled and ADAPTIVE_BATCH_ENABLED
            ),
            "rollbacks": len(self.state.rollbacks),
        }

    def note_open_s1_sec(self, sec: float, *, opt_name: str = "s1_light_nav") -> dict[str, Any]:
        """Record open_s1 duration; auto-disable light nav if regression >= 0.5s."""
        s = float(sec)
        self.state.open_s1_window.append(s)
        if len(self.state.open_s1_window) > 50:
            self.state.open_s1_window = self.state.open_s1_window[-50:]
        window = self.state.open_s1_window
        avg = sum(window) / len(window)
        decision: dict[str, Any] = {
            "open_s1_sec": round(s, 4),
            "open_s1_window_avg": round(avg, 4),
            "open_s1_baseline": self.state.open_s1_baseline_sec,
            "rolled_back": False,
        }
        if self.state.open_s1_baseline_sec is None and len(window) >= 5:
            self.state.open_s1_baseline_sec = avg
            decision["baseline_set"] = True
        elif (
            self.state.open_s1_baseline_sec is not None
            and len(window) >= 8
            and avg >= self.state.open_s1_baseline_sec + OPEN_S1_ROLLBACK_DELTA_SEC
        ):
            if self.state.open_s1_light_nav_ok:
                self.state.open_s1_light_nav_ok = False
                self.state.rollbacks.append(
                    {
                        "at": _utc(),
                        "reason": "open_s1_regression",
                        "baseline": self.state.open_s1_baseline_sec,
                        "window_avg": avg,
                        "opt_name": opt_name or self.state.last_opt_name,
                    }
                )
                decision["rolled_back"] = True
                decision["action"] = "disable_s1_light_nav"
            self.state.open_s1_baseline_sec = avg
            self.state.open_s1_window = []
        elif (
            self.state.open_s1_baseline_sec is not None
            and avg < self.state.open_s1_baseline_sec
        ):
            saved = self.state.open_s1_baseline_sec - avg
            self.state.useful_seconds_saved_open_s1 += saved / max(len(window), 1)
            decision["useful_seconds_vs_baseline"] = round(saved, 4)
        if opt_name:
            self.state.last_opt_name = opt_name
        self.save()
        self._append_kpi(decision)
        return decision

    def should_use_s1_light_nav(self) -> bool:
        return bool(self.state.open_s1_light_nav_ok)

    # --- Telemetry observe-first -----------------------------------------

    def is_telemetry_url(self, url: str) -> bool:
        u = url or ""
        return any(r.search(u) for r in self._telem_re)

    def observe_telemetry(self, *, elapsed_sec: float, nbytes: int = 0) -> None:
        self.state.telemetry_observe_sec += max(0.0, float(elapsed_sec))
        self.state.telemetry_bytes += max(0, int(nbytes))
        self.state.telemetry_requests += 1

    def note_case_wall_for_telemetry(self, wall_sec: float) -> dict[str, Any]:
        """After enough cases, decide abort vs ignore based on wall share."""
        self._wall_sum_sec += max(0.0, float(wall_sec))
        self.state.telemetry_observed_cases += 1
        denom = max(self._wall_sum_sec, 1.0)
        share = self.state.telemetry_observe_sec / denom
        decision = {
            "telemetry_share": round(share, 6),
            "telemetry_requests": self.state.telemetry_requests,
            "abort_enabled": self.state.telemetry_abort_enabled,
            "just_enabled": False,
        }
        if self.state.telemetry_abort_enabled:
            return decision
        if self.state.telemetry_observed_cases < 15:
            self.save()
            return decision
        if share >= TELEMETRY_ABORT_MIN_WALL_SHARE:
            self.state.telemetry_abort_enabled = True
            self.state.last_opt_name = "telemetry_abort"
            decision["abort_enabled"] = True
            decision["just_enabled"] = True
        elif share <= TELEMETRY_IGNORE_MAX_WALL_SHARE:
            self.state.telemetry_abort_enabled = False
        self.save()
        return decision

    def should_abort_telemetry(self) -> bool:
        return bool(self.state.telemetry_abort_enabled)

    def observe_http_if_telemetry(
        self, url: str, *, elapsed_sec: float, nbytes: int = 0
    ) -> None:
        if self.is_telemetry_url(url):
            self.observe_telemetry(elapsed_sec=elapsed_sec, nbytes=nbytes)


def write_batch_proof_note(reports_dir: Path) -> Path:
    path = Path(reports_dir) / "adaptive_batch_proof.md"
    path.write_text(
        "\n".join(
            [
                "# Adaptive Batch Size — Proof Gate",
                "",
                f"ADAPTIVE_BATCH_ENABLED = {ADAPTIVE_BATCH_ENABLED}",
                f"Proposed batch size = {ADAPTIVE_BATCH_SIZE}",
                "",
                "Status: **OFF** (default).",
                "",
                "Required before enable:",
                "1. Proof WebPT allows multi-case discovery under one session without cross-case mix.",
                "2. Golden Rule preserved (S1 per case before that case's PDF wave).",
                "3. Measured Useful Seconds > 0 vs case-by-case baseline.",
                "",
                "Do not enable in production until all three are documented here.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
