"""Track C production wave: chunked download (250), checkpoint, health gate.

Download-only between chunks. Extract once + E2E once at the end.
Does not promote live REC.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import finish_run, load_gate, resume_run  # noqa: E402
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402
from snowflake_pull.scripts.run_gap_batches import (  # noqa: E402
    _build_patients_csv,
    _dos_in_extracted,
)

BATCH_ID = "gap_dos_after_last_note"
EXPORT = _REPO / "webpt_edco_scraper/output/jun_jul_2026/patients_export_273d.csv"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_checkpoint(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "wave_id": "",
        "chunk_index": 0,
        "chunk_size": 250,
        "last_downloaded_unit_id": "",
        "downloaded_unit_ids": [],
        "failed_unit_ids": [],
        "stats": {
            "attempted": 0,
            "download_ok": 0,
            "download_fail": 0,
            "auth_failures": 0,
            "reauth_count": 0,
        },
        "updated_at": "",
    }


def _save_checkpoint(path: Path, ck: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ck["updated_at"] = _utc()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ck, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sort_patients_csv(path: Path) -> None:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
        if not rows:
            return
        fields = list(rows[0].keys())
    rows.sort(
        key=lambda r: (
            (r.get("facility_id") or "").strip(),
            (r.get("patient_id") or "").strip(),
        )
    )
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _patient_has_chart_notes(edocs: Path, emr: str) -> bool:
    chart = edocs / emr / "chart_notes"
    if not chart.is_dir():
        return False
    return any(chart.glob("*.pdf"))


def _read_heartbeat(run_dir: Path) -> dict:
    path = run_dir / "monitoring" / "heartbeat.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _health_gate(
    *,
    chunk_auth_failures: int,
    chunk_attempted: int,
    chunk_fail: int,
    chunk_elapsed_s: float,
    hb: dict,
    auth_fail_threshold: int = 3,
    retry_rate_threshold: float = 0.35,
    min_throughput: float = 1.0,
) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means pause+reauth."""
    if chunk_auth_failures >= auth_fail_threshold:
        return False, f"auth_failures={chunk_auth_failures}"
    if chunk_attempted > 0:
        fail_rate = chunk_fail / chunk_attempted
        if fail_rate >= retry_rate_threshold:
            return False, f"chunk_fail_rate={fail_rate:.3f}"
    if chunk_attempted >= 50 and chunk_elapsed_s > 0:
        tpm = (chunk_attempted / chunk_elapsed_s) * 60.0
        if tpm < min_throughput:
            return False, f"throughput_units_per_min={tpm:.2f}"
    # Do not use workers_stalled — long parallel-download chunks idle the parent
    # obs success clock by design.
    return True, "ok"


