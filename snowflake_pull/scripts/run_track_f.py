"""Track F: offline CPT recovery for note_exists_cpt_missing.

No browser download. No live REC promote.
Fixes bare-CPT parse → re-extract → merge into side-by-side → reconcile → E2E.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402

BASE = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
EDOCS = BASE / "edocs"
SF_CSV = _REPO / "snowflake_pull/output/all_billing_data.csv"
WIN_START = date(2026, 6, 1)
WIN_END = date(2026, 7, 31)
PLANNING_YIELD_F = 0.95


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


def _cpt_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        (row.get("patient_id") or "").strip(),
        (row.get("date_of_daily_note") or "")[:10],
        (row.get("cpt_code") or "").strip(),
        (row.get("daily_note_id") or "").strip(),
    )


def _emr_dos_set_from_notes(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for row in rows:
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        if pid and dos:
            out.add((pid, dos))
    return out


def _emr_dos_set_from_cpt(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    return _emr_dos_set_from_notes(rows)


def _rec_keys(path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in _read_csv(path):
        pid = (row.get("webpt_patient_id") or "").strip()
        dos = (row.get("date_of_service") or "")[:10]
        if pid and dos:
            keys.add((pid, dos))
    return keys


def _note_file_lookup(notes_rows: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for row in notes_rows:
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        nf = (row.get("note_file") or "").strip()
        if pid and dos and nf:
            out[(pid, dos)] = nf
    return out


def _find_pdf(emr: str, dos: str, note_file: str) -> Path | None:
    chart = EDOCS / emr / "chart_notes"
    if note_file:
        p = chart / note_file
        if p.is_file():
            return p
    if not chart.is_dir():
        return None
    # Prefer DailyNote matching DOS
    matches = sorted(chart.glob(f"{dos}_DailyNote*.pdf"))
    if matches:
        return matches[0]
    matches = sorted(chart.glob(f"*{dos}*DailyNote*.pdf"))
    if matches:
        return matches[0]
    return None


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
            f"stdout={(proc.stdout or '')[-2000:]}\nstderr={(proc.stderr or '')[-2000:]}"
        )
    return json.loads(summary_path.read_text(encoding="utf-8"))


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
        service_from=WIN_START,
        service_to=WIN_END,
        transaction_tracker=tracker,
    )
    return summary if isinstance(summary, dict) else {"summary": str(summary)}


def _fsm_mark_recovered(db_path: Path, recovered_keys: set[tuple[str, str]]) -> int:
    if not db_path.is_file() or not recovered_keys:
        return 0
    con = sqlite3.connect(str(db_path))
    n = 0
    now = utc_now_iso()
    for emr, dos in recovered_keys:
        cur = con.execute(
            "SELECT unit_id, state FROM units "
            "WHERE batch_id=? AND (emr_id=? OR webpt_patient_id=?) AND dos=?",
            ("gap_note_exists_cpt_missing", emr, emr, dos),
        ).fetchall()
        for unit_id, state in cur:
            if state == "done":
                continue
            con.execute(
                "UPDATE units SET state='done', prev_state=?, error_type='', "
                "updated_at=?, in_progress_since='' WHERE unit_id=?",
                (state or "queued", now, unit_id),
            )
            n += 1
    con.commit()
    con.close()
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument(
        "--skip-reconcile",
        action="store_true",
        help="Skip reconcile (debug only)",
    )
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_track_f.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("track_f")
    run.obs.online = False

    out_dir = run.artifacts / "track_f"
    out_dir.mkdir(parents=True, exist_ok=True)

    gap_csv = run.artifacts / "remaining_gap" / "remaining_gap_breakdown.csv"
    if not gap_csv.is_file():
        raise SystemExit(f"missing {gap_csv}; run classify_remaining_sf_gap first")

    side_extracted = run.side_by_side / "extracted"
    side_recon = run.side_by_side / "reconciliation"
    side_rec_path = side_recon / "reconciliation_visits.csv"
    if not side_rec_path.is_file():
        raise SystemExit(f"side REC missing: {side_rec_path}")

    side_notes = _read_csv(side_extracted / "daily_notes.csv")
    side_cpt = _read_csv(side_extracted / "cpt_codes.csv")
    live_notes = _read_csv(BASE / "extracted" / "daily_notes.csv")
    notes_lookup = _note_file_lookup(side_notes)
    notes_lookup.update(_note_file_lookup(live_notes))
    note_emr_dos = _emr_dos_set_from_notes(side_notes) | _emr_dos_set_from_notes(
        live_notes
    )
    cpt_emr_dos = _emr_dos_set_from_cpt(side_cpt)
    rec_keys = _rec_keys(side_rec_path)

    # Snapshot gap before Track F
    before_summary = _compare_visits(
        side_rec_path, out_dir / "sf_compare_before"
    )
    sf_missing_before = int(
        (before_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )

    # --- Phase A: validate ---
    validated: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    with gap_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("root_cause") or "") != "note_exists_cpt_missing":
                continue
            emr = (row.get("emr_id") or "").strip()
            dos = (row.get("date_of_service") or "")[:10]
            base_out = {
                "emr_id": emr,
                "date_of_service": dos,
                "sf_patient": (row.get("sf_patient") or "").strip(),
                "sf_clinic": (row.get("sf_clinic") or "").strip(),
            }
            if not emr or not dos:
                rejected.append({**base_out, "reject_reason": "missing_emr_or_dos"})
                continue
            if (emr, dos) in rec_keys:
                rejected.append({**base_out, "reject_reason": "already_in_side_rec"})
                continue
            if (emr, dos) in cpt_emr_dos:
                rejected.append({**base_out, "reject_reason": "cpt_already_present"})
                continue
            if (emr, dos) not in note_emr_dos:
                rejected.append({**base_out, "reject_reason": "note_missing"})
                continue
            nf = notes_lookup.get((emr, dos), "")
            pdf = _find_pdf(emr, dos, nf)
            if pdf is None:
                rejected.append({**base_out, "reject_reason": "pdf_missing"})
                continue
            validated.append(
                {
                    **base_out,
                    "note_file": nf or pdf.name,
                    "pdf_path": str(pdf),
                }
            )

    _write_csv(
        out_dir / "validated_candidates.csv",
        validated,
        [
            "emr_id",
            "date_of_service",
            "sf_patient",
            "sf_clinic",
            "note_file",
            "pdf_path",
        ],
    )
    _write_csv(
        out_dir / "rejected_candidates.csv",
        rejected,
        ["emr_id", "date_of_service", "sf_patient", "sf_clinic", "reject_reason"],
    )

    # --- Phase B: recover CPT ---
    from chart_notes_parse import (  # noqa: E402
        CPT_CODES_FIELDNAMES,
        cpt_code_rows,
        extract_daily_note,
    )

    recovered_cpt: list[dict[str, str]] = []
    recovery_log: list[dict[str, Any]] = []
    outcomes: Counter[str] = Counter()

    for cand in validated:
        emr = cand["emr_id"]
        dos = cand["date_of_service"]
        pdf = Path(cand["pdf_path"])
        entry: dict[str, Any] = {
            "emr_id": emr,
            "date_of_service": dos,
            "pdf_path": str(pdf),
        }
        try:
            extract = extract_daily_note(pdf, patient_id=emr)
            rows = cpt_code_rows(extract)
        except Exception as exc:  # noqa: BLE001
            outcomes["extract_exception"] += 1
            entry.update({"status": "extract_exception", "error": str(exc), "cpt_n": 0})
            recovery_log.append(entry)
            continue
        if extract.error:
            outcomes["extract_error"] += 1
            entry.update(
                {"status": "extract_error", "error": extract.error, "cpt_n": 0}
            )
            recovery_log.append(entry)
            continue
        if not rows:
            outcomes["cpt_still_empty"] += 1
            entry.update({"status": "cpt_still_empty", "cpt_n": 0})
            recovery_log.append(entry)
            continue
        # Keep only rows matching candidate DOS
        matched = [
            r
            for r in rows
            if (r.get("date_of_daily_note") or "")[:10] == dos
            or not (r.get("date_of_daily_note") or "").strip()
        ]
        if not matched:
            matched = rows
        outcomes["cpt_recovered"] += 1
        recovered_cpt.extend(matched)
        entry.update(
            {
                "status": "cpt_recovered",
                "cpt_n": len(matched),
                "cpt_codes": ",".join(
                    sorted({(r.get("cpt_code") or "") for r in matched if r.get("cpt_code")})
                ),
            }
        )
        recovery_log.append(entry)
        run.obs.mark_success(operation="track_f_reextract", emr_id=emr, dos=dos)

    _write_csv(
        out_dir / "recovered_cpt.csv",
        recovered_cpt,
        list(CPT_CODES_FIELDNAMES),
    )
    (out_dir / "recovery_log.json").write_text(
        json.dumps(
            {
                "ts": utc_now_iso(),
                "validated": len(validated),
                "rejected": len(rejected),
                "outcomes": dict(outcomes),
                "entries": recovery_log,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # --- Phase C: merge CPT into side extracted (preserve notes) ---
    existing_cpt = _read_csv(side_extracted / "cpt_codes.csv")
    seen = {_cpt_key(r) for r in existing_cpt}
    added = 0
    collisions = 0
    for row in recovered_cpt:
        k = _cpt_key(row)
        if not k[0] or not k[2]:
            continue
        if k in seen:
            collisions += 1
            continue
        seen.add(k)
        existing_cpt.append(row)
        added += 1
    _write_csv(
        side_extracted / "cpt_codes.csv",
        existing_cpt,
        list(CPT_CODES_FIELDNAMES),
    )
    merge_summary = {
        "notes_preserved": True,
        "cpt_base": len(existing_cpt) - added,
        "cpt_recovered_rows": len(recovered_cpt),
        "cpt_rows_added": added,
        "cpt_collisions": collisions,
        "side_cpt_total": len(existing_cpt),
        "side_notes_total": len(side_notes),
    }
    (out_dir / "merged_summary.json").write_text(
        json.dumps(merge_summary, indent=2) + "\n", encoding="utf-8"
    )

    # --- Phase D: reconcile ---
    recon_summary: dict[str, Any] = {}
    if not args.skip_reconcile:
        recon_summary = _run_side_reconcile(side_extracted, side_recon)
    if not side_rec_path.is_file():
        raise SystemExit("side REC not produced after reconcile")

    # --- Phase E–F: coverage + integrity ---
    after_summary = _compare_visits(side_rec_path, out_dir / "sf_compare_after")
    sf_missing_after = int(
        (after_summary.get("emr_id") or {}).get("missing_in_ours") or 0
    )
    # Promote Track F compare as authoritative remaining-gap input
    import shutil

    auth_after = run.artifacts / "validation" / "sf_compare_after"
    track_c_bak = run.artifacts / "validation" / "sf_compare_after_track_c"
    if auth_after.is_dir() and not track_c_bak.is_dir():
        shutil.copytree(auth_after, track_c_bak)
    auth_after.mkdir(parents=True, exist_ok=True)
    for src in (out_dir / "sf_compare_after").iterdir():
        if src.is_file():
            shutil.copy2(src, auth_after / src.name)
    net_gap = sf_missing_before - sf_missing_after

    side_rec_rows = _read_csv(side_rec_path)
    side_keys = _rec_keys(side_rec_path)
    baseline_rec = run.run_dir / "baseline" / "reconciliation_visits.csv"
    baseline_keys = _rec_keys(baseline_rec) if baseline_rec.is_file() else set()

    # Candidate acceptance: validated with CPT recovered AND now in side REC
    recovered_ok_keys = {
        (e["emr_id"], e["date_of_service"])
        for e in recovery_log
        if e.get("status") == "cpt_recovered"
    }
    accepted_keys = sorted(k for k in recovered_ok_keys if k in side_keys)
    rejected_after = sorted(k for k in recovered_ok_keys if k not in side_keys)
    failure_reasons = Counter(
        e.get("status") or "unknown" for e in recovery_log if e.get("status") != "cpt_recovered"
    )
    for emr, dos in rejected_after:
        failure_reasons["cpt_recovered_but_not_in_rec"] += 1

    # Integrity
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

    side_notes_after = _read_csv(side_extracted / "daily_notes.csv")
    side_cpt_after = _read_csv(side_extracted / "cpt_codes.csv")
    note_keys = _emr_dos_set_from_notes(side_notes_after)
    cpt_keys = _emr_dos_set_from_cpt(side_cpt_after)
    orphan_cpt = sorted(k for k in (cpt_keys & recovered_ok_keys) if k not in note_keys)
    missing_notes = sorted(k for k in accepted_keys if k not in note_keys)

    # Window-scoped unexpected removals vs pre-Track-F side keys
    # Use before REC snapshot: re-read was after reconcile; capture from before compare is enough for KPI
    # For deletions: compare baseline window to side — same as Track C
    win_start, win_end = "2026-06-01", "2026-07-31"

    def _in_win(dos: str) -> bool:
        return bool(dos) and win_start <= dos[:10] <= win_end

    baseline_win = {k for k in baseline_keys if _in_win(k[1])}
    side_win = {k for k in side_keys if _in_win(k[1])}
    removed = sorted(baseline_win - side_win)

    integrity = {
        "no_duplicate_emr_dos": {
            "pass": len(dups) == 0,
            "duplicate_count": len(dups),
        },
        "no_orphan_cpt_for_recovered_dos": {
            "pass": len(orphan_cpt) == 0,
            "fail_count": len(orphan_cpt),
            "sample": [f"{a}|{b}" for a, b in orphan_cpt[:20]],
        },
        "accepted_have_notes": {
            "pass": len(missing_notes) == 0,
            "fail_count": len(missing_notes),
            "sample": [f"{a}|{b}" for a, b in missing_notes[:20]],
        },
        "merge_notes_preserved": {
            "pass": bool(merge_summary.get("notes_preserved")),
            "side_notes_total": merge_summary.get("side_notes_total"),
        },
        "no_unexpected_rec_deletions_vs_baseline_window": {
            "pass": len(removed) == 0,
            "removed_count": len(removed),
            "sample": [f"{a}|{b}" for a, b in removed[:20]],
            "note": "Scoped to Jun–Jul vs run baseline",
        },
        "reconciliation_output_present": {
            "pass": side_rec_path.is_file() and len(side_rec_rows) > 0,
            "rows": len(side_rec_rows),
        },
    }
    integrity_ok = all(v.get("pass") for v in integrity.values())

    validated_n = len(validated)
    accepted_n = len(accepted_keys)
    recovered_n = len(recovered_ok_keys)
    yield_rate = (accepted_n / validated_n) if validated_n else 0.0
    accept_rate = (accepted_n / recovered_n) if recovered_n else 0.0

    e2e = {
        "run_id": run.run_id,
        "track": "F",
        "root_cause": "note_exists_cpt_missing",
        "ts": utc_now_iso(),
        "promote_blocked": True,
        "validated_candidates": validated_n,
        "rejected_false_positives": len(rejected),
        "cpt_recovered_visits": recovered_n,
        "accepted_into_side_rec": accepted_n,
        "rejected_after_recovery": len(rejected_after),
        "failure_reasons": dict(failure_reasons),
        "coverage_metrics": {
            "sf_missing_before": sf_missing_before,
            "sf_missing_after": sf_missing_after,
            "net_gap_reduction": net_gap,
            "track_f_yield": round(yield_rate, 4),
            "track_f_acceptance_rate": round(accept_rate, 4),
            "formula_yield": "accepted_into_side_rec / validated_candidates",
            "formula_acceptance": "accepted_into_side_rec / cpt_recovered_visits",
        },
        "merge": merge_summary,
        "reconcile": recon_summary,
        "integrity": integrity,
        "integrity_all_pass": integrity_ok,
        "pipeline_pass": integrity_ok and accepted_n > 0 and added > 0,
    }
    (out_dir / "e2e_validation_report.json").write_text(
        json.dumps(e2e, indent=2) + "\n", encoding="utf-8"
    )

    cov_md = [
        "# Track F Coverage Delta",
        "",
        f"**Run:** `{run.run_id}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Validated candidates | {validated_n} |",
        f"| CPT recovered visits | {recovered_n} |",
        f"| Accepted into side REC | {accepted_n} |",
        f"| Rejected (false positive) | {len(rejected)} |",
        f"| SF missing before | {sf_missing_before} |",
        f"| SF missing after | {sf_missing_after} |",
        f"| Net gap reduction | {net_gap} |",
        f"| Track F yield | {100.0 * yield_rate:.1f}% |",
        f"| Acceptance rate | {100.0 * accept_rate:.1f}% |",
        f"| Integrity all pass | {integrity_ok} |",
        "",
        f"Yield formula: `{e2e['coverage_metrics']['formula_yield']}`",
        "",
        "## Failure reasons",
        "",
    ]
    for reason, n in failure_reasons.most_common():
        cov_md.append(f"- `{reason}`: {n}")
    (out_dir / "coverage_delta.md").write_text("\n".join(cov_md) + "\n", encoding="utf-8")

    # --- Phase G: planning vs actual + historical snippet ---
    historical_yield = yield_rate
    variance_pp = (historical_yield - PLANNING_YIELD_F) * 100.0
    pva = [
        "# Track F Planning vs Actual",
        "",
        "> Planning assumptions for prioritization only. Historical is measured.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Planning Yield | {100.0 * PLANNING_YIELD_F:.0f}% |",
        f"| Historical Yield | {100.0 * historical_yield:.1f}% |",
        f"| Variance | {variance_pp:+.1f} pp |",
        f"| Numerator (accepted) | {accepted_n} |",
        f"| Denominator (validated) | {validated_n} |",
        "",
        f"Formula: `{e2e['coverage_metrics']['formula_yield']}`",
        "",
        f"Source: `{out_dir / 'e2e_validation_report.json'}`",
    ]
    (out_dir / "planning_vs_actual.md").write_text("\n".join(pva) + "\n", encoding="utf-8")

    # Persist measured historical fragment for classify hook
    (out_dir / "historical_yield_fragment.json").write_text(
        json.dumps(
            {
                "track": "F",
                "label": "Track F note_exists_cpt_missing",
                "historical_yield": round(historical_yield, 4),
                "historical_yield_pct": f"{100.0 * historical_yield:.1f}%",
                "formula": "accepted_into_side_rec / validated_candidates",
                "inputs": {
                    "accepted_into_side_rec": accepted_n,
                    "validated_candidates": validated_n,
                    "cpt_recovered_visits": recovered_n,
                    "net_gap_reduction": net_gap,
                },
                "source": str(out_dir / "e2e_validation_report.json"),
                "status": "measured",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    fsm_n = _fsm_mark_recovered(
        run.run_dir / "state" / "units.sqlite", set(accepted_keys)
    )

    summary = {
        "run_id": run.run_id,
        "track": "F",
        "validated": validated_n,
        "cpt_recovered": recovered_n,
        "accepted": accepted_n,
        "cpt_rows_added": added,
        "sf_missing_before": sf_missing_before,
        "sf_missing_after": sf_missing_after,
        "net_gap_reduction": net_gap,
        "historical_yield": round(historical_yield, 4),
        "integrity_all_pass": integrity_ok,
        "fsm_units_marked_done": fsm_n,
        "promote_blocked": True,
        "artifacts_dir": str(out_dir),
    }
    (run.run_dir / "summaries" / "track_f_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    run.obs.stage_end("track_f", **summary)
    print(json.dumps(summary, indent=2))
    finish_run(run, status="track_f_done" if integrity_ok else "track_f_failed")
    set_global_obs(None)
    return 0 if integrity_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
