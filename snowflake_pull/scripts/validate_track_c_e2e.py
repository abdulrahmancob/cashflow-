"""End-to-end Track C validation: merge gap extracts → side-by-side REC → coverage delta.

Offline only. Does not download more notes and does not promote live REC.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402

BATCH_ID = "gap_dos_after_last_note"
BASE = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
SF_CSV = _REPO / "snowflake_pull/output/all_billing_data.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _note_key(row: dict[str, str]) -> tuple[str, str, str]:
    pid = (row.get("patient_id") or "").strip()
    dos = (row.get("date_of_daily_note") or "")[:10]
    nid = (row.get("note_file") or row.get("daily_note_id") or "").strip()
    return (pid, dos, nid)


def _cpt_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        (row.get("patient_id") or "").strip(),
        (row.get("date_of_daily_note") or "")[:10],
        (row.get("cpt_code") or "").strip(),
        (row.get("daily_note_id") or "").strip(),
    )


def _rec_keys(path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in _read_csv(path):
        pid = (row.get("webpt_patient_id") or "").strip()
        dos = (row.get("date_of_service") or "")[:10]
        if pid and dos:
            keys.add((pid, dos))
    return keys


def _note_keys_by_emr_dos(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for row in rows:
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        if pid and dos:
            out.add((pid, dos))
    return out


def _cpt_keys_by_emr_dos(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for row in rows:
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        if pid and dos:
            out.add((pid, dos))
    return out


def _merge_extracted(
    side: Path, batch_extracted: Path, *, seed: str = "live"
) -> dict:
    """Merge gap-batch extracts into side-by-side extracted.

    seed='live' (Track C default): reseed side CSVs from live extracted, then append.
    seed='side' (Track D): keep existing side CSVs (preserves Track F CPT merges), then append.
    """
    side.mkdir(parents=True, exist_ok=True)
    if seed not in {"live", "side"}:
        raise SystemExit(f"invalid merge seed={seed!r}; expected live|side")
    if seed == "live":
        for name in ("daily_notes.csv", "cpt_codes.csv"):
            src = BASE / "extracted" / name
            if not src.is_file():
                raise SystemExit(f"live extracted missing: {src}")
            shutil.copy2(src, side / name)
    else:
        for name in ("daily_notes.csv", "cpt_codes.csv"):
            if not (side / name).is_file():
                raise SystemExit(
                    f"side extracted missing for seed=side: {side / name}"
                )

    from chart_notes_parse import CPT_CODES_FIELDNAMES, DAILY_NOTES_FIELDNAMES

    notes_path = side / "daily_notes.csv"
    cpt_path = side / "cpt_codes.csv"
    base_notes = _read_csv(notes_path)
    base_cpt = _read_csv(cpt_path)
    gap_notes = _read_csv(batch_extracted / "daily_notes.csv")
    gap_cpt = _read_csv(batch_extracted / "cpt_codes.csv")

    note_seen = {_note_key(r) for r in base_notes}
    notes_added = 0
    note_collisions = 0
    for row in gap_notes:
        k = _note_key(row)
        if not k[0] or not k[1]:
            continue
        if k in note_seen:
            note_collisions += 1
            continue
        note_seen.add(k)
        base_notes.append(row)
        notes_added += 1

    cpt_seen = {_cpt_key(r) for r in base_cpt}
    cpt_added = 0
    cpt_collisions = 0
    for row in gap_cpt:
        k = _cpt_key(row)
        if not k[0] or not k[1]:
            continue
        if k in cpt_seen:
            cpt_collisions += 1
            continue
        cpt_seen.add(k)
        base_cpt.append(row)
        cpt_added += 1

    _write_csv(notes_path, base_notes, list(DAILY_NOTES_FIELDNAMES))
    _write_csv(cpt_path, base_cpt, list(CPT_CODES_FIELDNAMES))
    return {
        "seed": seed,
        "notes_base": len(base_notes) - notes_added,
        "notes_gap": len(gap_notes),
        "notes_added": notes_added,
        "note_collisions": note_collisions,
        "cpt_base": len(base_cpt) - cpt_added,
        "cpt_gap": len(gap_cpt),
        "cpt_rows_added": cpt_added,
        "cpt_collisions": cpt_collisions,
        "side_notes_total": len(base_notes),
        "side_cpt_total": len(base_cpt),
    }


def _run_side_reconcile(side_extracted: Path, out_dir: Path) -> dict:
    from cashflow_reconcile.reconcile import run_reconciliation

    tracker_candidates = [
        _REPO / "revflow_scraper/output/Transaction Tracker 2026.xlsx",
        _REPO / "webpt_edco_scraper/Transaction Tracker 2026.xlsx",
    ]
    tracker = next((t for t in tracker_candidates if t.is_file()), None)
    revflow = _REPO / "revflow_scraper/output"
    if tracker is None:
        raise SystemExit("Transaction Tracker xlsx not found")
    if not revflow.is_dir():
        raise SystemExit(f"revflow dir missing: {revflow}")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = run_reconciliation(
        webpt_dir=side_extracted,
        patients_export=BASE / "patients_export_273d.csv",
        revflow_dir=revflow,
        manifest=None,
        output_dir=out_dir,
        insurance_map=None,
        service_from=date(2026, 6, 1),
        service_to=date(2026, 7, 31),
        transaction_tracker=tracker,
    )
    return summary if isinstance(summary, dict) else {"summary": str(summary)}


def _compare_visits(visits: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "snowflake_pull.compare_visits",
        "--dual-key",
        "--snowflake",
        str(SF_CSV),
        "--visits",
        str(visits),
        "--from",
        "2026-06-01",
        "--to",
        "2026-07-31",
        "--output",
        str(output),
    ]
    proc = subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True)
    summary_path = output / "coverage_summary.json"
    if proc.returncode != 0 or not summary_path.is_file():
        raise RuntimeError(
            f"compare_visits failed rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    return json.loads(summary_path.read_text(encoding="utf-8"))


def _load_recovered_units(db_path: Path, batch_id: str) -> list[dict[str, str]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT unit_id, emr_id, webpt_patient_id, dos, facility_id, state, batch_id
        FROM units
        WHERE batch_id = ? AND state = 'done'
        ORDER BY unit_id
        """,
        (batch_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _find_pdf(edocs: Path, emr: str, dos: str) -> str | None:
    chart = edocs / emr / "chart_notes"
    if not chart.is_dir():
        return None
    # Prefer filename starting with DOS
    hits = sorted(chart.glob(f"{dos}*.pdf"))
    if hits:
        return str(hits[0])
    # Any DailyNote pdf for patient (weaker lineage)
    any_dn = sorted(chart.glob("*DailyNote*.pdf"))
    return str(any_dn[0]) if any_dn else None


def _recommendation(
    *,
    net_gap: int,
    accepted_new: int,
    recovered: int,
    integrity_ok: bool,
) -> str:
    rate = accepted_new / recovered if recovered else 0.0
    if not integrity_ok or net_gap <= 0 or rate < 0.20:
        return "Stop Track C because recovery is not translating into REC improvements"
    if net_gap >= 20 and rate >= 0.40 and integrity_ok:
        return "Continue Track C with larger batches (100 → 200 → remaining queue)"
    return "Continue with caution and explain why"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--batch-id", default=BATCH_ID)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument(
        "--skip-reconcile",
        action="store_true",
        help="Reuse existing side-by-side reconciliation if present",
    )
    p.add_argument(
        "--seed",
        choices=("live", "side"),
        default="live",
        help="Merge seed: live reseeds from live extracted; side appends onto existing side",
    )
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="validate_track_c_e2e.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("track_c_e2e")
    run.obs.online = False

    batch_dir = run.artifacts / "gap_batches" / args.batch_id
    batch_extracted = batch_dir / "extracted"
    edocs = batch_dir / "edocs"
    if not (batch_extracted / "daily_notes.csv").is_file():
        raise SystemExit(f"batch extracted missing: {batch_extracted}")

    baseline_rec = run.baseline / "reconciliation_visits.csv"
    if not baseline_rec.is_file():
        raise SystemExit(f"baseline REC missing: {baseline_rec}")

    validation = run.artifacts / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    side_extracted = run.side_by_side / "extracted"
    side_recon = run.side_by_side / "reconciliation"
    side_rec_path = side_recon / "reconciliation_visits.csv"

    # Snapshot previous E2E report for Previous vs New table.
    prev_report: dict = {}
    report_json_path = validation / "e2e_validation_report.json"
    report_md_path = validation / "e2e_validation_report.md"
    if report_json_path.is_file():
        try:
            prev_report = json.loads(report_json_path.read_text(encoding="utf-8"))
        except Exception:
            prev_report = {}
        shutil.copy2(report_json_path, validation / "e2e_validation_report.prev.json")
    if report_md_path.is_file():
        shutil.copy2(report_md_path, validation / "e2e_validation_report.prev.md")

    # --- 1. Merge ---
    run.obs.emit(
        "decision",
        operation="merge",
        decision="start_merge_gap_extracted",
        extra={"seed": args.seed},
    )
    merge_stats = _merge_extracted(side_extracted, batch_extracted, seed=args.seed)
    run.obs.emit(
        "decision",
        operation="merge",
        decision="merge_complete",
        extra=merge_stats,
    )

    # --- 2. Reconcile ---
    if args.skip_reconcile and side_rec_path.is_file():
        recon_summary = {"skipped": True, "path": str(side_rec_path)}
        run.obs.emit(
            "decision",
            operation="reconcile",
            decision="reuse_existing_side_rec",
        )
    else:
        run.obs.emit("decision", operation="reconcile", decision="side_by_side_reconcile_start")
        recon_summary = _run_side_reconcile(side_extracted, side_recon)
        run.obs.emit(
            "decision",
            operation="reconcile",
            decision="side_by_side_reconcile_done",
            extra={"summary_keys": list(recon_summary.keys()) if isinstance(recon_summary, dict) else []},
        )

    if not side_rec_path.is_file():
        raise SystemExit(f"side-by-side REC not produced: {side_rec_path}")

    # --- 3. Acceptance ---
    recovered_units = _load_recovered_units(
        run.run_dir / "state" / "units.sqlite", args.batch_id
    )
    baseline_keys = _rec_keys(baseline_rec)
    side_keys = _rec_keys(side_rec_path)
    # Live baseline includes Jan–May history; side reconcile uses jun_jul extracted
    # only. Integrity deletions are scoped to the Track C service window.
    win_start, win_end = "2026-06-01", "2026-07-31"

    def _in_window(key: tuple[str, str]) -> bool:
        dos = key[1]
        return bool(dos) and win_start <= dos <= win_end

    baseline_keys_win = {k for k in baseline_keys if _in_window(k)}
    side_keys_win = {k for k in side_keys if _in_window(k)}
    side_notes = _read_csv(side_extracted / "daily_notes.csv")
    side_cpt = _read_csv(side_extracted / "cpt_codes.csv")
    note_emr_dos = _note_keys_by_emr_dos(side_notes)
    cpt_emr_dos = _cpt_keys_by_emr_dos(side_cpt)
    batch_notes = _read_csv(batch_extracted / "daily_notes.csv")
    batch_note_emr_dos = _note_keys_by_emr_dos(batch_notes)

    new_rec_keys = side_keys_win - baseline_keys_win
    removed_rec_keys = baseline_keys_win - side_keys_win

    acceptance_rows: list[dict[str, str]] = []
    reason_counts: Counter[str] = Counter()
    accepted = 0
    accepted_new = 0
    rejected = 0

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
            rejected += 1
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

    acc_csv = validation / "recovered_acceptance.csv"
    _write_csv(
        acc_csv,
        acceptance_rows,
        list(acceptance_rows[0].keys())
        if acceptance_rows
        else [
            "unit_id",
            "emr_id",
            "dos",
            "outcome",
            "reason",
        ],
    )

    # --- 4. Coverage before/after ---
    run.obs.emit("decision", operation="coverage", decision="compare_before_start")
    before_summary = _compare_visits(
        baseline_rec, validation / "sf_compare_before"
    )
    run.obs.emit("decision", operation="coverage", decision="compare_after_start")
    after_summary = _compare_visits(
        side_rec_path, validation / "sf_compare_after"
    )
    sf_missing_before = int(
        (before_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )
    sf_missing_after = int(
        (after_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )
    net_gap = sf_missing_before - sf_missing_after
    recovered_n = len(recovered_units)
    recovery_efficiency = (net_gap / recovered_n) if recovered_n else 0.0
    acceptance_rate = (accepted_new / recovered_n) if recovered_n else 0.0

    # --- 5. Integrity ---
    # Duplicate EMR+DOS in side REC
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

    # Lineage for accepted recovered units
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

    # New REC keys must have daily_notes source
    new_without_notes = sorted(k for k in new_rec_keys if k not in note_emr_dos)

    # Orphan CPT for recovered DOS (cpt without note parent)
    recovered_keys = {
        ((u.get("webpt_patient_id") or u.get("emr_id") or "").strip(), (u.get("dos") or "")[:10])
        for u in recovered_units
    }
    orphan_cpt = sorted(
        k for k in (cpt_emr_dos & recovered_keys) if k not in note_emr_dos
    )

    unexpected_removals = sorted(removed_rec_keys)
    baseline_n = len(baseline_keys)
    side_n = len(side_keys)
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
            "window": f"{win_start}..{win_end}",
            "sample": [f"{a}|{b}" for a, b in unexpected_removals[:20]],
            "note": (
                "Scoped to Jun–Jul; baseline also has Jan–May history "
                "outside side-by-side extracted scope"
            ),
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
            "baseline_keys_full": baseline_n,
            "side_keys_full": side_n,
            "baseline_keys_window": baseline_n_win,
            "side_keys_window": side_n_win,
            "delta_keys_window": side_n_win - baseline_n_win,
            "window": f"{win_start}..{win_end}",
        },
    }
    integrity_ok = all(v.get("pass") for v in integrity.values())

    recommendation = _recommendation(
        net_gap=net_gap,
        accepted_new=accepted_new,
        recovered=recovered_n,
        integrity_ok=integrity_ok,
    )

    pipeline_ok = (
        accepted_new > 0
        and net_gap > 0
        and integrity_ok
        and merge_stats["notes_added"] > 0
    )

    report = {
        "run_id": run.run_id,
        "batch_id": args.batch_id,
        "pipeline_validation": {
            "pass": pipeline_ok,
            "recovered_notes_became_valid_rec_visits": accepted_new > 0 and net_gap > 0,
            "reason": (
                "recovered notes produced measurable side-by-side REC / coverage gains"
                if pipeline_ok
                else "recovered notes did not produce sufficient validated REC improvement"
            ),
        },
        "merge": merge_stats,
        "reconcile": {
            "output": str(side_rec_path),
            "summary": recon_summary,
        },
        "recovery_metrics": {
            "recovered_units": recovered_n,
            "extracted_visits_in_batch": len(batch_note_emr_dos),
            "batch_daily_note_rows": len(batch_notes),
            "batch_cpt_rows": len(_read_csv(batch_extracted / "cpt_codes.csv")),
            "visits_accepted_into_rec": accepted,
            "visits_accepted_new": accepted_new,
            "visits_rejected": rejected,
            "rejection_reasons": dict(reason_counts),
            "new_rec_keys_vs_baseline": len(new_rec_keys),
            "removed_rec_keys_vs_baseline": len(removed_rec_keys),
        },
        "rec_metrics": {
            "previous_rec_visit_keys_full": baseline_n,
            "new_rec_visit_keys_full": side_n,
            "previous_rec_visit_keys_window": baseline_n_win,
            "new_rec_visit_keys_window": side_n_win,
            "previous_rec_rows": len(_read_csv(baseline_rec)),
            "new_rec_rows": len(side_rec_rows),
            "duplicate_emr_dos": len(dups),
            "orphan_new_rec_without_notes": len(new_without_notes),
            "unexpected_removals_window": len(unexpected_removals),
            "service_window": f"{win_start}..{win_end}",
        },
        "coverage_metrics": {
            "sf_missing_before": sf_missing_before,
            "sf_missing_after": sf_missing_after,
            "net_gap_reduction": net_gap,
            "recovery_efficiency": round(recovery_efficiency, 4),
            "acceptance_rate_new": round(acceptance_rate, 4),
            "before_summary_path": str(validation / "sf_compare_before" / "coverage_summary.json"),
            "after_summary_path": str(validation / "sf_compare_after" / "coverage_summary.json"),
        },
        "integrity": integrity,
        "integrity_all_pass": integrity_ok,
        "recommendation": recommendation,
        "artifacts": {
            "acceptance_csv": str(acc_csv),
            "report_json": str(validation / "e2e_validation_report.json"),
            "report_md": str(validation / "e2e_validation_report.md"),
        },
    }

    prev_cov = (prev_report or {}).get("coverage_metrics") or {}
    prev_recov = (prev_report or {}).get("recovery_metrics") or {}
    prev_vs_new = {
        "sf_missing_after": {
            "previous": prev_cov.get("sf_missing_after"),
            "new": sf_missing_after,
            "delta": (
                None
                if prev_cov.get("sf_missing_after") is None
                else int(prev_cov["sf_missing_after"]) - sf_missing_after
            ),
        },
        "net_gap_reduction_vs_baseline": {
            "previous": prev_cov.get("net_gap_reduction"),
            "new": net_gap,
        },
        "accepted_new": {
            "previous": prev_recov.get("visits_accepted_new"),
            "new": accepted_new,
        },
        "new_rec_keys_window": {
            "previous": prev_recov.get("new_rec_keys_vs_baseline"),
            "new": len(new_rec_keys),
        },
        "recovery_efficiency": {
            "previous": prev_cov.get("recovery_efficiency"),
            "new": round(recovery_efficiency, 4),
        },
    }
    report["previous_vs_new"] = prev_vs_new

    report_json = validation / "e2e_validation_report.json"
    report_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def _cell(v) -> str:
        return "—" if v is None else str(v)

    md_lines = [
        "# Track C End-to-End REC Validation",
        "",
        f"**Run:** `{run.run_id}`",
        f"**Batch:** `{args.batch_id}`",
        "",
        "## 0. Previous vs New (wave delta)",
        "",
        "| Metric | Previous | New | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| SF missing after | {_cell(prev_vs_new['sf_missing_after']['previous'])} "
            f"| {sf_missing_after} | {_cell(prev_vs_new['sf_missing_after']['delta'])} |"
        ),
        (
            f"| Net gap reduction (vs baseline) | "
            f"{_cell(prev_vs_new['net_gap_reduction_vs_baseline']['previous'])} "
            f"| {net_gap} | |"
        ),
        (
            f"| Accepted new | {_cell(prev_vs_new['accepted_new']['previous'])} "
            f"| {accepted_new} | |"
        ),
        (
            f"| New REC keys (window) | "
            f"{_cell(prev_vs_new['new_rec_keys_window']['previous'])} "
            f"| {len(new_rec_keys)} | |"
        ),
        (
            f"| Recovery efficiency | "
            f"{_cell(prev_vs_new['recovery_efficiency']['previous'])} "
            f"| {recovery_efficiency:.4f} | |"
        ),
        "",
        "## 1. End-to-End Pipeline Validation",
        "",
        f"- **Pass:** {pipeline_ok}",
        f"- **Recovered notes → valid REC visits:** "
        f"{report['pipeline_validation']['recovered_notes_became_valid_rec_visits']}",
        f"- {report['pipeline_validation']['reason']}",
        "",
        "## 2. Coverage Delta",
        "",
        f"| Metric | Value |",
        f"|---|---:|",
        f"| SF missing before (EMR+DOS) | {sf_missing_before} |",
        f"| SF missing after (EMR+DOS) | {sf_missing_after} |",
        f"| Net gap reduction | {net_gap} |",
        f"| Recovery efficiency (gap↓ / recovered) | {recovery_efficiency:.4f} |",
        "",
        "## 3. Acceptance Report",
        "",
        f"| Stage | N |",
        f"|---|---:|",
        f"| Recovered units (FSM done) | {recovered_n} |",
        f"| Accepted into side REC | {accepted} |",
        f"| Accepted **new** (not in baseline) | {accepted_new} |",
        f"| Rejected / not net-new buckets | see reasons |",
        "",
        "### Reasons",
        "",
    ]
    for reason, n in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        md_lines.append(f"- `{reason}`: {n}")
    md_lines += [
        "",
        "### Merge",
        "",
        f"- notes added: {merge_stats['notes_added']} (collisions {merge_stats['note_collisions']})",
        f"- CPT rows added: {merge_stats['cpt_rows_added']} (collisions {merge_stats['cpt_collisions']})",
        "",
        "### REC counts",
        "",
        f"- baseline keys (full / Jun–Jul window): {baseline_n} / {baseline_n_win}",
        f"- side-by-side keys (full / window): {side_n} / {side_n_win}",
        f"- new keys (window): {len(new_rec_keys)}",
        f"- unexpected removals (window): {len(unexpected_removals)}",
        "",
        "## Integrity Checks",
        "",
    ]
    for name, detail in integrity.items():
        md_lines.append(
            f"- **{name}:** `{'PASS' if detail.get('pass') else 'FAIL'}` "
            f"({json.dumps({k: v for k, v in detail.items() if k != 'sample'}, ensure_ascii=True)})"
        )
    md_lines += [
        "",
        "## 4. Recommendation",
        "",
        f"**{recommendation}**",
        "",
        f"- integrity_all_pass={integrity_ok}",
        f"- net_gap_reduction={net_gap}",
        f"- accepted_new/recovered={acceptance_rate:.4f}",
        "",
        f"Detail CSV: `{acc_csv}`",
        "",
    ]
    report_md = validation / "e2e_validation_report.md"
    report_md.write_text("\n".join(md_lines), encoding="utf-8")

    rollup = {
        "run_id": run.run_id,
        "pass": pipeline_ok,
        "integrity_all_pass": integrity_ok,
        "sf_missing_before": sf_missing_before,
        "sf_missing_after": sf_missing_after,
        "net_gap_reduction": net_gap,
        "recovered_units": recovered_n,
        "accepted_new": accepted_new,
        "rejected": rejected,
        "recommendation": recommendation,
        "report_md": str(report_md),
        "report_json": str(report_json),
    }
    (run.run_dir / "summaries" / "track_c_e2e_summary.json").write_text(
        json.dumps(rollup, indent=2) + "\n", encoding="utf-8"
    )

    run.obs.stage_end("track_c_e2e", **{k: rollup[k] for k in (
        "pass", "net_gap_reduction", "accepted_new", "recovered_units"
    )})
    print(json.dumps(rollup, indent=2))
    finish_run(run, status="track_c_e2e_done")
    set_global_obs(None)
    return 0 if integrity_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