def _reauth_webpt() -> bool:
    """Fresh login via scraper auth stack; returns True on success."""
    scraper = str(_SCRAPER).replace("\\", "\\\\")
    env_path = str(_SCRAPER / ".env").replace("\\", "\\\\")
    script = f"""
import asyncio, sys
from dotenv import load_dotenv
sys.path.insert(0, r"{scraper}")
load_dotenv(r"{env_path}", override=False)
from auth import create_context, ensure_authenticated
from config import WebPTConfig
from playwright.async_api import async_playwright

async def main():
    config = WebPTConfig.from_env()
    if not config.username or not config.password:
        raise SystemExit("missing creds")
    async with async_playwright() as pw:
        context = await create_context(pw, config)
        page = await context.new_page()
        try:
            await ensure_authenticated(
                page, context, config, allow_oust=True, fresh_login=True
            )
        finally:
            await context.close()

asyncio.run(main())
print("REAUTH_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_SCRAPER),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and "REAUTH_OK" in (proc.stdout or "")


def _claim_chunk(store, batch_id: str, n: int, skip_ids: set[str]):
    claimed = []
    while len(claimed) < n:
        unit = store.claim_next(batch_id=batch_id)
        if unit is None:
            break
        if unit.unit_id in skip_ids:
            try:
                store.transition(unit.unit_id, "done", force=True)
            except Exception:
                store.transition(
                    unit.unit_id, "failed_terminal", error_type="skip_dup"
                )
            continue
        claimed.append(unit)
    return claimed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--batch-id", default=BATCH_ID)
    p.add_argument("--chunk-size", type=int, default=250)
    p.add_argument("--early-stop-rate", type=float, default=0.15)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument(
        "--skip-enqueue",
        action="store_true",
        help="Do not re-run build_sf_note_gap_list",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Stop after download chunks (no extract/E2E)",
    )
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download loop; run extract+E2E on existing edocs",
    )
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_track_c_wave.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("track_c_wave")
    run.obs.online = True
    # Chunk downloads of 250 patients routinely exceed default 120s/600s stalls.
    run.obs.stall_seconds = 1800.0
    run.obs.stall_abort_seconds = 7200.0
    run.obs.start_heartbeat()
    run.obs.set_batch(args.batch_id)
    run.obs.mark_success(operation="wave_start")

    p2b = load_gate(run.run_dir, "P2b")
    if not p2b or p2b.get("pass") is not True:
        payload = {"blocked": True, "reason": "P2b_not_passed"}
        print(json.dumps(payload, indent=2))
        finish_run(run, status="track_c_wave_blocked")
        set_global_obs(None)
        return 2

    if not args.skip_enqueue:
        enq = subprocess.run(
            [
                sys.executable,
                str(_REPO / "snowflake_pull/scripts/build_sf_note_gap_list.py"),
                "--run-id",
                run.run_id,
                "--subtype",
                "dos_after_last_note",
                "--enqueue",
            ],
            cwd=str(_REPO),
        )
        if enq.returncode != 0:
            raise SystemExit(f"enqueue failed rc={enq.returncode}")

    batch_dir = run.artifacts / "gap_batches" / args.batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    edocs = batch_dir / "edocs"
    extracted = batch_dir / "extracted"
    ck_path = batch_dir / "wave_checkpoint.json"
    ck = _load_checkpoint(ck_path)
    if not ck.get("wave_id"):
        ck["wave_id"] = (
            f"wave_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
    ck["chunk_size"] = args.chunk_size
    skip_ids = set(ck.get("downloaded_unit_ids") or []) | set(
        ck.get("failed_unit_ids") or []
    )

    run.store.reclaim_stale_in_progress(60.0)

    consecutive_reauth_fails = 0
    wave_download_ok = int(ck["stats"].get("download_ok") or 0)
    wave_download_fail = int(ck["stats"].get("download_fail") or 0)

    if not args.skip_download:
        while True:
            if run.obs.abort_requested():
                run.obs.emit(
                    "decision",
                    level="ERROR",
                    operation="wave",
                    decision="stall_abort",
                    decision_reason="abort_requested_between_chunks",
                )
                break
            claimed = _claim_chunk(
                run.store, args.batch_id, args.chunk_size, skip_ids
            )
            if not claimed:
                run.obs.emit(
                    "decision",
                    operation="wave",
                    decision="queue_drained",
                    decision_reason="no_more_queued_units",
                )
                break

            ck["chunk_index"] = int(ck.get("chunk_index") or 0) + 1
            chunk_idx = ck["chunk_index"]
            patients_csv = batch_dir / f"patients_chunk_{chunk_idx:04d}.csv"
            n_pat = _build_patients_csv(claimed, EXPORT, patients_csv)
            _sort_patients_csv(patients_csv)

            run.obs.emit(
                "decision",
                operation="wave_chunk",
                decision="start_download_chunk",
                extra={
                    "batch_id": args.batch_id,
                    "chunk_index": chunk_idx,
                    "units": len(claimed),
                    "patients": n_pat,
                },
            )
            run.obs.mark_success(
                operation="wave_chunk_start",
                facility_id=str(chunk_idx),
            )

            chunk_t0 = time.perf_counter()
            chunk_auth_fail = 0
            chunk_ok = 0
            chunk_fail = 0

            if n_pat == 0:
                for u in claimed:
                    run.store.transition(
                        u.unit_id, "failed_terminal", error_type="PatientNotInWebPT"
                    )
                    ck.setdefault("failed_unit_ids", []).append(u.unit_id)
                    skip_ids.add(u.unit_id)
                    chunk_fail += 1
                    wave_download_fail += 1
            else:
                dl_cmd = [
                    sys.executable,
                    str(_SCRAPER / "scraper.py"),
                    "parallel-download",
                    "--input",
                    str(patients_csv),
                    "--output",
                    str(batch_dir),
                    "--skip-edocs",
                    "--max-patients",
                    str(n_pat),
                ]
                # Parent unit-success clock idle during long child downloads;
                # disable online stall so 30–120m chunks are not abort-flagged.
                run.obs.online = False
                try:
                    proc = subprocess.run(dl_cmd, cwd=str(_SCRAPER))
                finally:
                    run.obs.online = True
                    run.obs.mark_success(
                        operation="wave_chunk_download_done",
                        facility_id=str(chunk_idx),
                    )
                if proc.returncode != 0:
                    chunk_auth_fail += 1
                    ck["stats"]["auth_failures"] = (
                        int(ck["stats"].get("auth_failures") or 0) + 1
                    )
                    run.obs.emit(
                        "error",
                        level="ERROR",
                        operation="wave_chunk",
                        outcome="fail",
                        error_type="AuthExpired",
                        decision_reason=f"parallel_download_rc={proc.returncode}",
                    )

                for u in claimed:
                    emr = (u.webpt_patient_id or u.emr_id or "").strip()
                    ok = bool(emr) and _patient_has_chart_notes(edocs, emr)
                    if ok:
                        run.store.transition(u.unit_id, "downloaded")
                        ck.setdefault("downloaded_unit_ids", []).append(u.unit_id)
                        ck["last_downloaded_unit_id"] = u.unit_id
                        skip_ids.add(u.unit_id)
                        chunk_ok += 1
                        wave_download_ok += 1
                        run.obs.mark_success(
                            operation="wave_unit_downloaded",
                            emr_id=emr,
                            dos=u.dos,
                            facility_id=u.facility_id,
                        )
                    else:
                        run.store.transition(
                            u.unit_id,
                            "failed_terminal",
                            error_type="DownloadEmpty",
                        )
                        ck.setdefault("failed_unit_ids", []).append(u.unit_id)
                        skip_ids.add(u.unit_id)
                        chunk_fail += 1
                        wave_download_fail += 1

            elapsed = time.perf_counter() - chunk_t0
            ck["stats"]["attempted"] = int(ck["stats"].get("attempted") or 0) + len(
                claimed
            )
            ck["stats"]["download_ok"] = wave_download_ok
            ck["stats"]["download_fail"] = wave_download_fail
            _save_checkpoint(ck_path, ck)

            run.obs.set_progress(
                completed=wave_download_ok + wave_download_fail,
                remaining=max(run.store.counts_by_state().get("queued", 0), 0),
            )
            run.obs.mark_checkpoint()

            attempted_chunk = len(claimed)
            if attempted_chunk >= 20:
                ok_rate = chunk_ok / attempted_chunk
                if ok_rate < args.early_stop_rate:
                    run.obs.emit(
                        "decision",
                        level="WARN",
                        operation="wave",
                        decision="early_stop_download",
                        decision_reason=f"chunk_ok_rate={ok_rate:.3f}",
                    )
                    break

            hb = _read_heartbeat(run.run_dir)
            healthy, reason = _health_gate(
                chunk_auth_failures=chunk_auth_fail,
                chunk_attempted=attempted_chunk,
                chunk_fail=chunk_fail,
                chunk_elapsed_s=elapsed,
                hb=hb,
            )
            if not healthy:
                run.obs.emit(
                    "decision",
                    level="WARN",
                    operation="health_gate",
                    decision="pause_reauth",
                    decision_reason=reason,
                )
                ok_reauth = _reauth_webpt()
                ck["stats"]["reauth_count"] = (
                    int(ck["stats"].get("reauth_count") or 0) + 1
                )
                _save_checkpoint(ck_path, ck)
                if ok_reauth:
                    consecutive_reauth_fails = 0
                    run.obs.set_auth_healthy(True)
                    run.obs.emit(
                        "decision",
                        operation="health_gate",
                        decision="reauth_ok_continue",
                    )
                else:
                    consecutive_reauth_fails += 1
                    run.obs.set_auth_healthy(False)
                    run.obs.emit(
                        "error",
                        level="ERROR",
                        operation="health_gate",
                        outcome="fail",
                        error_type="AuthExpired",
                        decision_reason="reauth_failed",
                    )
                    if consecutive_reauth_fails >= 2:
                        run.obs.emit(
                            "decision",
                            level="ERROR",
                            operation="wave",
                            decision="hard_abort_reauth",
                        )
                        break

    if args.download_only:
        summary = {
            "run_id": run.run_id,
            "download_only": True,
            "wave_id": ck.get("wave_id"),
            "chunks": ck.get("chunk_index"),
            "download_ok": wave_download_ok,
            "download_fail": wave_download_fail,
            "checkpoint": str(ck_path),
        }
        (run.run_dir / "summaries" / "track_c_wave_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, indent=2))
        finish_run(run, status="track_c_wave_download_done")
        set_global_obs(None)
        return 0

    # --- Extract once ---
    pdf_n = len(list(edocs.rglob("*.pdf"))) if edocs.is_dir() else 0
    run.obs.emit(
        "decision",
        operation="wave_extract",
        decision="start_extract_once",
        extra={"pdf_n": pdf_n},
    )
    if pdf_n == 0:
        raise SystemExit("no PDFs in batch edocs — cannot extract")
    extracted.mkdir(parents=True, exist_ok=True)
    ex = subprocess.run(
        [
            sys.executable,
            str(_SCRAPER / "scraper.py"),
            "extract-daily-notes",
            "--input",
            str(edocs),
            "--output-dir",
            str(extracted),
        ],
        cwd=str(_SCRAPER),
    )
    if ex.returncode != 0:
        raise SystemExit(f"extract failed rc={ex.returncode}")

    downloaded = [
        u
        for u in run.store.units_in_states(["downloaded"])
        if u.batch_id == args.batch_id
    ]
    dos_ok = 0
    dos_fail = 0
    for u in downloaded:
        emr = (u.webpt_patient_id or u.emr_id or "").strip()
        if _dos_in_extracted(extracted, emr, u.dos or ""):
            run.store.transition(u.unit_id, "extracted")
            run.store.transition(u.unit_id, "reconciled")
            run.store.transition(u.unit_id, "done")
            dos_ok += 1
        else:
            run.store.transition(
                u.unit_id,
                "failed_terminal",
                error_type="NoteDosAbsentAfterDownload",
            )
            dos_fail += 1
            if u.unit_id not in (ck.get("failed_unit_ids") or []):
                ck.setdefault("failed_unit_ids", []).append(u.unit_id)

    _save_checkpoint(ck_path, ck)

    attempted_dos = dos_ok + dos_fail
    if attempted_dos >= 20:
        rate = dos_ok / attempted_dos
        if rate < args.early_stop_rate:
            run.obs.emit(
                "decision",
                level="WARN",
                operation="wave",
                decision="early_stop_dos_rate",
                decision_reason=f"dos_rate={rate:.3f}",
            )

    e2e = subprocess.run(
        [
            sys.executable,
            str(_REPO / "snowflake_pull/scripts/validate_track_c_e2e.py"),
            "--run-id",
            run.run_id,
            "--batch-id",
            args.batch_id,
        ],
        cwd=str(_REPO),
    )

    summary = {
        "run_id": run.run_id,
        "wave_id": ck.get("wave_id"),
        "chunks": ck.get("chunk_index"),
        "download_ok": ck["stats"].get("download_ok"),
        "download_fail": ck["stats"].get("download_fail"),
        "dos_ok_after_extract": dos_ok,
        "dos_fail_after_extract": dos_fail,
        "counts_by_state": run.store.counts_by_state(),
        "e2e_rc": e2e.returncode,
        "checkpoint": str(ck_path),
        "ts": utc_now_iso(),
    }
    (run.run_dir / "summaries" / "track_c_wave_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    run.obs.stage_end(
        "track_c_wave",
        chunks=summary.get("chunks"),
        dos_ok_after_extract=dos_ok,
        e2e_rc=e2e.returncode,
    )
    finish_run(run, status="track_c_wave_done")
    set_global_obs(None)
    return 0 if e2e.returncode in (0, 2) else e2e.returncode


if __name__ == "__main__":
    raise SystemExit(main())
