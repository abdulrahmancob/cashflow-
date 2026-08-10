#!/usr/bin/env python3
"""Sample-scoped Phase B: extract daily_notes/CPT for sample case dirs only.

Avoids symlink staging (Path.glob does not reliably follow dir symlinks).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--case-root",
        type=Path,
        default=Path("/data/exports/side_by_side_case"),
    )
    ap.add_argument("--sample-csv", type=Path, default=None)
    args = ap.parse_args()

    sample_csv = args.sample_csv or (
        args.case_root / "reports" / "checked_out_gap_sample_500.csv"
    )
    cases = args.case_root / "cases"
    out_dir = args.case_root / "sample500_extracted"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(sample_csv.open(encoding="utf-8-sig", newline="")))
    pairs = sorted(
        {
            ((r.get("facility_id") or "").strip(), (r.get("case_id") or "").strip())
            for r in rows
            if (r.get("facility_id") or "").strip() and (r.get("case_id") or "").strip()
        }
    )

    sys.path.insert(0, "/app/webpt_edco_scraper")
    sys.path.insert(0, "/app")
    from case_extract import (  # noqa: E402
        CASE_CPT_CODES_FIELDNAMES,
        CASE_DAILY_NOTES_FIELDNAMES,
        _load_manifest_index,
        _write_csv,
        cpt_code_rows,
        daily_note_row,
        extract_daily_note,
        parse_facility_case_from_path,
        require_case_columns,
    )

    # Collect PDFs only under sample case directories (same patterns as iter_case_daily_note_pdfs)
    patterns = (
        "daily_notes/*DailyNote*.pdf",
        "daily_notes/*.pdf",
        "evaluations/*.pdf",
        "progress_notes/*.pdf",
        "other/*DailyNote*.pdf",
        "chart/*DailyNote*.pdf",
    )
    pdfs: list[Path] = []
    missing = 0
    for fac, case_id in pairs:
        case_dir = cases / fac / case_id
        if not case_dir.is_dir():
            missing += 1
            continue
        found: dict[str, Path] = {}
        for pattern in patterns:
            for p in case_dir.glob(pattern):
                if p.is_file():
                    found[str(p.resolve())] = p
        pdfs.extend(found.values())
    pdfs = sorted(set(pdfs), key=lambda p: str(p))
    print(
        f"[sample-extract] cases={len(pairs)} missing_dirs={missing} pdfs={len(pdfs)}",
        flush=True,
    )

    # Manifest index for patient_id / urls — load only sample case manifests
    # Reuse full index under cases/ (may be large but OK)
    print("[sample-extract] loading manifests...", flush=True)
    manifest_idx = _load_manifest_index(cases)

    daily_rows: list[dict[str, str]] = []
    cpt_rows: list[dict[str, str]] = []
    errors: list[str] = []

    for i, pdf_path in enumerate(pdfs, start=1):
        if i == 1 or i % 100 == 0 or i == len(pdfs):
            print(
                f"[sample-extract] progress {i}/{len(pdfs)} notes={len(daily_rows)} "
                f"cpt={len(cpt_rows)} errors={len(errors)}",
                flush=True,
            )
        try:
            facility_id, case_id = parse_facility_case_from_path(pdf_path)
        except ValueError as exc:
            errors.append(f"{pdf_path}: {exc}")
            continue

        meta = manifest_idx.get(pdf_path.name, {})
        patient_id = (meta.get("patient_id") or "").strip()
        if not patient_id:
            meta_json = pdf_path.parents[1] / "meta.json"
            if meta_json.is_file():
                try:
                    mj = json.loads(meta_json.read_text(encoding="utf-8"))
                    pids = mj.get("patient_ids") or []
                    if pids:
                        patient_id = str(pids[0])
                except (OSError, ValueError):
                    pass
        if not patient_id:
            errors.append(f"{pdf_path}: patient_id missing in manifest/meta")
            continue

        extract = extract_daily_note(pdf_path, patient_id=patient_id)
        if extract.error:
            errors.append(f"{pdf_path.name}: {extract.error}")

        base = daily_note_row(extract)
        base["facility_id"] = facility_id
        base["case_id"] = case_id
        base["patient_id"] = patient_id
        base["source_url"] = meta.get("source_url", "")
        base["downloaded_at"] = meta.get("downloaded_at", "")
        base["chart_id"] = ""
        base["visit_id"] = ""
        base["cnsid"] = (
            meta.get("artifact_id", "") if meta.get("doc_source") == "chart_note" else ""
        )
        base["appointment_id"] = ""
        if not base.get("date_of_daily_note") and meta.get("dos"):
            base["date_of_daily_note"] = meta["dos"][:10]

        try:
            require_case_columns(base)
        except ValueError as exc:
            errors.append(f"{pdf_path.name}: {exc}")
            continue

        daily_rows.append(base)
        for crow in cpt_code_rows(extract):
            crow["facility_id"] = facility_id
            crow["case_id"] = case_id
            crow["patient_id"] = patient_id
            crow["source_url"] = base.get("source_url", "")
            crow["downloaded_at"] = base.get("downloaded_at", "")
            if not crow.get("date_of_daily_note"):
                crow["date_of_daily_note"] = base.get("date_of_daily_note", "")
            try:
                require_case_columns(crow)
            except ValueError as exc:
                errors.append(f"{pdf_path.name} cpt: {exc}")
                continue
            cpt_rows.append(crow)

    _write_csv(out_dir / "daily_notes.csv", daily_rows, CASE_DAILY_NOTES_FIELDNAMES)
    _write_csv(out_dir / "cpt_codes.csv", cpt_rows, CASE_CPT_CODES_FIELDNAMES)

    report: dict[str, Any] = {
        "sample_cases": len(pairs),
        "missing_case_dirs": missing,
        "pdfs_found": len(pdfs),
        "daily_notes_count": len(daily_rows),
        "cpt_lines_count": len(cpt_rows),
        "errors": len(errors),
        "error_samples": errors[:30],
        "out_dir": str(out_dir),
    }
    report_path = args.case_root / "reports" / "checked_out_gap_sample_500_extract.json"
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        Path("/tmp/checked_out_gap_sample_500_extract.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    print(json.dumps({k: report[k] for k in report if k != "error_samples"}, indent=2), flush=True)
    for e in errors[:10]:
        print("ERR", e, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
