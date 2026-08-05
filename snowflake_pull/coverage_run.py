"""Coverage recovery run lifecycle: init, resume, locks, input hashes, workspace."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snowflake_pull.observability import ObsContext, _atomic_write_json, utc_now_iso
from snowflake_pull.unit_state import UnitStateStore

_REPO = Path(__file__).resolve().parents[1]

DEFAULT_COVERAGE_ROOT = (
    _REPO
    / "webpt_edco_scraper"
    / "output"
    / "jun_jul_2026"
    / "coverage_fix"
)

DEFAULT_INPUTS = {
    "snowflake": _REPO / "snowflake_pull" / "output" / "all_billing_data.csv",
    "patients_export": (
        _REPO
        / "webpt_edco_scraper"
        / "output"
        / "jun_jul_2026"
        / "patients_export_273d.csv"
    ),
    "daily_notes": (
        _REPO
        / "webpt_edco_scraper"
        / "output"
        / "jun_jul_2026"
        / "extracted"
        / "daily_notes.csv"
    ),
    "cpt_codes": (
        _REPO
        / "webpt_edco_scraper"
        / "output"
        / "jun_jul_2026"
        / "extracted"
        / "cpt_codes.csv"
    ),
    "reconciliation_visits": (
        _REPO
        / "webpt_edco_scraper"
        / "output"
        / "jun_jul_2026"
        / "reconciliation"
        / "reconciliation_visits.csv"
    ),
}


def file_sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


def make_run_id(operator: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    op = (operator or os.environ.get("USERNAME") or os.environ.get("USER") or "op").strip()
    return f"{ts}_{git_sha()}_{op}"


@dataclass
class CoverageRun:
    run_id: str
    root: Path
    run_dir: Path
    manifest: dict[str, Any]
    store: UnitStateStore
    obs: ObsContext

    @property
    def state_db(self) -> Path:
        return self.run_dir / "state" / "units.sqlite"

    @property
    def artifacts(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def baseline(self) -> Path:
        return self.run_dir / "baseline"

    @property
    def side_by_side(self) -> Path:
        return self.run_dir / "artifacts" / "side_by_side"


def coverage_root(path: Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_COVERAGE_ROOT


def lock_path(root: Path) -> Path:
    return root / "RUN_LOCK"


def promote_lock_path(root: Path) -> Path:
    return root / "PROMOTE_LOCK"


def acquire_lock(root: Path, run_id: str, *, lock_file: Path | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lp = lock_file or lock_path(root)
    if lp.exists():
        existing = lp.read_text(encoding="utf-8").strip()
        if existing != run_id:
            raise RuntimeError(
                f"Lock held by {existing!r} at {lp}. "
                f"Resume that run or remove the lock deliberately."
            )
        return lp
    tmp = lp.with_suffix(".tmp")
    tmp.write_text(run_id + "\n", encoding="utf-8")
    try:
        tmp.replace(lp)
    except FileExistsError:
        # Windows race
        existing = lp.read_text(encoding="utf-8").strip()
        if existing != run_id:
            raise RuntimeError(f"Lock race: held by {existing!r}")
    return lp


def release_lock(root: Path, run_id: str, *, lock_file: Path | None = None) -> None:
    lp = lock_file or lock_path(root)
    if not lp.exists():
        return
    existing = lp.read_text(encoding="utf-8").strip()
    if existing != run_id:
        raise RuntimeError(f"Refusing to release lock owned by {existing!r}")
    lp.unlink()


def write_current_pointer(root: Path, run_id: str) -> None:
    _atomic_write_json(root / "current.json", {"run_id": run_id, "ts": utc_now_iso()})


def hash_inputs(inputs: dict[str, Path]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for key, path in inputs.items():
        p = Path(path)
        if not p.is_file():
            out[key] = {"path": str(p), "missing": "true", "sha256": ""}
            continue
        out[key] = {
            "path": str(p.resolve()),
            "sha256": file_sha256(p),
            "size": str(p.stat().st_size),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
        }
    return out


def verify_input_hashes(
    manifest: dict[str, Any],
    *,
    allow_drift: bool = False,
) -> list[str]:
    drifts: list[str] = []
    for key, meta in (manifest.get("input_hashes") or {}).items():
        path = Path(meta.get("path") or "")
        expected = meta.get("sha256") or ""
        if not expected:
            continue
        if not path.is_file():
            drifts.append(f"{key}: missing {path}")
            continue
        actual = file_sha256(path)
        if actual != expected:
            drifts.append(f"{key}: sha256 changed ({expected[:12]} -> {actual[:12]})")
    if drifts and not allow_drift:
        raise RuntimeError(
            "Input drift detected (refuse to continue without --allow-input-drift):\n"
            + "\n".join(drifts)
        )
    return drifts


def _copy_baseline(inputs: dict[str, Path], baseline_dir: Path) -> None:
    import shutil

    baseline_dir.mkdir(parents=True, exist_ok=True)
    for key in ("reconciliation_visits",):
        src = inputs.get(key)
        if src and src.is_file():
            shutil.copy2(src, baseline_dir / src.name)
    # also copy prior sf_compare summary if present
    sf_summary = (
        _REPO
        / "webpt_edco_scraper"
        / "output"
        / "jun_jul_2026"
        / "reconciliation"
        / "sf_compare"
        / "summary.txt"
    )
    if sf_summary.is_file():
        shutil.copy2(sf_summary, baseline_dir / "sf_compare_summary.txt")


def init_run(
    *,
    root: Path | None = None,
    operator: str = "",
    script: str = "coverage_run",
    knobs: dict[str, Any] | None = None,
    inputs: dict[str, Path] | None = None,
) -> CoverageRun:
    root = coverage_root(root)
    run_id = make_run_id(operator)
    acquire_lock(root, run_id)
    run_dir = root / "runs" / run_id
    for sub in (
        "logs",
        "metrics",
        "monitoring",
        "summaries",
        "errors",
        "artifacts",
        "baseline",
        "state",
        "artifacts/side_by_side",
        "artifacts/shadow",
        "artifacts/pilots",
    ):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    input_paths = dict(DEFAULT_INPUTS)
    if inputs:
        input_paths.update(inputs)
    hashes = hash_inputs(input_paths)
    _copy_baseline(input_paths, run_dir / "baseline")

    manifest = {
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "git_sha": git_sha(),
        "operator": operator or os.environ.get("USERNAME") or "",
        "repo": str(_REPO),
        "input_hashes": hashes,
        "knobs": knobs or {},
        "status": "running",
    }
    _atomic_write_json(run_dir / "manifest.json", manifest)
    write_current_pointer(root, run_id)

    store = UnitStateStore(run_dir / "state" / "units.sqlite")
    obs = ObsContext(run_dir, run_id=run_id, script=script, stage="init")
    obs.start_heartbeat()
    obs.emit(
        "stage_start",
        operation="run_init",
        decision="run_created",
        decision_reason="new_coverage_run",
        extra={"run_dir": str(run_dir)},
    )
    # checkpoint pointer
    _atomic_write_json(
        run_dir / "checkpoint.json",
        {
            "run_id": run_id,
            "sqlite": str(run_dir / "state" / "units.sqlite"),
            "updated_at": utc_now_iso(),
            "cursors": {},
        },
    )
    return CoverageRun(
        run_id=run_id,
        root=root,
        run_dir=run_dir,
        manifest=manifest,
        store=store,
        obs=obs,
    )


def resume_run(
    run_id: str,
    *,
    root: Path | None = None,
    script: str = "coverage_run",
    allow_input_drift: bool = False,
    in_progress_ttl_seconds: float = 600.0,
) -> CoverageRun:
    root = coverage_root(root)
    run_dir = root / "runs" / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(run_dir)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    drifts = verify_input_hashes(manifest, allow_drift=allow_input_drift)
    acquire_lock(root, run_id)
    write_current_pointer(root, run_id)
    store = UnitStateStore(run_dir / "state" / "units.sqlite")
    reset = store.reclaim_stale_in_progress(in_progress_ttl_seconds)
    obs = ObsContext(run_dir, run_id=run_id, script=script, stage="resume")
    obs.start_heartbeat()
    obs.emit(
        "stage_start",
        operation="run_resume",
        decision="run_resumed",
        decision_reason="resume_from_checkpoint",
        extra={"reclaimed_in_progress": reset, "input_drifts": drifts},
    )
    obs.mark_checkpoint()
    return CoverageRun(
        run_id=run_id,
        root=root,
        run_dir=run_dir,
        manifest=manifest,
        store=store,
        obs=obs,
    )


def finish_run(run: CoverageRun, *, status: str = "completed") -> None:
    run.obs.write_errors_rollup()
    run.obs.write_retry_summary()
    run.manifest["status"] = status
    run.manifest["finished_at"] = utc_now_iso()
    _atomic_write_json(run.run_dir / "manifest.json", run.manifest)
    run.obs.stop_heartbeat()
    run.store.close()
    release_lock(run.root, run.run_id)


def load_gate(run_dir: Path, gate_id: str) -> dict[str, Any] | None:
    candidates = [
        run_dir / "summaries" / "gates" / f"{gate_id}.json",
        run_dir / "summaries" / f"gate_{gate_id}_summary.json",
        run_dir / "summaries" / f"pilot_{gate_id}_summary.json",
    ]
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def require_gate_pass(run_dir: Path, gate_id: str) -> dict[str, Any]:
    gate = load_gate(run_dir, gate_id)
    if gate is None:
        raise RuntimeError(f"Gate {gate_id} not found under {run_dir}/summaries")
    if not gate.get("pass"):
        raise RuntimeError(
            f"Gate {gate_id} did not pass: {gate.get('reason') or gate.get('fail_reason')}"
        )
    return gate
