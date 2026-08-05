"""Phase 4: process gap download batches with unit FSM — gated on P2b.

Default mode is dry-run/simulation advancing unit states for resume drills.
Pass --execute to invoke parallel-download (requires P2b pass).
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import finish_run, load_gate, resume_run  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402


def _build_patients_csv(units, export_path: Path, out_csv: Path) -> int:
    # OBSOLETE for Case-centric pipeline (first-row-per-patient case selection).
    # Case work: snowflake_pull.scripts.build_case_schedule + run_case_pipeline.
    """Write a minimal patients CSV for parallel-download from export rows."""
    wanted = {(u.webpt_patient_id or u.emr_id) for u in units}
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with export_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            if pid in wanted and pid not in seen:
                rows.append(row)
                seen.add(pid)
    if not rows:
        return 0
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def _dos_in_extracted(extracted_dir: Path, emr: str, dos: str) -> bool:
    notes = extracted_dir / "daily_notes.csv"
    if not notes.is_file():
        return False
    with notes.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("patient_id") or "").strip() != emr:
                continue
            if (row.get("date_of_daily_note") or "")[:10] == dos[:10]:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--batch-id", default="")
    p.add_argument("--max-units", type=int, default=50)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--simulate", action="store_true", help="Advance FSM without browser")
    p.add_argument("--early-stop-rate", type=float, default=0.15)
    p.add_argument("--allow-input-drift", action="store_true")
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_gap_batches.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("gap_batches")
    run.obs.online = bool(args.execute)

    p2b = load_gate(run.run_dir, "P2b")
    if args.execute:
        if not p2b or p2b.get("pass") is not True:
            payload = {
                "blocked": True,
                "reason": "P2b_not_passed",
                "p2b": p2b,
            }
            (run.run_dir / "summaries" / "gap_batches_summary.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            run.obs.emit(
                "decision",
                operation="gap_batches",
                outcome="skip",
                decision="blocked_on_p2b",
                decision_reason="P2b_not_passed",
            )
            run.obs.stage_end("gap_batches", **payload)
            print(json.dumps(payload, indent=2))
            finish_run(run, status="gap_batches_blocked")
            set_global_obs(None)
            return 2

    if not args.execute and not args.simulate:
        args.simulate = True

    run.obs.set_batch(args.batch_id or "all")
    attempted = 0
    recovered = 0
    failed = 0
    processed = 0
    claimed = []

    # Claim a batch of units first (resume-safe).
    while len(claimed) < args.max_units:
        unit = run.store.claim_next(batch_id=args.batch_id or None)
        if unit is None:
            break
        claimed.append(unit)
        run.obs.emit(
            "unit_state_change",
            operation="claim",
            correlation_id=f"{unit.emr_id}|{unit.dos}|{unit.facility_id}|{unit.unit_id}",
            emr_id=unit.emr_id,
            dos=unit.dos,
            facility_id=unit.facility_id,
            webpt_patient_id=unit.webpt_patient_id,
            unit_state_from="queued",
            unit_state_to="in_progress",
            outcome="success",
        )

    export_path = _REPO / "webpt_edco_scraper/output/jun_jul_2026/patients_export_273d.csv"
    batch_dir = run.artifacts / "gap_batches" / (args.batch_id or "batch")
    patients_csv = batch_dir / "patients_gap_batch.csv"
    edocs_out = batch_dir / "edocs"
    extracted_out = batch_dir / "extracted"

    if args.execute and claimed:
        n_pat = _build_patients_csv(claimed, export_path, patients_csv)
        run.obs.emit(
            "decision",
            operation="gap_execute_prepare",
            decision="patients_csv_ready",
            decision_reason=f"patients={n_pat} units={len(claimed)}",
            extra={"patients_csv": str(patients_csv)},
        )
        if n_pat == 0:
            for unit in claimed:
                run.store.transition(
                    unit.unit_id, "failed_terminal", error_type="PatientNotInWebPT"
                )
                failed += 1
                attempted += 1
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
            run.obs.emit(
                "decision",
                operation="parallel_download",
                decision="start_download",
                extra={"command": dl_cmd},
            )
            proc = subprocess.run(dl_cmd, cwd=str(_SCRAPER))
            if proc.returncode != 0:
                run.obs.emit(
                    "error",
                    level="ERROR",
                    operation="parallel_download",
                    outcome="fail",
                    error_type="Unexpected",
                    decision_reason=f"rc={proc.returncode}",
                )
            # Only extract from THIS batch's edocs — never the full jun_jul tree.
            edocs_root = edocs_out if edocs_out.is_dir() else (batch_dir / "edocs")
            pdf_n = (
                len(list(edocs_root.rglob("*.pdf"))) if edocs_root.is_dir() else 0
            )
            if pdf_n == 0:
                run.obs.emit(
                    "error",
                    level="ERROR",
                    operation="extract_daily_notes",
                    outcome="fail",
                    error_type="Unexpected",
                    decision="skip_extract_empty_batch_edocs",
                    decision_reason="parallel_download_produced_zero_pdfs",
                    extra={"edocs_root": str(edocs_root)},
                )
            else:
                ex_cmd = [
                    sys.executable,
                    str(_SCRAPER / "scraper.py"),
                    "extract-daily-notes",
                    "--input",
                    str(edocs_root),
                    "--output-dir",
                    str(extracted_out),
                ]
                run.obs.emit(
                    "decision",
                    operation="extract_daily_notes",
                    decision="start_extract",
                    extra={"command": ex_cmd, "pdf_n": pdf_n},
                )
                subprocess.run(ex_cmd, cwd=str(_SCRAPER))

    for unit in claimed:
        if run.obs.abort_requested():
            break
        current = run.store.get(unit.unit_id)
        if current and current.state == "failed_terminal":
            # already finalized in prepare step
            continue
        attempted += 1
        processed += 1
        run.obs.set_progress(
            completed=processed,
            remaining=max(len(claimed) - processed, 0),
        )
        corr = f"{unit.emr_id}|{unit.dos}|{unit.facility_id}|{unit.unit_id}"
        t0 = time.perf_counter()
        try:
            if args.simulate and not args.execute:
                run.store.transition(unit.unit_id, "downloaded")
                run.store.transition(unit.unit_id, "extracted")
                run.store.transition(unit.unit_id, "reconciled")
                run.store.transition(unit.unit_id, "done")
                recovered += 1
                run.obs.mark_success(
                    operation="simulate_gap_unit",
                    emr_id=unit.emr_id,
                    dos=unit.dos,
                    facility_id=unit.facility_id,
                    correlation_id=corr,
                )
            else:
                # Mark downloaded after batch download attempt
                run.store.transition(unit.unit_id, "downloaded")
                run.store.transition(unit.unit_id, "extracted")
                hit = _dos_in_extracted(extracted_out, unit.emr_id, unit.dos)
                if hit:
                    run.store.transition(unit.unit_id, "reconciled")
                    run.store.transition(unit.unit_id, "done")
                    recovered += 1
                    run.obs.emit(
                        "decision",
                        operation="gap_unit",
                        correlation_id=corr,
                        outcome="success",
                        decision="note_recovered_for_dos",
                        decision_reason="dos_in_extracted_notes",
                        emr_id=unit.emr_id,
                        dos=unit.dos,
                        facility_id=unit.facility_id,
                    )
                    run.obs.mark_success(
                        operation="gap_unit",
                        emr_id=unit.emr_id,
                        dos=unit.dos,
                        facility_id=unit.facility_id,
                        correlation_id=corr,
                    )
                else:
                    run.store.transition(
                        unit.unit_id,
                        "failed_terminal",
                        error_type="NoteDosAbsentAfterDownload",
                    )
                    failed += 1
                    run.obs.emit(
                        "decision",
                        operation="gap_unit",
                        correlation_id=corr,
                        outcome="fail",
                        decision="NoteDosAbsentAfterDownload",
                        decision_reason="dos_not_in_extracted_after_batch",
                        error_type="NoteDosAbsentAfterDownload",
                        error_expected=True,
                        emr_id=unit.emr_id,
                        dos=unit.dos,
                        facility_id=unit.facility_id,
                    )
            run.obs.metrics.incr("attempted")
            run.obs.metrics.observe_latency((time.perf_counter() - t0) * 1000)
            run.obs.mark_checkpoint()
        except Exception as exc:
            failed += 1
            run.obs.metrics.incr("attempted")
            run.obs.metrics.incr("failed")
            try:
                run.store.transition(
                    unit.unit_id, "failed_terminal", error_type="Unexpected"
                )
            except Exception:
                pass
            run.obs.emit(
                "error",
                level="ERROR",
                operation="gap_unit",
                correlation_id=corr,
                outcome="fail",
                error_type="Unexpected",
                error_expected=False,
                exception=exc,
                unit_state_to="failed_terminal",
                emr_id=unit.emr_id,
                dos=unit.dos,
            )

    if attempted >= 20 and args.execute:
        rate = recovered / attempted if attempted else 0
        if rate < args.early_stop_rate:
            run.obs.emit(
                "decision",
                operation="early_stop",
                decision="early_stop_recovery_collapse",
                decision_reason=f"rate={rate:.3f}<{args.early_stop_rate}",
            )

    summary = {
        "attempted": attempted,
        "recovered": recovered,
        "failed": failed,
        "simulate": bool(args.simulate) and not bool(args.execute),
        "execute": bool(args.execute),
        "counts_by_state": run.store.counts_by_state(),
    }
    (run.run_dir / "summaries" / "gap_batches_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    # refresh checkpoint pointer
    (run.run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "sqlite": str(run.state_db),
                "updated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "counts": summary["counts_by_state"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    run.obs.stage_end("gap_batches", **summary)
    print(json.dumps({"run_id": run.run_id, **summary}, indent=2))
    finish_run(run, status="gap_batches_done")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
