#!/usr/bin/env python3
"""Rebuild CPT/daily_notes aggregates from case daily_note PDFs (Phase B)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CASE_ROOT = Path("/data/exports/side_by_side_case")
CASES = CASE_ROOT / "cases"
BATCH = CASE_ROOT / "batch_extracted"
EXTRACTED = CASE_ROOT / "extracted"
STALE_MD5 = "9ce532053121afd73bc2ffdf96adacde"
REPORT = CASE_ROOT / "reports" / "phase_b_extract_summary.json"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def main() -> int:
    sys.path.insert(0, "/app/webpt_edco_scraper")
    sys.path.insert(0, "/app")

    from case_extract import export_case_daily_notes

    try:
        from snowflake_pull.case_merge import merge_case_extracted
    except Exception:
        sys.path.insert(0, "/app/snowflake_pull")
        from case_merge import merge_case_extracted  # type: ignore

    print("[phase-b] export_case_daily_notes starting...", flush=True)
    summary = export_case_daily_notes(CASES, BATCH)
    errs = list(summary.get("errors") or [])
    print(
        "[phase-b] export done:",
        {
            "daily_notes_count": summary.get("daily_notes_count"),
            "cpt_lines_count": summary.get("cpt_lines_count"),
            "skipped_no_case": summary.get("skipped_no_case"),
            "errors": len(errs),
        },
        flush=True,
    )
    for e in errs[:20]:
        print("  ERR:", e, flush=True)

    print("[phase-b] merge_case_extracted ...", flush=True)
    merge_stats = merge_case_extracted(EXTRACTED, BATCH, seed="side")
    print("[phase-b] merge:", merge_stats, flush=True)

    cpt = EXTRACTED / "cpt_codes.csv"
    notes = EXTRACTED / "daily_notes.csv"
    cpt_md5 = _md5(cpt) if cpt.is_file() else ""
    cpt_lines = _line_count(cpt)
    note_lines = _line_count(notes)
    pdf_ok = int(summary.get("daily_notes_count") or 0)
    pdf_err = len(errs)
    parse_rate = (pdf_ok / (pdf_ok + pdf_err)) if (pdf_ok + pdf_err) else 0.0

    gate = {
        "cpt_md5": cpt_md5,
        "cpt_md5_changed": bool(cpt_md5) and cpt_md5 != STALE_MD5,
        "cpt_line_count": cpt_lines,
        "daily_notes_line_count": note_lines,
        "cpt_lines_gt_114": cpt_lines > 114,
        "daily_notes_gt_83": note_lines > 83,
        "daily_notes_count": pdf_ok,
        "cpt_lines_count": int(summary.get("cpt_lines_count") or 0),
        "error_count": pdf_err,
        "skipped_no_case": int(summary.get("skipped_no_case") or 0),
        "parse_success_rate": round(parse_rate, 4),
        "merge_stats": merge_stats,
        "errors_sample": errs[:50],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(gate, indent=2), encoding="utf-8")
    print("[phase-b] GATE:", json.dumps(gate, indent=2), flush=True)

    ok = gate["cpt_md5_changed"] and gate["cpt_lines_gt_114"] and gate["daily_notes_gt_83"]
    print(f"[phase-b] DQ_PASS={ok}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
