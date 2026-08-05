"""Track D: dos_before_first_note recovery (historical chart-note download).

Phase A validate from remaining_gap → enqueue FSM → chunked download → extract
→ merge onto side-by-side (seed=side, preserve Track F) → reconcile → E2E

OBSOLETE for Case integrity windows — use run_case_pipeline / build_case_schedule.
Patient-first _build_patients_csv is Case-unsafe (see artifacts/CASE_PIPELINE.md).
→ historical yield → refresh remaining_gap.

Does not promote live REC.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from snowflake_pull.coverage_run import finish_run, load_gate, resume_run  # noqa: E402
from snowflake_pull.facility_map import WEBPT_FACILITIES, map_sf_clinic  # noqa: E402
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402
from snowflake_pull.scripts.run_gap_batches import (  # noqa: E402
    _build_patients_csv,
    _dos_in_extracted,
)
from snowflake_pull.scripts.run_track_c_wave import (  # noqa: E402
    _claim_chunk,
    _health_gate,
    _load_checkpoint,
    _patient_has_chart_notes,
    _read_heartbeat,
    _reauth_webpt,
    _save_checkpoint,
    _sort_patients_csv,
)
from snowflake_pull.scripts.validate_track_c_e2e import (  # noqa: E402
    _compare_visits,
    _cpt_keys_by_emr_dos,
    _find_pdf,
    _load_recovered_units,
    _merge_extracted,
    _note_keys_by_emr_dos,
    _rec_keys,
    _run_side_reconcile,
)

BATCH_ID = "gap_dos_before_first_note"
SUBTYPE = "dos_before_first_note"
PRIORITY = 30
EXPORT = _REPO / "webpt_edco_scraper/output/jun_jul_2026/patients_export_273d.csv"
BASE = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
PLANNING_YIELD_D = 0.55
WIN_START = "2026-06-01"
WIN_END = "2026-07-31"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _min_note_by_emr(notes_rows: list[dict[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in notes_rows:
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        if not pid or not dos:
            continue
        prev = out.get(pid)
        if prev is None or dos < prev:
            out[pid] = dos
    return out


def _export_patients(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in _read_csv(path):
        pid = (row.get("patient_id") or "").strip()
        if pid and pid not in out:
            out[pid] = row
    return out


def _done_keys_other_batches(db_path: Path, batch_id: str) -> set[tuple[str, str]]:
    if not db_path.is_file():
        return set()
    con = sqlite3.connect(str(db_path))
    rows = con.execute(
        """
        SELECT COALESCE(NULLIF(webpt_patient_id,''), emr_id) AS emr, dos
        FROM units
        WHERE state='done' AND batch_id != ? AND batch_id != ''
        """,
        (batch_id,),
    ).fetchall()
    con.close()
    return {
        ((emr or "").strip(), (dos or "")[:10])
        for emr, dos in rows
        if emr and dos
    }


def _write_chart_notes_inventory(edocs: Path, out_csv: Path) -> int:
    rows: list[dict[str, str]] = []
    if edocs.is_dir():
        for pdf in sorted(edocs.rglob("chart_notes/*.pdf")):
            emr = pdf.parent.parent.name
            rows.append(
                {
                    "patient_id": emr,
                    "filename": pdf.name,
                    "pdf_path": str(pdf),
                    "relative_path": str(pdf.relative_to(edocs)).replace("\\", "/"),
                }
            )
    _write_csv(
        out_csv,
        rows,
        ["patient_id", "filename", "pdf_path", "relative_path"],
    )
    return len(rows)


def _phase_a_validate(
    *,
    gap_csv: Path,
    side_rec_path: Path,
    side_notes: list[dict[str, str]],
    live_notes: list[dict[str, str]],
    export_by_pid: dict[str, dict[str, str]],
    done_other: set[tuple[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    # Classification "first note" is from extracted corpus — side overrides live.
    min_for_check = dict(_min_note_by_emr(live_notes))
    min_for_check.update(_min_note_by_emr(side_notes))

    rec_keys = _rec_keys(side_rec_path)
    validated: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []

    with gap_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("root_cause") or "") != SUBTYPE:
                continue
            emr = (row.get("emr_id") or "").strip()
            dos = (row.get("date_of_service") or "")[:10]
            clinic = (row.get("sf_clinic") or "").strip()
            fac_id = (row.get("webpt_facility_id") or "").strip()
            base_out = {
                "emr_id": emr,
                "date_of_service": dos,
                "sf_patient": (row.get("sf_patient") or "").strip(),
                "sf_clinic": clinic,
                "webpt_facility_id": fac_id,
                "sf_status": (row.get("sf_status") or "").strip(),
            }
            if not emr or not dos:
                rejected.append({**base_out, "reject_reason": "incomplete_key"})
                continue
            if (emr, dos) in rec_keys:
                rejected.append({**base_out, "reject_reason": "already_in_side_rec"})
                continue
            if emr not in export_by_pid:
                rejected.append({**base_out, "reject_reason": "patient_not_in_export"})
                continue
            m = map_sf_clinic(clinic) if clinic else None
            if m is not None and m.status not in {"unmapped", "out_of_scope"} and m.webpt_facility_id:
                fac_id = m.webpt_facility_id
                fac_name = m.webpt_facility_name or clinic
            elif fac_id and fac_id in WEBPT_FACILITIES:
                fac_name = WEBPT_FACILITIES[fac_id]
            else:
                rejected.append({**base_out, "reject_reason": "facility_unsupported"})
                continue

            first = min_for_check.get(emr)
            if first is not None and dos >= first:
                rejected.append(
                    {
                        **base_out,
                        "reject_reason": "classification_false_positive",
                        "min_note_date": first,
                    }
                )
                continue
            if (emr, dos) in done_other:
                rejected.append(
                    {**base_out, "reject_reason": "already_recovered_other_track"}
                )
                continue

            validated.append(
                {
                    **base_out,
                    "webpt_facility_id": fac_id,
                    "facility_name": fac_name,
                    "min_note_date": first or "",
                    "unit_id": f"{fac_id}:{emr}:{dos}",
                }
            )
    return validated, rejected


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--chunk-size", type=int, default=250)
    p.add_argument("--early-stop-rate", type=float, default=0.15)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument(
        "--skip-phase-a",
        action="store_true",
        help="Reuse existing validated_candidates.csv and skip FSM enqueue",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Stop after download chunks",
    )
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download; extract+E2E on existing batch edocs",
    )
    p.add_argument(
        "--skip-reconcile",
        action="store_true",
        help="Skip side reconcile (debug)",
    )
    p.add_argument(
        "--skip-classify",
        action="store_true",
        help="Skip remaining_gap refresh",
    )
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_track_d.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("track_d")
    run.obs.online = True
    run.obs.stall_seconds = 1800.0
    run.obs.stall_abort_seconds = 7200.0
    run.obs.start_heartbeat()
    run.obs.set_batch(BATCH_ID)
    run.obs.mark_success(operation="track_d_start")

    out_dir = run.artifacts / "track_d"
    out_dir.mkdir(parents=True, exist_ok=True)
    batch_dir = run.artifacts / "gap_batches" / BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    edocs = batch_dir / "edocs"
    batch_extracted = batch_dir / "extracted"
    track_extracted = out_dir / "extracted"

    p2b = load_gate(run.run_dir, "P2b")
    if not p2b or p2b.get("pass") is not True:
        payload = {"blocked": True, "reason": "P2b_not_passed", "track": "D"}
        print(json.dumps(payload, indent=2))
        finish_run(run, status="track_d_blocked")
        set_global_obs(None)
        return 2

    side_extracted = run.side_by_side / "extracted"
    side_recon = run.side_by_side / "reconciliation"
    side_rec_path = side_recon / "reconciliation_visits.csv"
    if not side_rec_path.is_file():
        raise SystemExit(f"side REC missing: {side_rec_path}")

    # --- Phase A ---
    if args.skip_phase_a:
        validated = _read_csv(out_dir / "validated_candidates.csv")
        rejected = _read_csv(out_dir / "rejected_candidates.csv")
        enqueued = 0
    else:
        gap_csv = run.artifacts / "remaining_gap" / "remaining_gap_breakdown.csv"
        if not gap_csv.is_file():
            raise SystemExit(f"missing {gap_csv}")
        export_by_pid = _export_patients(EXPORT)
        done_other = _done_keys_other_batches(
            run.run_dir / "state" / "units.sqlite", BATCH_ID
        )
        validated, rejected = _phase_a_validate(
            gap_csv=gap_csv,
            side_rec_path=side_rec_path,
            side_notes=_read_csv(side_extracted / "daily_notes.csv"),
            live_notes=_read_csv(BASE / "extracted" / "daily_notes.csv"),
            export_by_pid=export_by_pid,
            done_other=done_other,
        )
        v_fields = [
            "emr_id",
            "date_of_service",
            "sf_patient",
            "sf_clinic",
            "webpt_facility_id",
            "facility_name",
            "sf_status",
            "min_note_date",
            "unit_id",
        ]
        r_fields = [
            "emr_id",
            "date_of_service",
            "sf_patient",
            "sf_clinic",
            "webpt_facility_id",
            "sf_status",
            "reject_reason",
            "min_note_date",
        ]
        _write_csv(out_dir / "validated_candidates.csv", validated, v_fields)
        _write_csv(out_dir / "rejected_candidates.csv", rejected, r_fields)

        upsert_rows = []
        for c in validated:
            upsert_rows.append(
                {
                    "unit_id": c["unit_id"],
                    "priority": PRIORITY,
                    "batch_id": BATCH_ID,
                    "facility_id": c["webpt_facility_id"],
                    "facility_name": c.get("facility_name") or c.get("sf_clinic") or "",
                    "webpt_patient_id": c["emr_id"],
                    "emr_id": c["emr_id"],
                    "dos": c["date_of_service"],
                    "visit_status": c.get("sf_status") or "",
                    "patient_name": c.get("sf_patient") or "",
                    "extra_json": json.dumps({"subtype": SUBTYPE, "track": "D"}),
                }
            )
        enqueued = run.store.upsert_units(upsert_rows) if upsert_rows else 0

    phase_a_summary = {
        "validated": len(validated),
        "rejected": len(rejected),
        "enqueued_new": enqueued,
        "reject_reasons": dict(Counter(r.get("reject_reason") or "" for r in rejected)),
    }
    (out_dir / "phase_a_summary.json").write_text(
        json.dumps(phase_a_summary, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.emit(
        "decision",
        operation="track_d_phase_a",
        decision="validate_complete",
        extra=phase_a_summary,
    )

    if not validated and not args.skip_download:
        raise SystemExit("Phase A produced 0 validated candidates")

    # Snapshot gap before Track D (side REC current state)
    before_summary = _compare_visits(side_rec_path, out_dir / "sf_compare_before")
    sf_missing_before = int(
        (before_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )

    # --- Phase B: download ---
    ck_path = batch_dir / "wave_checkpoint.json"
    ck = _load_checkpoint(ck_path)
    if not ck.get("wave_id"):
        ck["wave_id"] = f"track_d_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    ck["chunk_size"] = args.chunk_size
    skip_ids = set(ck.get("downloaded_unit_ids") or []) | set(
        ck.get("failed_unit_ids") or []
    )
    run.store.reclaim_stale_in_progress(60.0)

    consecutive_reauth_fails = 0
    wave_download_ok = int(ck["stats"].get("download_ok") or 0)
    wave_download_fail = int(ck["stats"].get("download_fail") or 0)
    early_stopped = False

    if not args.skip_download:
        while True:
            if run.obs.abort_requested():
                run.obs.emit(
                    "decision",
                    level="ERROR",
                    operation="track_d_wave",
                    decision="stall_abort",
                )
                break
            claimed = _claim_chunk(run.store, BATCH_ID, args.chunk_size, skip_ids)
            if not claimed:
                run.obs.emit(
                    "decision",
                    operation="track_d_wave",
                    decision="queue_drained",
                )
                break

            ck["chunk_index"] = int(ck.get("chunk_index") or 0) + 1
            chunk_idx = ck["chunk_index"]
            patients_csv = batch_dir / f"patients_chunk_{chunk_idx:04d}.csv"
            n_pat = _build_patients_csv(claimed, EXPORT, patients_csv)
            _sort_patients_csv(patients_csv)

            run.obs.emit(
                "decision",
                operation="track_d_chunk",
                decision="start_download_chunk",
                extra={
                    "chunk_index": chunk_idx,
                    "units": len(claimed),
                    "patients": n_pat,
                },
            )
            run.obs.mark_success(
                operation="track_d_chunk_start", facility_id=str(chunk_idx)
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
                run.obs.online = False
                try:
                    proc = subprocess.run(dl_cmd, cwd=str(_SCRAPER))
                finally:
                    run.obs.online = True
                    run.obs.mark_success(
                        operation="track_d_chunk_download_done",
                        facility_id=str(chunk_idx),
                    )
                if proc.returncode != 0:
                    chunk_auth_fail += 1
                    ck["stats"]["auth_failures"] = (
                        int(ck["stats"].get("auth_failures") or 0) + 1
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
            healthy, reason = _health_gate(
                chunk_auth_failures=chunk_auth_fail,
                chunk_attempted=attempted_chunk,
                chunk_fail=chunk_fail,
                chunk_elapsed_s=elapsed,
                hb=_read_heartbeat(run.run_dir),
            )
            # Low ok-rate often means auth death — reauth before early-stop.
            ok_rate = (chunk_ok / attempted_chunk) if attempted_chunk else 1.0
            if (
                not healthy
                or (
                    attempted_chunk >= 20
                    and args.early_stop_rate > 0
                    and ok_rate < args.early_stop_rate
                )
            ):
                run.obs.emit(
                    "decision",
                    level="WARN",
                    operation="health_gate",
                    decision="pause_reauth",
                    decision_reason=reason
                    if not healthy
                    else f"chunk_ok_rate={ok_rate:.3f}",
                )
                ok_reauth = _reauth_webpt()
                ck["stats"]["reauth_count"] = (
                    int(ck["stats"].get("reauth_count") or 0) + 1
                )
                _save_checkpoint(ck_path, ck)
                if ok_reauth:
                    consecutive_reauth_fails = 0
                    run.obs.set_auth_healthy(True)
                    # Requeue this chunk's DownloadEmpty failures for retry
                    for u in claimed:
                        if u.unit_id in (ck.get("failed_unit_ids") or []):
                            try:
                                run.store.transition(
                                    u.unit_id, "queued", force=True
                                )
                            except Exception:
                                pass
                            try:
                                ck["failed_unit_ids"].remove(u.unit_id)
                            except ValueError:
                                pass
                            skip_ids.discard(u.unit_id)
                    continue
                consecutive_reauth_fails += 1
                run.obs.set_auth_healthy(False)
                if consecutive_reauth_fails >= 2:
                    early_stopped = True
                    run.obs.emit(
                        "decision",
                        level="ERROR",
                        operation="track_d_wave",
                        decision="hard_abort_reauth",
                    )
                    break
                if (
                    attempted_chunk >= 20
                    and args.early_stop_rate > 0
                    and ok_rate < args.early_stop_rate
                ):
                    early_stopped = True
                    run.obs.emit(
                        "decision",
                        level="WARN",
                        operation="track_d_wave",
                        decision="early_stop_download",
                        decision_reason=f"chunk_ok_rate={ok_rate:.3f}",
                    )
                    break

    download_summary = {
        "run_id": run.run_id,
        "track": "D",
        "batch_id": BATCH_ID,
        "wave_id": ck.get("wave_id"),
        "chunks": ck.get("chunk_index"),
        "download_ok": wave_download_ok,
        "download_fail": wave_download_fail,
        "auth_failures": ck["stats"].get("auth_failures"),
        "reauth_count": ck["stats"].get("reauth_count"),
        "early_stopped": early_stopped,
        "checkpoint": str(ck_path),
        "ts": utc_now_iso(),
    }
    (out_dir / "download_summary.json").write_text(
        json.dumps(download_summary, indent=2) + "\n", encoding="utf-8"
    )

    if args.download_only:
        (run.run_dir / "summaries" / "track_d_summary.json").write_text(
            json.dumps(
                {**download_summary, "download_only": True, "promote_blocked": True},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps(download_summary, indent=2))
        finish_run(run, status="track_d_download_done")
        set_global_obs(None)
        return 0

    # --- Phase C: extract ---
    pdf_n = len(list(edocs.rglob("*.pdf"))) if edocs.is_dir() else 0
    if pdf_n == 0:
        raise SystemExit("no PDFs in batch edocs — cannot extract")
    batch_extracted.mkdir(parents=True, exist_ok=True)
    ex = subprocess.run(
        [
            sys.executable,
            str(_SCRAPER / "scraper.py"),
            "extract-daily-notes",
            "--input",
            str(edocs),
            "--output-dir",
            str(batch_extracted),
        ],
        cwd=str(_SCRAPER),
    )
    if ex.returncode != 0:
        raise SystemExit(f"extract failed rc={ex.returncode}")

    track_extracted.mkdir(parents=True, exist_ok=True)
    for name in ("daily_notes.csv", "cpt_codes.csv"):
        src = batch_extracted / name
        if src.is_file():
            shutil.copy2(src, track_extracted / name)
    chart_n = _write_chart_notes_inventory(
        edocs, track_extracted / "chart_notes.csv"
    )

    downloaded = [
        u
        for u in run.store.units_in_states(["downloaded"])
        if u.batch_id == BATCH_ID
    ]
    dos_ok = 0
    dos_fail = 0
    for u in downloaded:
        emr = (u.webpt_patient_id or u.emr_id or "").strip()
        if _dos_in_extracted(batch_extracted, emr, u.dos or ""):
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

    batch_notes = _read_csv(batch_extracted / "daily_notes.csv")
    batch_cpt = _read_csv(batch_extracted / "cpt_codes.csv")
    discovery = {
        "run_id": run.run_id,
        "track": "D",
        "units_download_ok": wave_download_ok,
        "units_download_fail": wave_download_fail,
        "pdf_count": pdf_n,
        "chart_notes_inventory_rows": chart_n,
        "unique_patients_with_pdf": len(
            {p.parent.parent.name for p in edocs.rglob("chart_notes/*.pdf")}
        )
        if edocs.is_dir()
        else 0,
        "extracted_daily_note_rows": len(batch_notes),
        "extracted_cpt_rows": len(batch_cpt),
        "dos_ok_after_extract": dos_ok,
        "dos_fail_after_extract": dos_fail,
        "ts": utc_now_iso(),
    }
    (out_dir / "historical_note_discovery.json").write_text(
        json.dumps(discovery, indent=2) + "\n", encoding="utf-8"
    )

    # --- Phase D: merge seed=side ---
    merge_stats = _merge_extracted(side_extracted, batch_extracted, seed="side")
    (out_dir / "merged_summary.json").write_text(
        json.dumps(merge_stats, indent=2) + "\n", encoding="utf-8"
    )

    # --- Phase E: reconcile ---
    if args.skip_reconcile and side_rec_path.is_file():
        recon_summary: dict[str, Any] = {"skipped": True, "path": str(side_rec_path)}
    else:
        recon_summary = _run_side_reconcile(side_extracted, side_recon)
    if not side_rec_path.is_file():
        raise SystemExit(f"side REC not produced: {side_rec_path}")

    # Backup prior sf_compare_after (Track F) before overwrite
    validation = run.artifacts / "validation"
    after_dir = validation / "sf_compare_after"
    bak = validation / "sf_compare_after_track_f"
    if after_dir.is_dir() and not bak.is_dir():
        shutil.copytree(after_dir, bak)

    # --- Phase F–G: acceptance + coverage + integrity ---
    recovered_units = _load_recovered_units(
        run.run_dir / "state" / "units.sqlite", BATCH_ID
    )
    baseline_rec = run.baseline / "reconciliation_visits.csv"
    baseline_keys = _rec_keys(baseline_rec)
    side_keys = _rec_keys(side_rec_path)

    def _in_window(key: tuple[str, str]) -> bool:
        dos = key[1]
        return bool(dos) and WIN_START <= dos <= WIN_END

    baseline_keys_win = {k for k in baseline_keys if _in_window(k)}
    side_keys_win = {k for k in side_keys if _in_window(k)}
    side_notes = _read_csv(side_extracted / "daily_notes.csv")
    side_cpt = _read_csv(side_extracted / "cpt_codes.csv")
    note_emr_dos = _note_keys_by_emr_dos(side_notes)
    cpt_emr_dos = _cpt_keys_by_emr_dos(side_cpt)
    batch_note_emr_dos = _note_keys_by_emr_dos(batch_notes)

    new_rec_keys = side_keys_win - baseline_keys_win
    removed_rec_keys = baseline_keys_win - side_keys_win

    acceptance_rows: list[dict[str, str]] = []
    reason_counts: Counter[str] = Counter()
    accepted = 0
    accepted_new = 0
    rejected_n = 0

    for u in recovered_units:
        emr = (u.get("webpt_patient_id") or u.get("emr_id") or "").strip()
        dos = (u.get("dos") or "")[:10]
        key = (emr, dos)
        in_side = key in side_keys
        in_base = key in baseline_keys
        in_notes = key in note_emr_dos
        in_cpt = key in cpt_emr_dos
        in_batch_notes = key in batch_note_emr_dos
        pdf = _find_pdf(edocs, emr, dos) if emr else None

        if in_base and in_side:
            outcome = "already_in_baseline_rec"
            reason = "already_in_baseline_rec"
            accepted += 1
        elif in_side and not in_base:
            outcome = "accepted_new"
            reason = "accepted_into_side_rec"
            accepted += 1
            accepted_new += 1
        else:
            rejected_n += 1
            outcome = "rejected"
            if not in_notes and not in_batch_notes:
                reason = "note_missing_after_merge"
            elif in_notes and not in_cpt:
                reason = "cpt_missing_after_extract"
            elif in_notes and in_cpt:
                reason = "in_extracted_not_in_rec"
            else:
                reason = "unknown_reject"
        reason_counts[reason] += 1
        acceptance_rows.append(
            {
                "unit_id": u.get("unit_id") or "",
                "emr_id": emr,
                "dos": dos,
                "facility_id": u.get("facility_id") or "",
                "outcome": outcome,
                "reason": reason,
                "in_baseline_rec": str(in_base).lower(),
                "in_side_rec": str(in_side).lower(),
                "in_side_notes": str(in_notes).lower(),
                "in_side_cpt": str(in_cpt).lower(),
                "in_batch_notes": str(in_batch_notes).lower(),
                "pdf_path": pdf or "",
                "has_pdf_lineage": str(bool(pdf)).lower(),
            }
        )

    _write_csv(
        out_dir / "recovered_acceptance.csv",
        acceptance_rows,
        list(acceptance_rows[0].keys())
        if acceptance_rows
        else ["unit_id", "emr_id", "dos", "outcome", "reason"],
    )

    after_summary = _compare_visits(side_rec_path, after_dir)
    # Also keep a Track D-local copy
    shutil.copytree(after_dir, out_dir / "sf_compare_after", dirs_exist_ok=True)
    sf_missing_after = int(
        (after_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )
    net_gap = sf_missing_before - sf_missing_after
    recovered_n = len(recovered_units)
    validated_n = len(validated)
    yield_rate = (accepted_new / validated_n) if validated_n else 0.0
    accept_rate = (accepted_new / recovered_n) if recovered_n else 0.0

    side_rec_rows = _read_csv(side_rec_path)
    seen_dup: set[tuple[str, str]] = set()
    dups: list[tuple[str, str]] = []
    for row in side_rec_rows:
        k = (
            (row.get("webpt_patient_id") or "").strip(),
            (row.get("date_of_service") or "")[:10],
        )
        if not k[0] or not k[1]:
            continue
        if k in seen_dup:
            dups.append(k)
        seen_dup.add(k)

    lineage_fail = [
        r
        for r in acceptance_rows
        if r["outcome"] in {"accepted_new", "already_in_baseline_rec"}
        and (
            r["has_pdf_lineage"] != "true"
            or r["in_side_notes"] != "true"
            or r["in_side_rec"] != "true"
        )
    ]
    new_without_notes = sorted(k for k in new_rec_keys if k not in note_emr_dos)
    recovered_keys = {
        (
            (u.get("webpt_patient_id") or u.get("emr_id") or "").strip(),
            (u.get("dos") or "")[:10],
        )
        for u in recovered_units
    }
    orphan_cpt = sorted(
        k for k in (cpt_emr_dos & recovered_keys) if k not in note_emr_dos
    )
    unexpected_removals = sorted(removed_rec_keys)
    baseline_n_win = len(baseline_keys_win)
    side_n_win = len(side_keys_win)
    row_shrink = side_n_win < baseline_n_win and bool(unexpected_removals)

    integrity = {
        "no_duplicate_emr_dos": {
            "pass": len(dups) == 0,
            "duplicate_count": len(dups),
            "sample": [f"{a}|{b}" for a, b in dups[:10]],
        },
        "no_unexpected_rec_deletions": {
            "pass": len(unexpected_removals) == 0,
            "removed_count": len(unexpected_removals),
            "window": f"{WIN_START}..{WIN_END}",
            "sample": [f"{a}|{b}" for a, b in unexpected_removals[:20]],
        },
        "accepted_units_have_lineage": {
            "pass": len(lineage_fail) == 0,
            "fail_count": len(lineage_fail),
            "sample": [
                {"emr_id": r["emr_id"], "dos": r["dos"], "reason": r["reason"]}
                for r in lineage_fail[:20]
            ],
        },
        "new_rec_keys_have_daily_notes_source": {
            "pass": len(new_without_notes) == 0,
            "fail_count": len(new_without_notes),
            "sample": [f"{a}|{b}" for a, b in new_without_notes[:20]],
        },
        "no_orphan_cpt_for_recovered_dos": {
            "pass": len(orphan_cpt) == 0,
            "fail_count": len(orphan_cpt),
            "sample": [f"{a}|{b}" for a, b in orphan_cpt[:20]],
        },
        "no_unexplained_rec_shrink": {
            "pass": not row_shrink,
            "baseline_keys_window": baseline_n_win,
            "side_keys_window": side_n_win,
            "delta_keys_window": side_n_win - baseline_n_win,
        },
        "merge_seed_side": {
            "pass": merge_stats.get("seed") == "side",
            "seed": merge_stats.get("seed"),
        },
    }
    integrity_ok = all(v.get("pass") for v in integrity.values())

    e2e = {
        "run_id": run.run_id,
        "track": "D",
        "root_cause": SUBTYPE,
        "batch_id": BATCH_ID,
        "ts": utc_now_iso(),
        "promote_blocked": True,
        "phase_a": phase_a_summary,
        "download": download_summary,
        "discovery": discovery,
        "merge": merge_stats,
        "reconcile": {
            "output": str(side_rec_path),
            "summary_keys": list(recon_summary.keys())
            if isinstance(recon_summary, dict)
            else [],
        },
        "recovery_metrics": {
            "validated_candidates": validated_n,
            "recovered_units": recovered_n,
            "visits_accepted_new": accepted_new,
            "visits_accepted": accepted,
            "visits_rejected": rejected_n,
            "rejection_reasons": dict(reason_counts),
            "new_rec_keys_vs_baseline": len(new_rec_keys),
            "removed_rec_keys_vs_baseline": len(removed_rec_keys),
        },
        "coverage_metrics": {
            "sf_missing_before": sf_missing_before,
            "sf_missing_after": sf_missing_after,
            "net_gap_reduction": net_gap,
            "track_d_yield": round(yield_rate, 4),
            "track_d_acceptance_rate": round(accept_rate, 4),
            "formula_yield": "accepted_new / validated_candidates",
            "formula_acceptance": "accepted_new / recovered_units",
        },
        "integrity": integrity,
        "integrity_all_pass": integrity_ok,
        "pipeline_pass": integrity_ok and accepted_new > 0 and merge_stats.get("notes_added", 0) > 0,
        "next_execution_track": "E",
    }
    (out_dir / "e2e_validation_report.json").write_text(
        json.dumps(e2e, indent=2) + "\n", encoding="utf-8"
    )

    cov_md = [
        "# Track D Coverage Delta",
        "",
        f"**Run:** `{run.run_id}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Initial gap (SF missing) | {sf_missing_before} |",
        f"| Current gap (SF missing) | {sf_missing_after} |",
        f"| Validated candidates | {validated_n} |",
        f"| Recovered visits (FSM done) | {recovered_n} |",
        f"| Accepted REC (new) | {accepted_new} |",
        f"| Rejected after recovery | {rejected_n} |",
        f"| Net gap reduction | {net_gap} |",
        f"| Track D historical yield | {100.0 * yield_rate:.1f}% |",
        f"| Acceptance rate | {100.0 * accept_rate:.1f}% |",
        f"| Integrity all pass | {integrity_ok} |",
        "",
        f"Yield formula: `{e2e['coverage_metrics']['formula_yield']}`",
        "",
        "## Failure reasons",
        "",
    ]
    for reason, n in reason_counts.most_common():
        cov_md.append(f"- `{reason}`: {n}")
    phase_a_rejects = Counter(r.get("reject_reason") or "" for r in rejected)
    if phase_a_rejects:
        cov_md.extend(["", "## Phase A reject reasons", ""])
        for reason, n in phase_a_rejects.most_common():
            cov_md.append(f"- `{reason}`: {n}")
    (out_dir / "coverage_delta.md").write_text("\n".join(cov_md) + "\n", encoding="utf-8")

    # --- Phase H: historical yield ---
    variance_pp = (yield_rate - PLANNING_YIELD_D) * 100.0
    pva = [
        "# Track D Planning vs Actual",
        "",
        "> Planning assumptions for prioritization only. Historical is measured.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Planning Yield | {100.0 * PLANNING_YIELD_D:.0f}% |",
        f"| Historical Yield | {100.0 * yield_rate:.1f}% |",
        f"| Variance | {variance_pp:+.1f} pp |",
        f"| Numerator (accepted_new) | {accepted_new} |",
        f"| Denominator (validated) | {validated_n} |",
        "",
        f"Formula: `{e2e['coverage_metrics']['formula_yield']}`",
        "",
        f"Source: `{out_dir / 'e2e_validation_report.json'}`",
    ]
    (out_dir / "planning_vs_actual.md").write_text("\n".join(pva) + "\n", encoding="utf-8")

    frag = {
        "track": "D",
        "label": "Track D dos_before_first_note",
        "historical_yield": round(yield_rate, 4),
        "historical_yield_pct": f"{100.0 * yield_rate:.1f}%",
        "formula": "accepted_new / validated_candidates",
        "inputs": {
            "accepted_new": accepted_new,
            "validated_candidates": validated_n,
            "recovered_units": recovered_n,
            "net_gap_reduction": net_gap,
            "dos_ok_after_extract": dos_ok,
            "dos_fail_after_extract": dos_fail,
        },
        "source": str(out_dir / "e2e_validation_report.json"),
        "status": "measured",
    }
    (out_dir / "historical_yield_fragment.json").write_text(
        json.dumps(frag, indent=2) + "\n", encoding="utf-8"
    )

    summary = {
        "run_id": run.run_id,
        "track": "D",
        "validated": validated_n,
        "recovered_units": recovered_n,
        "accepted_new": accepted_new,
        "sf_missing_before": sf_missing_before,
        "sf_missing_after": sf_missing_after,
        "net_gap_reduction": net_gap,
        "historical_yield": round(yield_rate, 4),
        "integrity_all_pass": integrity_ok,
        "promote_blocked": True,
        "next_execution_track": "E",
        "planning_yields_unchanged": True,
        "artifacts_dir": str(out_dir),
        "ts": utc_now_iso(),
    }
    (run.run_dir / "summaries" / "track_d_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    # --- Phase I: classify refresh ---
    classify_rc = None
    if not args.skip_classify:
        cl = subprocess.run(
            [
                sys.executable,
                str(_REPO / "snowflake_pull/scripts/classify_remaining_sf_gap.py"),
                "--run-id",
                run.run_id,
                "--allow-input-drift",
            ],
            cwd=str(_REPO),
        )
        classify_rc = cl.returncode
        summary["classify_rc"] = classify_rc
        (run.run_dir / "summaries" / "track_d_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )

    run.obs.stage_end("track_d", **{k: summary[k] for k in summary if k != "ts"})
    print(json.dumps(summary, indent=2))
    finish_run(
        run,
        status="track_d_done" if integrity_ok else "track_d_failed",
    )
    set_global_obs(None)
    if not integrity_ok:
        return 2
    if classify_rc not in (None, 0):
        return classify_rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
