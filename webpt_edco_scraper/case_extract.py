"""Case-centric daily-note extraction (facility+case from path/manifest)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from case_paths import MANIFEST_FIELDNAMES, parse_facility_case_from_path
from chart_notes_parse import (
    CPT_CODES_FIELDNAMES,
    DAILY_NOTES_FIELDNAMES,
    cpt_code_rows,
    daily_note_row,
    extract_daily_note,
)
from logging_config import get_logger

log = get_logger("case_extract")

def _uniq(cols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


CASE_DAILY_NOTES_FIELDNAMES: list[str] = _uniq(
    [
        "facility_id",
        "case_id",
        "patient_id",
        "date_of_daily_note",
        "daily_note_id",
        "note_file",
        "source_url",
        "downloaded_at",
        "chart_id",
        "visit_id",
        "cnsid",
        "appointment_id",
        *DAILY_NOTES_FIELDNAMES,
    ]
)

CASE_CPT_CODES_FIELDNAMES: list[str] = _uniq(
    [
        "facility_id",
        "case_id",
        "patient_id",
        "date_of_daily_note",
        "daily_note_id",
        "cpt_code",
        "note_file",
        "source_url",
        "downloaded_at",
        *CPT_CODES_FIELDNAMES,
    ]
)


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def _load_manifest_index(cases_dir: Path) -> dict[str, dict[str, str]]:
    """Map absolute/relative pdf path basename+parent → manifest metadata."""
    index: dict[str, dict[str, str]] = {}
    for manifest in cases_dir.glob("*/*/manifests/artifacts_manifest.csv"):
        with manifest.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                path = (row.get("path") or "").strip()
                if not path:
                    continue
                key = Path(path).name
                index[key] = {k: (row.get(k) or "") for k in MANIFEST_FIELDNAMES}
    return index


def iter_case_daily_note_pdfs(cases_dir: Path) -> list[Path]:
    """PDFs under cases/{facility}/{case}/daily_notes|evaluations|progress_notes|other."""
    patterns = (
        "*/**/daily_notes/*DailyNote*.pdf",
        "*/**/daily_notes/*.pdf",
        "*/**/evaluations/*.pdf",
        "*/**/progress_notes/*.pdf",
        "*/**/other/*DailyNote*.pdf",
        "*/**/chart/*DailyNote*.pdf",
    )
    found: dict[str, Path] = {}
    root = Path(cases_dir)
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file():
                found[str(p.resolve())] = p
    # Prefer DailyNote-named files; keep evals too (extract may yield CPT)
    return sorted(found.values(), key=lambda p: str(p))


def require_case_columns(row: dict[str, str]) -> None:
    """S3: refuse rows missing facility_id / case_id / patient_id / DOS."""
    for col in ("facility_id", "case_id", "patient_id", "date_of_daily_note"):
        if not (row.get(col) or "").strip():
            raise ValueError(f"extract row missing required case column {col}")


def case_note_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        (row.get("facility_id") or "").strip(),
        (row.get("case_id") or "").strip(),
        (row.get("patient_id") or "").strip(),
        (row.get("date_of_daily_note") or "")[:10],
        (row.get("daily_note_id") or row.get("note_file") or "").strip(),
    )


def case_cpt_key(row: dict[str, str]) -> tuple[str, str, str, str, str, str]:
    return (
        (row.get("facility_id") or "").strip(),
        (row.get("case_id") or "").strip(),
        (row.get("patient_id") or "").strip(),
        (row.get("date_of_daily_note") or "")[:10],
        (row.get("cpt_code") or "").strip(),
        (row.get("daily_note_id") or "").strip(),
    )


def export_case_daily_notes(
    cases_dir: Path,
    output_dir: Path,
    *,
    require_case_id: bool = True,
) -> dict[str, Any]:
    """Walk cases/ layout; stamp facility_id+case_id from path (never patient folder alone)."""
    cases_dir = Path(cases_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[phase-b] loading manifests under {cases_dir} ...", flush=True)
    manifest_idx = _load_manifest_index(cases_dir)
    print(f"[phase-b] manifest index size={len(manifest_idx)}", flush=True)
    print(f"[phase-b] globbing daily_note PDFs under {cases_dir} ...", flush=True)
    pdfs = iter_case_daily_note_pdfs(cases_dir)
    log.info("Found %d case PDF(s) under %s", len(pdfs), cases_dir)
    print(f"[phase-b] Found {len(pdfs)} case PDF(s)", flush=True)

    daily_rows: list[dict[str, str]] = []
    cpt_rows: list[dict[str, str]] = []
    errors: list[str] = []
    skipped_no_case = 0

    total_pdfs = len(pdfs)
    for i, pdf_path in enumerate(pdfs, start=1):
        if i == 1 or i % 500 == 0 or i == total_pdfs:
            log.info(
                "Case extract progress %d/%d notes=%d cpt=%d errors=%d",
                i,
                total_pdfs,
                len(daily_rows),
                len(cpt_rows),
                len(errors),
            )
            print(
                f"[phase-b] progress {i}/{total_pdfs} notes={len(daily_rows)} "
                f"cpt={len(cpt_rows)} errors={len(errors)}",
                flush=True,
            )
        try:
            facility_id, case_id = parse_facility_case_from_path(pdf_path)
        except ValueError as exc:
            skipped_no_case += 1
            errors.append(f"{pdf_path}: {exc}")
            if require_case_id:
                continue
            raise

        meta = manifest_idx.get(pdf_path.name, {})
        patient_id = (meta.get("patient_id") or "").strip()
        if not patient_id:
            # meta.json sibling
            meta_json = pdf_path.parents[1] / "meta.json"
            if meta_json.is_file():
                try:
                    import json

                    mj = json.loads(meta_json.read_text(encoding="utf-8"))
                    pids = mj.get("patient_ids") or []
                    if pids:
                        patient_id = str(pids[0])
                except (OSError, ValueError):
                    pass
        if not patient_id:
            errors.append(f"{pdf_path}: patient_id missing in manifest/meta")
            if require_case_id:
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
        base["cnsid"] = meta.get("artifact_id", "") if meta.get("doc_source") == "chart_note" else ""
        base["appointment_id"] = ""
        if not base.get("date_of_daily_note") and meta.get("dos"):
            base["date_of_daily_note"] = meta["dos"][:10]

        try:
            require_case_columns(base)
        except ValueError as exc:
            errors.append(f"{pdf_path.name}: {exc}")
            if require_case_id:
                continue
            raise

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

    notes_path = output_dir / "daily_notes.csv"
    cpt_path = output_dir / "cpt_codes.csv"
    _write_csv(notes_path, daily_rows, CASE_DAILY_NOTES_FIELDNAMES)
    _write_csv(cpt_path, cpt_rows, CASE_CPT_CODES_FIELDNAMES)

    summary = {
        "daily_notes_count": len(daily_rows),
        "cpt_lines_count": len(cpt_rows),
        "errors": errors,
        "skipped_no_case": skipped_no_case,
        "daily_notes_path": str(notes_path),
        "cpt_codes_path": str(cpt_path),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info(
        "Case extract: %d notes, %d CPT lines -> %s",
        len(daily_rows),
        len(cpt_rows),
        output_dir,
    )
    return summary
