"""Classify post–Track C EMR-key SF missing visits; emit remaining-gap roadmap.

Offline only. Does not download, reconcile, or promote live REC.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import parse_date  # noqa: E402
from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.facility_map import map_sf_clinic  # noqa: E402
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402

START = date(2026, 6, 1)
END = date(2026, 7, 31)
BROWNSVILLE_FID = "28029"

PLANNING_DISCLAIMER = (
    "Planning assumptions for prioritization only. Not measured values."
)

# Execution slate (authoritative next-work order). C.2 is deferred.
EXECUTION_SLATE: list[dict[str, Any]] = [
    {
        "track": "F",
        "root_cause": "note_exists_cpt_missing",
        "planning_yield": 0.95,
        "confidence": "low",
        "deferred": False,
    },
    {
        "track": "D",
        "root_cause": "dos_before_first_note",
        "planning_yield": 0.55,
        "confidence": "low",
        "deferred": False,
    },
    {
        "track": "E",
        "root_cause": "interior_gap",
        "planning_yield": 0.80,
        "confidence": "low",
        "deferred": False,
    },
    {
        "track": "Brownsville",
        "root_cause": "brownsville",
        "planning_yield": 0.25,
        "confidence": "low",
        "deferred": False,
    },
    {
        "track": "C.2",
        "root_cause": "dos_after_last_note",
        "planning_yield": 0.35,
        "confidence": "medium",
        "deferred": True,
        "confidence_note": (
            "Analogous to prior Track C after_last_note results; "
            "not a re-measure of residual"
        ),
    },
]

# recoverable / effort / pipeline labels only. Yield fields here are NOT used for
# planning tables or execution projection (see planning_yields.json).
CATEGORY_META: dict[str, dict[str, Any]] = {
    "note_exists_cpt_missing": {
        "recoverable": "yes",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "Track F",
        "yield_best": 0.90,
        "yield_expected": 0.75,
        "yield_worst": 0.40,
        "notes": "Note present; extract/scrape CPT lines",
    },
    "interior_gap": {
        "recoverable": "yes",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "Track E",
        "yield_best": 0.80,
        "yield_expected": 0.55,
        "yield_worst": 0.25,
        "notes": "DOS between first/last note; chart-note pull",
    },
    "dos_before_first_note": {
        "recoverable": "yes",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track D",
        "yield_best": 0.70,
        "yield_expected": 0.45,
        "yield_worst": 0.20,
        "notes": "Historical large bucket; same Track C download pattern",
    },
    "dos_after_last_note": {
        "recoverable": "yes",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track C.2",
        "yield_best": 0.70,
        "yield_expected": 0.50,
        "yield_worst": 0.20,
        "notes": "Residual after primary Track C wave + FSM retries",
    },
    "note_and_cpt_exist_recon_missed": {
        "recoverable": "yes",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track G",
        "yield_best": 0.95,
        "yield_expected": 0.80,
        "yield_worst": 0.50,
        "notes": "Data present; reconcile/filter bug",
    },
    "pdf_download_failed": {
        "recoverable": "maybe",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track C.2 retry",
        "yield_best": 0.60,
        "yield_expected": 0.35,
        "yield_worst": 0.10,
        "notes": "FSM DownloadEmpty / auth; reauth + retry",
    },
    "extract_failed": {
        "recoverable": "maybe",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track C.2 retry",
        "yield_best": 0.50,
        "yield_expected": 0.30,
        "yield_worst": 0.10,
        "notes": "Downloaded but DOS absent after extract",
    },
    "brownsville": {
        "recoverable": "maybe",
        "effort": "L",
        "effort_cost": 8,
        "pipeline": "P3 rediscover",
        "yield_best": 0.50,
        "yield_expected": 0.25,
        "yield_worst": 0.05,
        "notes": "Weak schedule overlap; P3 / clinic rediscover",
    },
    "clinic_unmapped": {
        "recoverable": "maybe",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "facility_map / P4",
        "yield_best": 0.60,
        "yield_expected": 0.30,
        "yield_worst": 0.05,
        "notes": "Map SF clinic before scrape",
    },
    "emr_in_export_not_in_rec": {
        "recoverable": "yes",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track H export→REC",
        "yield_best": 0.60,
        "yield_expected": 0.35,
        "yield_worst": 0.10,
        "notes": "In patients export; never landed in REC",
    },
    "emr_not_in_export": {
        "recoverable": "maybe",
        "effort": "L",
        "effort_cost": 8,
        "pipeline": "export refresh / rediscover",
        "yield_best": 0.40,
        "yield_expected": 0.20,
        "yield_worst": 0.05,
        "notes": "EMR absent from WebPT export",
    },
    "patient_known_no_notes": {
        "recoverable": "maybe",
        "effort": "M",
        "effort_cost": 3,
        "pipeline": "Track I note discovery",
        "yield_best": 0.50,
        "yield_expected": 0.25,
        "yield_worst": 0.05,
        "notes": "Patient known; zero chart notes indexed",
    },
    "already_in_side_rec": {
        "recoverable": "no",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "document / refresh compare",
        "yield_best": 1.0,
        "yield_expected": 1.0,
        "yield_worst": 1.0,
        "notes": "Hygiene: already in side REC",
    },
    "outside_service_window": {
        "recoverable": "no",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "document",
        "yield_best": 0.0,
        "yield_expected": 0.0,
        "yield_worst": 0.0,
        "notes": "Outside Jun–Jul window",
    },
    "no_emr_id": {
        "recoverable": "no",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "document",
        "yield_best": 0.0,
        "yield_expected": 0.0,
        "yield_worst": 0.0,
        "notes": "SF row lacks EMR id",
    },
    "name_mismatch": {
        "recoverable": "maybe",
        "effort": "S",
        "effort_cost": 1,
        "pipeline": "measurement / mapping",
        "yield_best": 0.80,
        "yield_expected": 0.50,
        "yield_worst": 0.10,
        "notes": "Name-key vs EMR-key discrepancy evidence",
    },
    "UNKNOWN": {
        "recoverable": "maybe",
        "effort": "L",
        "effort_cost": 8,
        "pipeline": "manual RCA",
        "yield_best": 0.20,
        "yield_expected": 0.05,
        "yield_worst": 0.0,
        "notes": "Must carry full evidence blob",
    },
}


def _in_range(dos: str) -> bool:
    d = parse_date(dos)
    return d is not None and START <= d <= END


def _load_pid_dos_from_notes(paths: Iterable[Path]) -> tuple[dict[str, list[str]], set[tuple[str, str]]]:
    notes_by_pid: dict[str, list[str]] = defaultdict(list)
    notes_pid_dos: set[tuple[str, str]] = set()
    seen_add: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                pid = (row.get("patient_id") or row.get("webpt_patient_id") or "").strip()
                dos = (row.get("date_of_daily_note") or row.get("date_of_service") or "")[:10]
                if not pid or not dos:
                    continue
                key = (pid, dos)
                if key not in notes_pid_dos:
                    notes_pid_dos.add(key)
                if key not in seen_add:
                    notes_by_pid[pid].append(dos)
                    seen_add.add(key)
    for pid, dates in notes_by_pid.items():
        dates.sort()
    return notes_by_pid, notes_pid_dos


def _load_cpt_pid_dos(paths: Iterable[Path]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                pid = (row.get("patient_id") or row.get("webpt_patient_id") or "").strip()
                dos = (row.get("date_of_daily_note") or row.get("date_of_service") or "")[:10]
                if pid and dos:
                    out.add((pid, dos))
    return out


def _load_rec(path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    pids: set[str] = set()
    pid_dos: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("date_of_service") or "").strip()
            if not _in_range(dos):
                continue
            pid = (row.get("webpt_patient_id") or row.get("patient_id") or "").strip()
            if pid:
                pids.add(pid)
                pid_dos.add((pid, dos))
    return pids, pid_dos


def _load_export_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            if pid:
                ids.add(pid)
    return ids


def _load_fsm(db_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Map (emr, dos) → best FSM evidence."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    if not db_path.is_file():
        return out
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT unit_id, emr_id, webpt_patient_id, dos, state, error_type, batch_id "
        "FROM units WHERE batch_id NOT LIKE 'toy%'"
    ).fetchall()
    con.close()
    rank = {
        "failed_terminal": 3,
        "downloaded": 2,
        "done": 1,
        "extracted": 1,
        "reconciled": 1,
        "in_progress": 0,
        "queued": 0,
    }
    for r in rows:
        emr = (r["webpt_patient_id"] or r["emr_id"] or "").strip()
        dos = (r["dos"] or "").strip()[:10]
        if not emr or not dos:
            continue
        key = (emr, dos)
        cand = {
            "unit_id": r["unit_id"],
            "state": r["state"] or "",
            "error_type": r["error_type"] or "",
            "batch_id": r["batch_id"] or "",
        }
        prev = out.get(key)
        if prev is None or rank.get(cand["state"], 0) >= rank.get(prev["state"], 0):
            # Prefer failed_terminal with error over bare done for same key
            if prev and prev.get("error_type") and not cand["error_type"]:
                continue
            out[key] = cand
    return out


def _classify(
    *,
    emr: str,
    dos: str,
    clinic: str,
    rec_pids: set[str],
    rec_pid_dos: set[tuple[str, str]],
    export_ids: set[str],
    notes_by_pid: dict[str, list[str]],
    notes_pid_dos: set[tuple[str, str]],
    cpt_pid_dos: set[tuple[str, str]],
    fsm: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, dict[str, str]]:
    fmap = map_sf_clinic(clinic)
    is_brownsville = (fmap.webpt_facility_id == BROWNSVILLE_FID) or (
        "brownsville" in (clinic or "").lower()
    )
    evidence = {
        "emr_id": emr,
        "date_of_service": dos,
        "sf_clinic": clinic,
        "facility_map_status": fmap.status,
        "webpt_facility_id": fmap.webpt_facility_id or "",
        "is_brownsville": "yes" if is_brownsville else "no",
        "emr_in_export": "yes" if emr in export_ids else "no",
        "emr_in_side_rec": "yes" if emr in rec_pids else "no",
        "dos_in_side_rec": "yes" if (emr, dos) in rec_pid_dos else "no",
        "emr_has_notes": "yes" if emr in notes_by_pid else "no",
        "dos_in_notes": "yes" if (emr, dos) in notes_pid_dos else "no",
        "dos_in_cpt": "yes" if (emr, dos) in cpt_pid_dos else "no",
        "fsm_state": "",
        "fsm_error_type": "",
        "fsm_batch_id": "",
        "fsm_unit_id": "",
        "evidence_json": "",
    }
    unit = fsm.get((emr, dos))
    if unit:
        evidence["fsm_state"] = unit.get("state", "")
        evidence["fsm_error_type"] = unit.get("error_type", "")
        evidence["fsm_batch_id"] = unit.get("batch_id", "")
        evidence["fsm_unit_id"] = unit.get("unit_id", "")

    if not _in_range(dos):
        return "outside_service_window", evidence

    if not emr:
        return "no_emr_id", evidence

    if (emr, dos) in rec_pid_dos:
        return "already_in_side_rec", evidence

    # FSM outcomes for attempted recovery (before temporal note gaps)
    err = evidence["fsm_error_type"]
    if err == "DownloadEmpty" or (
        evidence["fsm_state"] == "failed_terminal" and "Auth" in err
    ):
        return "pdf_download_failed", evidence
    if err == "NoteDosAbsentAfterDownload":
        return "extract_failed", evidence

    if (emr, dos) in notes_pid_dos:
        if (emr, dos) in cpt_pid_dos:
            return "note_and_cpt_exist_recon_missed", evidence
        return "note_exists_cpt_missing", evidence

    notes = notes_by_pid.get(emr) or []
    if notes:
        mn, mx = min(notes), max(notes)
        if dos < mn:
            return "dos_before_first_note", evidence
        if dos > mx:
            return "dos_after_last_note", evidence
        return "interior_gap", evidence

    # No notes for this EMR
    if is_brownsville and (
        emr not in export_ids or emr not in rec_pids or not notes
    ):
        return "brownsville", evidence

    if fmap.status in {"unmapped", "out_of_scope"}:
        return "clinic_unmapped", evidence

    if emr in rec_pids:
        return "patient_known_no_notes", evidence

    if emr in export_ids:
        return "emr_in_export_not_in_rec", evidence

    if emr not in export_ids:
        return "emr_not_in_export", evidence

    evidence["evidence_json"] = json.dumps(evidence, ensure_ascii=False)
    return "UNKNOWN", evidence


def _meta(cat: str) -> dict[str, Any]:
    return CATEGORY_META.get(
        cat,
        {
            "recoverable": "maybe",
            "effort": "L",
            "effort_cost": 8,
            "pipeline": "manual",
            "yield_best": 0.1,
            "yield_expected": 0.0,
            "yield_worst": 0.0,
            "notes": "",
        },
    )


def _planning_by_cause() -> dict[str, dict[str, Any]]:
    return {t["root_cause"]: t for t in EXECUTION_SLATE}


def _build_planning_yields_doc() -> dict[str, Any]:
    tracks = []
    for t in EXECUTION_SLATE:
        row = {
            "track": t["track"],
            "root_cause": t["root_cause"],
            "planning_yield": t["planning_yield"],
            "confidence": t["confidence"],
            "deferred": bool(t.get("deferred")),
        }
        if t.get("confidence_note"):
            row["confidence_note"] = t["confidence_note"]
        tracks.append(row)
    return {"disclaimer": PLANNING_DISCLAIMER, "tracks": tracks}


def _build_historical_yields(run_dir: Path) -> dict[str, Any]:
    """Measured yields from executed Track C only; others N/A."""
    wave_path = run_dir / "summaries" / "track_c_wave_summary.json"
    e2e_path = run_dir / "summaries" / "track_c_e2e_summary.json"
    wave = (
        json.loads(wave_path.read_text(encoding="utf-8"))
        if wave_path.is_file()
        else {}
    )
    e2e = (
        json.loads(e2e_path.read_text(encoding="utf-8"))
        if e2e_path.is_file()
        else {}
    )
    dos_ok = int(wave.get("dos_ok_after_extract") or 0)
    dos_fail = int(wave.get("dos_fail_after_extract") or 0)
    dos_attempted = dos_ok + dos_fail
    download_ok = int(wave.get("download_ok") or 0)
    recovered = int(e2e.get("recovered_units") or 0)
    accepted = int(e2e.get("accepted_new") or 0)

    wave_dos_yield = (dos_ok / dos_attempted) if dos_attempted else None
    e2e_accept_yield = (accepted / recovered) if recovered else None

    tracks = [
        {
            "track": "C",
            "label": "Track C primary (gap_dos_after_last_note)",
            "historical_yield": round(wave_dos_yield, 4) if wave_dos_yield is not None else None,
            "historical_yield_pct": (
                f"{100.0 * wave_dos_yield:.1f}%" if wave_dos_yield is not None else "N/A"
            ),
            "formula": "dos_ok_after_extract / (dos_ok_after_extract + dos_fail_after_extract)",
            "inputs": {
                "dos_ok_after_extract": dos_ok,
                "dos_fail_after_extract": dos_fail,
                "download_ok": download_ok,
            },
            "source": str(wave_path) if wave_path.is_file() else "missing",
            "status": "measured" if wave_dos_yield is not None else "unavailable",
        },
        {
            "track": "C_e2e_accept",
            "label": "Track C E2E acceptance (accepted_new / recovered_units)",
            "historical_yield": (
                round(e2e_accept_yield, 4) if e2e_accept_yield is not None else None
            ),
            "historical_yield_pct": (
                f"{100.0 * e2e_accept_yield:.1f}%"
                if e2e_accept_yield is not None
                else "N/A"
            ),
            "formula": "accepted_new / recovered_units",
            "inputs": {"accepted_new": accepted, "recovered_units": recovered},
            "source": str(e2e_path) if e2e_path.is_file() else "missing",
            "status": "measured" if e2e_accept_yield is not None else "unavailable",
        },
    ]
    measured_by_track: dict[str, dict[str, Any]] = {}
    # Track F / D fragments written by run_track_f.py / run_track_d.py
    for track_letter, frag_name in (
        ("F", "track_f"),
        ("D", "track_d"),
    ):
        frag_path = (
            run_dir / "artifacts" / frag_name / "historical_yield_fragment.json"
        )
        if frag_path.is_file():
            try:
                measured_by_track[track_letter] = json.loads(
                    frag_path.read_text(encoding="utf-8")
                )
            except Exception:
                pass

    for t in EXECUTION_SLATE:
        measured = measured_by_track.get(t["track"])
        if measured and measured.get("status") == "measured":
            frag_fallback = (
                run_dir
                / "artifacts"
                / ("track_f" if t["track"] == "F" else "track_d")
                / "historical_yield_fragment.json"
            )
            tracks.append(
                {
                    "track": t["track"],
                    "label": measured.get("label") or t["root_cause"],
                    "historical_yield": measured.get("historical_yield"),
                    "historical_yield_pct": measured.get("historical_yield_pct")
                    or "N/A",
                    "formula": measured.get("formula"),
                    "inputs": measured.get("inputs") or {},
                    "source": measured.get("source") or str(frag_fallback),
                    "status": "measured",
                }
            )
        else:
            tracks.append(
                {
                    "track": t["track"],
                    "label": t["root_cause"],
                    "historical_yield": None,
                    "historical_yield_pct": "N/A",
                    "formula": None,
                    "inputs": {},
                    "source": "Not executed",
                    "status": "not_executed",
                }
            )
    measured_letters = sorted(
        t["track"] for t in tracks if t.get("status") == "measured"
    )
    next_track = None
    for t in EXECUTION_SLATE:
        if t.get("deferred"):
            continue
        if measured_by_track.get(t["track"], {}).get("status") != "measured":
            next_track = t["track"]
            break
    return {
        "disclaimer": (
            "Historical yields include measured executed Tracks only "
            "(C plus Track fragments under artifacts/, e.g. F and D)."
        ),
        "measured_tracks": measured_letters,
        "next_execution_track": next_track,
        "tracks": tracks,
    }


def _build_track_yield_table(
    by_cause: Counter[str],
) -> dict[str, Any]:
    planning_rows = []
    for i, t in enumerate(EXECUTION_SLATE, 1):
        n = int(by_cause.get(t["root_cause"], 0))
        y = float(t["planning_yield"])
        delta = int(round(n * y))
        planning_rows.append(
            {
                "execution_priority": None if t.get("deferred") else i,
                "track": t["track"],
                "root_cause": t["root_cause"],
                "count": n,
                "planning_yield": y,
                "planning_yield_pct": f"{100.0 * y:.0f}%",
                "confidence": t["confidence"],
                "expected_delta": delta,
                "deferred": bool(t.get("deferred")),
                "confidence_note": t.get("confidence_note") or "",
            }
        )
    # Fix execution_priority numbering excluding deferred
    ep = 0
    for row in planning_rows:
        if row["deferred"]:
            row["execution_priority"] = None
            row["execution_status"] = "deferred"
        else:
            ep += 1
            row["execution_priority"] = ep
            row["execution_status"] = "active"
    return {
        "disclaimer": PLANNING_DISCLAIMER,
        "planning_table": planning_rows,
    }


def _write_breakdown_md(
    path: Path,
    *,
    run_id: str,
    total: int,
    by_cause: Counter[str],
    brownsville_cross: int,
    unknown_n: int,
    historical: dict[str, Any],
    track_table: dict[str, Any],
    execution_order: list[str],
) -> None:
    lines = [
        "# Remaining SF Gap Breakdown",
        "",
        f"**Run:** `{run_id}`",
        "",
        "## Track C status",
        "",
        "- Track C primary (`gap_dos_after_last_note`): **COMPLETED**",
        "- Promote: **DEFERRED** (`promote_blocked: true`)",
        "",
        "## KPI",
        "",
        f"- EMR-key SF missing after: **{total}**",
        f"- UNKNOWN: **{unknown_n}**",
        f"- Brownsville clinic tag (cross-cut, any root cause): **{brownsville_cross}**",
        "",
        "## Historical Yield (measured only)",
        "",
        "| Track | Historical Yield | Source |",
        "|---|---:|---|",
    ]
    for t in historical.get("tracks") or []:
        y = t.get("historical_yield_pct") or "N/A"
        src = t.get("source") or ""
        if t.get("status") == "measured" and t.get("formula"):
            src = f"{t.get('label')}: `{t['formula']}`"
        elif t.get("status") == "not_executed":
            src = "Not executed"
        lines.append(f"| {t.get('track')} | {y} | {src} |")

    lines.extend(
        [
            "",
            "## Planning Yield (assumptions)",
            "",
            f"> **{PLANNING_DISCLAIMER}**",
            "",
            "| Track | Count | Planning Yield | Confidence | Expected Δ | Status |",
            "|---|---:|---:|---|---:|---|",
        ]
    )
    for r in track_table.get("planning_table") or []:
        status = "deferred" if r.get("deferred") else f"P{r.get('execution_priority')}"
        lines.append(
            f"| {r['track']} | {r['count']} | {r['planning_yield_pct']} | "
            f"{str(r['confidence']).title()} | {r['expected_delta']} | {status} |"
        )

    lines.extend(
        [
            "",
            "## Execution priority (authoritative)",
            "",
            f"Order: {' → '.join(execution_order)} ; **C.2 deferred** "
            "(prove Track D before more after_last_note investment).",
            "",
            "## Root cause counts",
            "",
            "| Category | Count | % | Recoverable | Pipeline | Effort |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for cat, n in by_cause.most_common():
        m = _meta(cat)
        pct = 100.0 * n / total if total else 0.0
        lines.append(
            f"| {cat} | {n} | {pct:.2f}% | {m['recoverable']} | {m['pipeline']} | {m['effort']} |"
        )
    lines.extend(["", "## Recoverability notes", ""])
    for cat, n in by_cause.most_common():
        m = _meta(cat)
        lines.append(f"- **{cat}** ({n}): {m['notes']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_priority_matrix(
    by_cause: Counter[str],
    *,
    planning_by_cause: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """score_rank uses planning Δ when available (labeled); not historical."""
    rows: list[dict[str, Any]] = []
    for cat, n in by_cause.items():
        m = _meta(cat)
        plan = planning_by_cause.get(cat)
        if plan:
            y = float(plan["planning_yield"])
            expected = int(round(n * y))
            delta_source = "planning_yields.json"
            confidence = plan.get("confidence")
        else:
            # Not on execution slate — score uses coarse meta only, labeled as such
            y = float(m.get("yield_expected") or 0.0)
            expected = int(round(n * y))
            delta_source = "category_meta_fallback_not_planning_slate"
            confidence = None
        cost = int(m["effort_cost"])
        score = (expected / cost) if cost else 0.0
        rows.append(
            {
                "score_rank": 0,
                "execution_priority": None,
                "root_cause": cat,
                "pipeline": m["pipeline"],
                "track": (plan or {}).get("track") or m["pipeline"],
                "count": n,
                "planning_yield_used_for_score": y if plan else None,
                "delta_source": delta_source,
                "confidence": confidence,
                "estimated_gap_reduction_planning": expected if plan else None,
                "score_delta": expected,
                "effort": m["effort"],
                "effort_cost": cost,
                "score_reduction_per_cost": round(score, 3),
                "score_note": (
                    "Score Δ from planning assumptions, not historical yields"
                    if plan
                    else "Not on execution slate; coarse meta fallback for score only"
                ),
                "recoverable": m["recoverable"],
                "deferred": bool((plan or {}).get("deferred")),
                "dependencies": m["notes"],
                "risk": (
                    "low"
                    if m["effort"] == "S"
                    else ("medium" if m["effort"] == "M" else "high")
                ),
                "recommendation": (
                    "deferred"
                    if (plan or {}).get("deferred")
                    else (
                        "execute"
                        if m["recoverable"] == "yes"
                        else (
                            "pilot"
                            if m["recoverable"] == "maybe"
                            else "document_or_waive"
                        )
                    )
                ),
            }
        )
    rows.sort(
        key=lambda r: (
            0 if r["recoverable"] == "yes" else (1 if r["recoverable"] == "maybe" else 2),
            -r["score_reduction_per_cost"],
            -r["count"],
        )
    )
    for i, r in enumerate(rows, 1):
        r["score_rank"] = i

    ep = 0
    cause_to_ep: dict[str, int | None] = {}
    for t in EXECUTION_SLATE:
        if t.get("deferred"):
            cause_to_ep[t["root_cause"]] = None
        else:
            ep += 1
            cause_to_ep[t["root_cause"]] = ep
    for r in rows:
        if r["root_cause"] in cause_to_ep:
            r["execution_priority"] = cause_to_ep[r["root_cause"]]
    return rows


def _write_roadmap(
    path: Path,
    *,
    run_id: str,
    total: int,
    by_cause: Counter[str],
    matrix: list[dict[str, Any]],
    track_table: dict[str, Any],
    execution_order: list[str],
) -> None:
    lines = [
        "# Coverage Roadmap (post–Track C)",
        "",
        f"**Run:** `{run_id}`",
        f"**Current EMR-key gap:** {total}",
        "",
        "**Promote blocked** until high-priority recoverable Tracks complete or are explicitly waived.",
        "",
        f"> **{PLANNING_DISCLAIMER}**",
        "",
        "## Execution priority (authoritative)",
        "",
        f"{' → '.join(execution_order)}",
        "",
        "- **C.2 deferred** — large prior investment in `after_last_note`; prove Track D first.",
        "",
        "| Exec P | Track | Root cause | Count | Planning Yield | Confidence | Expected Δ |",
        "|---:|---|---|---:|---:|---|---:|",
    ]
    for r in track_table.get("planning_table") or []:
        if r.get("deferred"):
            continue
        lines.append(
            f"| {r['execution_priority']} | {r['track']} | {r['root_cause']} | "
            f"{r['count']} | {r['planning_yield_pct']} | {str(r['confidence']).title()} | "
            f"{r['expected_delta']} |"
        )
    lines.extend(
        [
            "",
            "### Deferred",
            "",
            "| Track | Root cause | Count | Planning Yield | Confidence | Expected Δ |",
            "|---|---|---:|---:|---|---:|",
        ]
    )
    for r in track_table.get("planning_table") or []:
        if not r.get("deferred"):
            continue
        lines.append(
            f"| {r['track']} | {r['root_cause']} | {r['count']} | "
            f"{r['planning_yield_pct']} | {str(r['confidence']).title()} | "
            f"{r['expected_delta']} |"
        )

    lines.extend(
        [
            "",
            "## Score rank (secondary, planning-based Δ)",
            "",
            "Efficiency view only. Δ uses planning assumptions (or coarse meta fallback), "
            "**not** historical yields.",
            "",
            "| Score P | Pipeline | Root cause | Count | Score Δ | Effort |",
            "|---:|---|---|---:|---:|---|",
        ]
    )
    for r in matrix:
        if r["count"] == 0:
            continue
        lines.append(
            f"| {r['score_rank']} | {r['pipeline']} | {r['root_cause']} | {r['count']} | "
            f"{r['score_delta']} | {r['effort']} |"
        )

    for r in track_table.get("planning_table") or []:
        status = "DEFERRED" if r.get("deferred") else f"P{r['execution_priority']}"
        lines.extend(
            [
                "",
                f"## Track {r['track']} — `{r['root_cause']}` ({status})",
                "",
                f"- **Universe count:** {r['count']}",
                f"- **Planning improvement:** ~{r['expected_delta']} "
                f"at {r['planning_yield_pct']} ({r['confidence']} confidence)",
                f"> {PLANNING_DISCLAIMER}",
                "- **Validation plan:** Re-run EMR-key `compare_visits` vs side REC; "
                "delta on `missing_in_ours` must drop; write acceptance CSV.",
                "- **Integrity checks:** Reuse Track C E2E integrity suite.",
                "- **Completion criteria:** Category count → 0 or explicitly waived; "
                "integrity_all_pass; then record **historical** yield for this track.",
            ]
        )

    lines.extend(
        [
            "",
            "## Promote gate",
            "",
            "- `promote_blocked: true`",
            "- Unblock only when execution-priority Tracks (F→D→E→Brownsville) are done "
            "or waived in `summaries/remaining_gap_summary.json`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_projection(
    path: Path,
    *,
    total: int,
    historical: dict[str, Any],
    track_table: dict[str, Any],
) -> dict[str, Any]:
    lines = [
        "# Projected Gap Reduction",
        "",
        f"**Current EMR-key SF missing:** {total}",
        "",
        f"> **{PLANNING_DISCLAIMER}**",
        "",
        "## Historical Yield (measured)",
        "",
        "| Track | Historical Yield | Source |",
        "|---|---:|---|",
    ]
    for t in historical.get("tracks") or []:
        lines.append(
            f"| {t.get('track')} | {t.get('historical_yield_pct') or 'N/A'} | "
            f"{'Not executed' if t.get('status') == 'not_executed' else (t.get('formula') or t.get('source') or '')} |"
        )

    lines.extend(
        [
            "",
            "## Planning Yield table",
            "",
            f"> **{PLANNING_DISCLAIMER}**",
            "",
            "| Track | Count | Planning Yield | Confidence | Expected Δ |",
            "|---|---:|---:|---|---:|",
        ]
    )
    for r in track_table.get("planning_table") or []:
        track_label = (
            f"{r['track']} (deferred)" if r.get("deferred") else r["track"]
        )
        lines.append(
            f"| {track_label} | {r['count']} | {r['planning_yield_pct']} | "
            f"{str(r['confidence']).title()} | {r['expected_delta']} |"
        )

    lines.extend(
        [
            "",
            "## Recommended cumulative path (execution_priority)",
            "",
            "C.2 excluded from main path (deferred).",
            "",
            "| Step | Track | Δ | Remaining | Yield Assumption |",
            "|---:|---|---:|---:|---|",
        ]
    )
    gap = total
    lines.append(f"| 0 | Current | — | {gap} | — |")
    step = 0
    active_deltas: list[tuple[str, int]] = []
    deferred_row = None
    for r in track_table.get("planning_table") or []:
        if r.get("deferred"):
            deferred_row = r
            continue
        step += 1
        delta = int(r["expected_delta"])
        gap = max(0, gap - delta)
        active_deltas.append((r["track"], delta))
        assume = (
            f"Planning {r['planning_yield_pct']} "
            f"({str(r['confidence']).title()})"
        )
        lines.append(
            f"| {step} | {r['track']} | −{delta} | {gap} | {assume} |"
        )

    recommended_remaining = gap
    deferred_remaining = gap
    if deferred_row:
        d_delta = int(deferred_row["expected_delta"])
        deferred_remaining = max(0, gap - d_delta)
        assume = (
            f"Planning {deferred_row['planning_yield_pct']} "
            f"({str(deferred_row['confidence']).title()})"
        )
        lines.extend(
            [
                "",
                "## Deferred appendix (C.2)",
                "",
                "| Step | Track | Δ | Remaining | Yield Assumption |",
                "|---:|---|---:|---:|---|",
                f"| + | {deferred_row['track']} | −{d_delta} | "
                f"{deferred_remaining} | {assume} |",
            ]
        )

    lines.extend(
        [
            "",
            "## Scenario summary (recommended path only)",
            "",
            "| Scenario | Projected remaining gap |",
            "|---|---:|",
            f"| Expected (F→D→E→Brownsville) | {recommended_remaining} |",
            f"| Expected + deferred C.2 | {deferred_remaining} |",
            "",
            "Promote remains blocked until execution-priority work is complete or waived.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "recommended_remaining": recommended_remaining,
        "with_deferred_c2": deferred_remaining,
        "active_deltas": active_deltas,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="classify_remaining_sf_gap.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("remaining_gap")
    run.obs.online = False

    missing_path = (
        run.artifacts / "validation" / "sf_compare_after" / "missing_in_ours_emr_key.csv"
    )
    if not missing_path.is_file():
        raise SystemExit(f"missing EMR-key list: {missing_path}")

    side_rec = (
        run.side_by_side / "reconciliation" / "reconciliation_visits.csv"
    )
    if not side_rec.is_file():
        raise SystemExit(f"side REC missing: {side_rec}")

    base = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
    note_paths = [
        run.side_by_side / "extracted" / "daily_notes.csv",
        base / "extracted" / "daily_notes.csv",
        *[
            p
            for p in (run.artifacts / "gap_batches").glob("*/extracted/daily_notes.csv")
        ],
    ]
    cpt_paths = [
        run.side_by_side / "extracted" / "cpt_codes.csv",
        base / "extracted" / "cpt_codes.csv",
        *[
            p
            for p in (run.artifacts / "gap_batches").glob("*/extracted/cpt_codes.csv")
        ],
    ]
    export_path = base / "patients_export_273d.csv"

    notes_by_pid, notes_pid_dos = _load_pid_dos_from_notes(note_paths)
    cpt_pid_dos = _load_cpt_pid_dos(cpt_paths)
    rec_pids, rec_pid_dos = _load_rec(side_rec)
    export_ids = _load_export_ids(export_path)
    fsm = _load_fsm(run.run_dir / "state" / "units.sqlite")

    out_dir = run.artifacts / "remaining_gap"
    out_dir.mkdir(parents=True, exist_ok=True)

    class_rows: list[dict[str, str]] = []
    by_cause: Counter[str] = Counter()
    brownsville_cross = 0
    unknown_evidence: list[dict[str, str]] = []

    with missing_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            # EMR-key export stores EMR in name_key column
            emr = (row.get("name_key") or row.get("emr_id") or "").strip()
            dos = (row.get("date_of_service") or "").strip()[:10]
            clinic = (row.get("sf_clinic") or "").strip()
            cause, evidence = _classify(
                emr=emr,
                dos=dos,
                clinic=clinic,
                rec_pids=rec_pids,
                rec_pid_dos=rec_pid_dos,
                export_ids=export_ids,
                notes_by_pid=notes_by_pid,
                notes_pid_dos=notes_pid_dos,
                cpt_pid_dos=cpt_pid_dos,
                fsm=fsm,
            )
            by_cause[cause] += 1
            if evidence.get("is_brownsville") == "yes":
                brownsville_cross += 1
            out = {
                "emr_id": emr,
                "date_of_service": dos,
                "sf_patient": (row.get("sf_patient") or "").strip(),
                "sf_clinic": clinic,
                "sf_status": (row.get("sf_status") or "").strip(),
                "sf_total_paid": (row.get("sf_total_paid") or "").strip(),
                "root_cause": cause,
                **evidence,
            }
            if cause == "UNKNOWN":
                out["evidence_json"] = json.dumps(out, ensure_ascii=False)
                unknown_evidence.append(dict(out))
            class_rows.append(out)

    total = len(class_rows)
    if total == 0:
        raise SystemExit("no missing rows loaded")

    csv_path = out_dir / "remaining_gap_breakdown.csv"
    fields = list(class_rows[0].keys())
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(class_rows)

    categories = []
    for cat, n in by_cause.most_common():
        m = _meta(cat)
        categories.append(
            {
                "root_cause": cat,
                "count": n,
                "pct": round(100.0 * n / total, 4),
                "recoverable": m["recoverable"],
                "effort": m["effort"],
                "pipeline": m["pipeline"],
                "notes": m["notes"],
            }
        )

    planning_doc = _build_planning_yields_doc()
    historical = _build_historical_yields(run.run_dir)
    track_table = _build_track_yield_table(by_cause)
    planning_by_cause = _planning_by_cause()
    matrix = _build_priority_matrix(by_cause, planning_by_cause=planning_by_cause)
    unknown_n = by_cause.get("UNKNOWN", 0)
    execution_order = [
        t["track"] for t in EXECUTION_SLATE if not t.get("deferred")
    ]
    deferred_tracks = [t["track"] for t in EXECUTION_SLATE if t.get("deferred")]

    (out_dir / "planning_yields.json").write_text(
        json.dumps(planning_doc, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "historical_yields.json").write_text(
        json.dumps(historical, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "track_yield_table.json").write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "ts": utc_now_iso(),
                "disclaimer": PLANNING_DISCLAIMER,
                "execution_order": execution_order,
                "deferred_tracks": deferred_tracks,
                **track_table,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    breakdown = {
        "run_id": run.run_id,
        "ts": utc_now_iso(),
        "kpi": "emr_id.missing_in_ours",
        "total_missing": total,
        "unknown_count": unknown_n,
        "brownsville_cross_cut": brownsville_cross,
        "track_c_primary": "completed",
        "promote_blocked": True,
        "execution_order": execution_order,
        "deferred_tracks": deferred_tracks,
        "by_root_cause": dict(by_cause),
        "categories": categories,
        "inputs": {
            "missing_emr_key_csv": str(missing_path),
            "side_rec": str(side_rec),
            "note_paths": [str(p) for p in note_paths if p.is_file()],
            "cpt_paths": [str(p) for p in cpt_paths if p.is_file()],
            "export": str(export_path),
            "fsm": str(run.run_dir / "state" / "units.sqlite"),
        },
        "unknown_evidence_sample": unknown_evidence[:20],
        "artifacts": {
            "csv": str(csv_path),
            "json": str(out_dir / "remaining_gap_breakdown.json"),
            "md": str(out_dir / "remaining_gap_breakdown.md"),
            "planning_yields": str(out_dir / "planning_yields.json"),
            "historical_yields": str(out_dir / "historical_yields.json"),
            "track_yield_table": str(out_dir / "track_yield_table.json"),
        },
    }
    (out_dir / "remaining_gap_breakdown.json").write_text(
        json.dumps(breakdown, indent=2) + "\n", encoding="utf-8"
    )
    _write_breakdown_md(
        out_dir / "remaining_gap_breakdown.md",
        run_id=run.run_id,
        total=total,
        by_cause=by_cause,
        brownsville_cross=brownsville_cross,
        unknown_n=unknown_n,
        historical=historical,
        track_table=track_table,
        execution_order=execution_order,
    )

    (out_dir / "priority_matrix.json").write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "ts": utc_now_iso(),
                "promote_blocked": True,
                "execution_order": execution_order,
                "deferred_tracks": deferred_tracks,
                "score_rank_note": (
                    "score_rank Δ from planning_yields.json when on slate; "
                    "not historical yields"
                ),
                "planning_yields": str(out_dir / "planning_yields.json"),
                "historical_yields": str(out_dir / "historical_yields.json"),
                "items": matrix,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_roadmap(
        out_dir / "coverage_roadmap.md",
        run_id=run.run_id,
        total=total,
        by_cause=by_cause,
        matrix=matrix,
        track_table=track_table,
        execution_order=execution_order,
    )
    proj = _write_projection(
        out_dir / "projected_gap_reduction.md",
        total=total,
        historical=historical,
        track_table=track_table,
    )

    summary = {
        "run_id": run.run_id,
        "ts": utc_now_iso(),
        "stage": "remaining_gap",
        "total_missing": total,
        "unknown_count": unknown_n,
        "brownsville_cross_cut": brownsville_cross,
        "by_root_cause": dict(by_cause),
        "top_categories": categories[:12],
        "execution_order": execution_order,
        "deferred_tracks": deferred_tracks,
        "next_execution_track": historical.get("next_execution_track"),
        "measured_tracks": historical.get("measured_tracks"),
        "planning_disclaimer": PLANNING_DISCLAIMER,
        "priority_top_by_score": matrix[:8],
        "projected_final_gap": {
            "recommended_path_expected": proj["recommended_remaining"],
            "recommended_plus_deferred_c2": proj["with_deferred_c2"],
        },
        "track_c_primary": "completed",
        "promote_blocked": True,
        "artifacts_dir": str(out_dir),
        "planning_yields": str(out_dir / "planning_yields.json"),
        "historical_yields": str(out_dir / "historical_yields.json"),
    }
    summaries = run.run_dir / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / "remaining_gap_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    run.obs.stage_end(
        "remaining_gap",
        total_missing=total,
        unknown_count=unknown_n,
        promote_blocked=True,
    )
    print(json.dumps(summary, indent=2))
    finish_run(run, status="remaining_gap_classified")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
