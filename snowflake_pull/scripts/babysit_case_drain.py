"""Babysit Jan–Aug Case drain until queue empty, then final validation + promote gate.

- Ensures a single run_case_download_worker is alive (restarts if crashed).
- Never starts a second concurrent WebPT drain / second browser while one is running.
- WebPT single-session only — ignores/rejects multi-browser flags.
- When cases_remaining == 0: run production validation --skip-download.
- Writes promote_gate.json + updates executive summary recommendation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BATCH_DEFAULT = "case_schedule_202601_202608"
WORKER_SCRIPT = ROOT / "snowflake_pull" / "scripts" / "run_case_download_worker.py"
VALIDATION_SCRIPT = ROOT / "snowflake_pull" / "scripts" / "run_production_validation.py"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_worker_pids() -> list[int]:
    """Return PIDs of run_case_download_worker / production_validation download loops."""
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore

    pids: list[int] = []
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except (psutil.Error, TypeError):
                continue
            if "run_case_download_worker.py" in cmd:
                pids.append(int(proc.info["pid"]))
            elif (
                "run_production_validation.py" in cmd
                and "--skip-download" not in cmd
            ):
                pids.append(int(proc.info["pid"]))
        return pids

    # Fallback: Windows WMIC / PowerShell-less via tasklist is weak; use psutil preferred
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                        "Where-Object { $_.CommandLine -match 'run_case_download_worker|"
                        "run_production_validation' -and $_.CommandLine -notmatch "
                        "'--skip-download' } | Select-Object -ExpandProperty ProcessId"
                    ),
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
        except Exception:
            pass
    return pids


def _start_worker(out_dir: Path, batch_id: str, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("a", encoding="utf-8")
    log_f.write(f"\n--- babysit start worker {_utc()} ---\n")
    log_f.flush()
    return subprocess.Popen(
        [
            sys.executable,
            str(WORKER_SCRIPT),
            "--out-dir",
            str(out_dir),
            "--batch-id",
            batch_id,
        ],
        cwd=str(ROOT),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32"
        else 0,
    )


def _queue_remaining(out_dir: Path, batch_id: str) -> tuple[int, int]:
    from snowflake_pull.case_unit_state import CLAIMABLE_STATES, CaseUnitStateStore

    store = CaseUnitStateStore(out_dir / "case_units.sqlite")
    try:
        counts = store.counts_by_state(batch_id=batch_id)
        queued_units = sum(int(counts.get(s, 0)) for s in CLAIMABLE_STATES)
        rem_cases = sum(
            store.remaining_cases_by_facility(
                batch_id=batch_id, states=CLAIMABLE_STATES
            ).values()
        )
        return queued_units, int(rem_cases)
    finally:
        store.close()


def _read_health(out_dir: Path) -> dict:
    path = out_dir / "reports" / "health.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _append_babysit_log(reports: Path, row: dict) -> None:
    path = reports / "babysit_drain.jsonl"
    reports.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _utc(), **row}) + "\n")


def _append_restart_log(reports: Path, *, reason: str, pids: list[int]) -> None:
    """Append to worker_restart_log.md (P6 deliverable)."""
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "worker_restart_log.md"
    line = f"- {_utc()}: {reason} pids={pids}\n"
    if path.is_file():
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    else:
        path.write_text(
            "# Worker Restart Log\n\n" f"Generated: {_utc()}\n\n" + line,
            encoding="utf-8",
        )
    _append_babysit_log(
        reports, {"event": "restart_worker", "reason": reason, "pids": pids}
    )


def _run_final_validation(out_dir: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATION_SCRIPT),
            "--out-dir",
            str(out_dir),
            "--skip-download",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    payload: dict = {
        "exit_code": proc.returncode,
        "stdout_tail": (proc.stdout or "")[-4000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }
    # Prefer last JSON object from stdout
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and "production_ready" in line:
            try:
                payload["summary"] = json.loads(line)
                break
            except json.JSONDecodeError:
                pass
    if "summary" not in payload:
        try:
            # whole stdout may be one JSON blob
            payload["summary"] = json.loads(proc.stdout or "")
        except Exception:
            payload["summary"] = {}
    return payload


def _write_promote_gate(out_dir: Path, validation: dict, rem_cases: int) -> Path:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    summary = validation.get("summary") or {}
    ready = bool(summary.get("production_ready")) and rem_cases == 0
    recommendation = summary.get("recommendation") or (
        "Promote" if ready else "Continue"
    )
    if rem_cases > 0:
        ready = False
        recommendation = "Continue"
    gate = {
        "generated_at": _utc(),
        "queued_cases": rem_cases,
        "production_ready": ready,
        "recommendation": "Promote" if ready else recommendation,
        "promote_allowed": ready and recommendation == "Promote",
        "validation_exit_code": validation.get("exit_code"),
        "summary": summary,
        "rule": "Promote only if queue=0 + Production Validation PASS + no blockers",
    }
    path = reports / "promote_gate.json"
    path.write_text(json.dumps(gate, indent=2), encoding="utf-8")

    # Append short note to executive summary if present
    exec_path = reports / "executive_summary.md"
    note = (
        f"\n\n## Promote Gate — {gate['generated_at']}\n\n"
        f"- Queued cases: {rem_cases}\n"
        f"- Production Ready: {'YES' if ready else 'NO'}\n"
        f"- Recommendation: **{gate['recommendation']}**\n"
        f"- Promote allowed: {gate['promote_allowed']}\n"
    )
    if exec_path.is_file():
        with exec_path.open("a", encoding="utf-8") as f:
            f.write(note)
    else:
        exec_path.write_text("# Executive Summary\n" + note, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    ap.add_argument("--batch-id", default=BATCH_DEFAULT)
    ap.add_argument(
        "--poll-sec",
        type=float,
        default=15.0,
        help="Poll interval (plan: 15s; clamped 5–30s)",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Single status check + ensure worker; do not loop",
    )
    ap.add_argument(
        "--browser-workers",
        type=int,
        default=1,
        help="Ignored except to reject values != 1 (WebPT single-session)",
    )
    args = ap.parse_args()
    if int(args.browser_workers) != 1:
        print(
            json.dumps(
                {
                    "error": "WebPT single-session only: --browser-workers must be 1",
                    "got": args.browser_workers,
                }
            ),
            flush=True,
        )
        return 2
    out_dir = Path(args.out_dir)
    reports = out_dir / "reports"
    worker_log = reports / "worker_rate_control.log"

    while True:
        pids = _find_worker_pids()
        queued_units, rem_cases = _queue_remaining(out_dir, args.batch_id)
        health = _read_health(out_dir)

        status = {
            "worker_pids": pids,
            "queued_units": queued_units,
            "cases_remaining": rem_cases,
            "throttle_state": health.get("throttle_state"),
            "cph": health.get("speed_cases_per_hour"),
            "auth_status": health.get("auth_status"),
            "health_updated_at": health.get("updated_at"),
        }
        print(f"BABYSIT_STATUS {json.dumps(status)}", flush=True)
        _append_babysit_log(reports, status)

        from snowflake_pull.case_unit_state import CaseUnitStateStore

        store = CaseUnitStateStore(out_dir / "case_units.sqlite")
        try:
            in_prog = int(
                store.counts_by_state(batch_id=args.batch_id).get("in_progress", 0)
            )
        finally:
            store.close()

        if rem_cases == 0 and queued_units == 0 and in_prog == 0:
            print("BABYSIT_QUEUE_EMPTY starting final validation", flush=True)
            # Wait for worker to exit naturally if still winding down
            for _ in range(12):
                pids = _find_worker_pids()
                if not pids:
                    break
                time.sleep(10)
            validation = _run_final_validation(out_dir)
            _, rem_after = _queue_remaining(out_dir, args.batch_id)
            gate_path = _write_promote_gate(out_dir, validation, rem_after)
            result = {
                "validation": {
                    "exit_code": validation.get("exit_code"),
                    "summary": validation.get("summary"),
                },
                "promote_gate": str(gate_path),
                "cases_remaining": rem_after,
            }
            print(f"BABYSIT_DONE {json.dumps(result)}", flush=True)
            _append_babysit_log(reports, {"event": "done", **result})
            return 0 if (validation.get("summary") or {}).get("production_ready") else 1

        if not pids:
            print("BABYSIT_RESTART_WORKER no drain process found", flush=True)
            _append_restart_log(
                reports, reason="no_drain_process", pids=[]
            )
            _start_worker(out_dir, args.batch_id, worker_log)
        elif len(pids) > 1:
            # Do not kill — just warn; operator/plan forbids second start
            print(
                f"BABYSIT_WARN multiple_drain_pids={pids} (not starting another)",
                flush=True,
            )
            _append_restart_log(
                reports, reason="multiple_pids_no_start", pids=pids
            )

        if args.once:
            return 0
        # Fast recovery: poll ≤30s (never slower than plan)
        time.sleep(min(30.0, max(5.0, float(args.poll_sec))))


if __name__ == "__main__":
    raise SystemExit(main())
