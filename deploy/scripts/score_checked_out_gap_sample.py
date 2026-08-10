#!/usr/bin/env python3
"""Score WebPT sample download/extract hit rates; emit expand/stop gate."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--case-root",
        type=Path,
        default=Path("/data/exports/side_by_side_case"),
    )
    ap.add_argument("--sample-csv", type=Path, default=None)
    ap.add_argument("--batch-id", default="checked_out_gap_sample_500")
    ap.add_argument("--gate-pct", type=float, default=40.0)
    args = ap.parse_args()

    sample_csv = args.sample_csv or (
        args.case_root / "reports" / "checked_out_gap_sample_500.csv"
    )
    sample = _load_csv(sample_csv)
    cases_root = args.case_root / "cases"
    notes_csv = args.case_root / "extracted" / "daily_notes.csv"
    cpt_csv = args.case_root / "extracted" / "cpt_codes.csv"
    notes_batch = args.case_root / "batch_extracted" / "daily_notes.csv"
    cpt_batch = args.case_root / "batch_extracted" / "cpt_codes.csv"
    notes_sample = args.case_root / "sample500_extracted" / "daily_notes.csv"
    cpt_sample = args.case_root / "sample500_extracted" / "cpt_codes.csv"

    sample_cases = {
        ((r.get("facility_id") or "").strip(), (r.get("case_id") or "").strip())
        for r in sample
    }
    sample_unit_ids = {(r.get("unit_id") or "").strip() for r in sample if r.get("unit_id")}

    def _case_has_any_daily_pdf(fac: str, case_id: str) -> bool:
        d = cases_root / fac / case_id / "daily_notes"
        if not d.is_dir():
            return False
        try:
            next(d.glob("*.pdf"))
            return True
        except StopIteration:
            return False

    def _case_has_dos_daily_pdf(fac: str, case_id: str, dos: str) -> bool:
        """True if a DailyNote PDF filename or sibling path contains the DOS."""
        d = cases_root / fac / case_id / "daily_notes"
        if not d.is_dir() or not dos:
            return False
        for p in d.glob("*.pdf"):
            name = p.name
            if dos in name or dos.replace("-", "") in name:
                return True
        return False

    pdf_hit = 0
    pdf_dos_hit = 0
    for r in sample:
        fac = (r.get("facility_id") or "").strip()
        case_id = (r.get("case_id") or "").strip()
        dos = (r.get("dos") or "")[:10]
        if _case_has_any_daily_pdf(fac, case_id):
            pdf_hit += 1
        if _case_has_dos_daily_pdf(fac, case_id, dos):
            pdf_dos_hit += 1

    def index_notes(path: Path) -> set[tuple[str, str, str]]:
        keys: set[tuple[str, str, str]] = set()
        for row in _load_csv(path):
            case_id = (row.get("case_id") or "").strip()
            dos = (row.get("date_of_daily_note") or row.get("dos") or "")[:10]
            pid = (row.get("patient_id") or "").strip()
            if case_id and dos:
                keys.add((case_id, dos, pid))
        return keys

    def index_cpt(path: Path) -> set[tuple[str, str, str]]:
        keys: set[tuple[str, str, str]] = set()
        for row in _load_csv(path):
            case_id = (row.get("case_id") or "").strip()
            dos = (row.get("date_of_daily_note") or row.get("dos") or "")[:10]
            pid = (row.get("patient_id") or "").strip()
            if case_id and dos:
                keys.add((case_id, dos, pid))
        return keys

    note_keys = index_notes(notes_csv) | index_notes(notes_batch) | index_notes(notes_sample)
    cpt_keys = index_cpt(cpt_csv) | index_cpt(cpt_batch) | index_cpt(cpt_sample)

    # Build richer note index from sample extract preferentially
    def load_note_rows(path: Path) -> list[dict[str, str]]:
        return _load_csv(path)

    note_rows = (
        load_note_rows(notes_sample)
        + load_note_rows(notes_batch)
        + load_note_rows(notes_csv)
    )
    note_hit = 0
    daily_note_id_hit = 0
    cpt_hit = 0
    for r in sample:
        case_id = (r.get("case_id") or "").strip()
        dos = (r.get("dos") or "")[:10]
        pid = (r.get("patient_id") or r.get("emr_id") or "").strip()
        note_ok = False
        dn_ok = False
        for n in note_rows:
            if (n.get("case_id") or "").strip() != case_id:
                continue
            if (n.get("date_of_daily_note") or "")[:10] != dos:
                continue
            if pid and (n.get("patient_id") or "").strip() not in {"", pid}:
                continue
            note_ok = True
            did = (n.get("daily_note_id") or "").strip()
            nfile = (n.get("note_file") or "").lower()
            if did.startswith("DN") or "dailynote" in nfile:
                dn_ok = True
                break
        cpt_ok = any(k[0] == case_id and k[1] == dos for k in cpt_keys)
        if note_ok:
            note_hit += 1
        if dn_ok:
            daily_note_id_hit += 1
        if cpt_ok:
            cpt_hit += 1

    # FSM states for sample units
    db = args.case_root / "case_units.sqlite"
    state_counts: Counter[str] = Counter()
    err_counts: Counter[str] = Counter()
    if db.is_file() and sample_unit_ids:
        conn = sqlite3.connect(str(db), timeout=60)
        cur = conn.cursor()
        for uid in sample_unit_ids:
            row = cur.execute(
                "SELECT state, error_type FROM case_units WHERE unit_id=?", (uid,)
            ).fetchone()
            if row:
                state_counts[row[0] or ""] += 1
                if row[1]:
                    err_counts[row[1]] += 1
            else:
                state_counts["missing_from_fsm"] += 1
        # also batch-level
        batch_states = cur.execute(
            "SELECT state, COUNT(*) FROM case_units WHERE batch_id=? GROUP BY 1",
            (args.batch_id,),
        ).fetchall()
        conn.close()
    else:
        batch_states = []

    n = len(sample) or 1
    pdf_pct = 100.0 * pdf_hit / n
    pdf_dos_pct = 100.0 * pdf_dos_hit / n
    note_pct = 100.0 * note_hit / n
    cpt_pct = 100.0 * cpt_hit / n
    # Gate union: case-level daily_note PDF OR DOS-matched parsed note
    union_hit = 0
    dos_evidence_hit = 0
    for r in sample:
        fac = (r.get("facility_id") or "").strip()
        case_id = (r.get("case_id") or "").strip()
        dos = (r.get("dos") or "")[:10]
        has_pdf = _case_has_any_daily_pdf(fac, case_id)
        has_pdf_dos = _case_has_dos_daily_pdf(fac, case_id, dos)
        has_note = any(k[0] == case_id and k[1] == dos for k in note_keys)
        has_cpt = any(k[0] == case_id and k[1] == dos for k in cpt_keys)
        if has_pdf or has_note:
            union_hit += 1
        if has_pdf_dos or has_note or has_cpt:
            dos_evidence_hit += 1
    union_pct = 100.0 * union_hit / n
    dos_evidence_pct = 100.0 * dos_evidence_hit / n

    # Plan gate: note-or-PDF ≥40% (case-level PDF counts; report DOS-specific too)
    verdict = "expand_to_full" if union_pct >= args.gate_pct else "stop_diagnose"

    report = {
        "batch_id": args.batch_id,
        "units_sampled": len(sample),
        "sample_distinct_cases": len(sample_cases),
        "fsm_states_sample_units": dict(state_counts),
        "fsm_error_types_sample": dict(err_counts.most_common(20)),
        "fsm_states_batch": {s: int(c) for s, c in batch_states},
        "pdf_daily_notes_case_dir_hit": pdf_hit,
        "pdf_pct": round(pdf_pct, 1),
        "pdf_dos_filename_hit": pdf_dos_hit,
        "pdf_dos_pct": round(pdf_dos_pct, 1),
        "parsed_note_hit": note_hit,
        "parsed_note_pct": round(note_pct, 1),
        "daily_note_id_hit": daily_note_id_hit,
        "daily_note_id_pct": round(100.0 * daily_note_id_hit / n, 1),
        "cpt_hit": cpt_hit,
        "cpt_pct": round(cpt_pct, 1),
        "note_or_pdf_union_hit": union_hit,
        "note_or_pdf_union_pct": round(union_pct, 1),
        "dos_specific_evidence_hit": dos_evidence_hit,
        "dos_specific_evidence_pct": round(dos_evidence_pct, 1),
        "gate_pct": args.gate_pct,
        "verdict": verdict,
        "note": (
            "Gate uses case-level daily_note PDF OR DOS-matched parsed note (>= gate_pct). "
            "dos_specific_evidence = DOS PDF filename OR parsed note OR CPT for that DOS. "
            "Do not start full 61k in this pass."
        ),
        "extract": {
            "pdfs_found": None,
            "sample_extracted_notes": str(notes_sample),
            "sample_extracted_cpt": str(cpt_sample),
        },
    }
    out = args.case_root / "reports" / "checked_out_gap_sample_500_results.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
