"""Case-preserving merge into side-by-side Case extracted store."""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SCRAPER = _ROOT / "webpt_edco_scraper"
for _p in (str(_ROOT), str(_SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_extract import (  # noqa: E402
    CASE_CPT_CODES_FIELDNAMES,
    CASE_DAILY_NOTES_FIELDNAMES,
    case_cpt_key,
    case_note_key,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def merge_case_extracted(
    side: Path,
    batch_extracted: Path,
    *,
    seed: str = "side",
) -> dict[str, Any]:
    """Merge Case extracts. Keys include facility+case+patient+DOS+note_id.

    Same patient + same DOS + different case → two rows (never collapsed).
    S4: reject/skip rows lacking case_id; never strip case.
    """
    if seed not in {"side", "empty"}:
        raise ValueError(f"invalid merge seed={seed!r}; expected side|empty")

    side = Path(side)
    batch_extracted = Path(batch_extracted)
    side.mkdir(parents=True, exist_ok=True)

    notes_path = side / "daily_notes.csv"
    cpt_path = side / "cpt_codes.csv"

    if seed == "empty" and not notes_path.is_file():
        _write_csv(notes_path, [], CASE_DAILY_NOTES_FIELDNAMES)
        _write_csv(cpt_path, [], CASE_CPT_CODES_FIELDNAMES)

    if seed == "side":
        if not notes_path.is_file():
            # bootstrap empty case store
            _write_csv(notes_path, [], CASE_DAILY_NOTES_FIELDNAMES)
            _write_csv(cpt_path, [], CASE_CPT_CODES_FIELDNAMES)

    base_notes = _read_csv(notes_path)
    base_cpt = _read_csv(cpt_path)
    gap_notes = _read_csv(batch_extracted / "daily_notes.csv")
    gap_cpt = _read_csv(batch_extracted / "cpt_codes.csv")

    rejected_no_case = 0
    note_seen = {case_note_key(r) for r in base_notes if (r.get("case_id") or "").strip()}
    notes_added = 0
    note_collisions = 0
    for row in gap_notes:
        if not (row.get("case_id") or "").strip() or not (row.get("facility_id") or "").strip():
            rejected_no_case += 1
            continue
        k = case_note_key(row)
        if not k[2] or not k[3]:
            rejected_no_case += 1
            continue
        if k in note_seen:
            note_collisions += 1
            continue
        note_seen.add(k)
        base_notes.append(row)
        notes_added += 1

    cpt_seen = {case_cpt_key(r) for r in base_cpt if (r.get("case_id") or "").strip()}
    cpt_added = 0
    cpt_collisions = 0
    for row in gap_cpt:
        if not (row.get("case_id") or "").strip() or not (row.get("facility_id") or "").strip():
            rejected_no_case += 1
            continue
        k = case_cpt_key(row)
        if not k[2] or not k[3] or not k[4]:
            rejected_no_case += 1
            continue
        if k in cpt_seen:
            cpt_collisions += 1
            continue
        cpt_seen.add(k)
        base_cpt.append(row)
        cpt_added += 1

    # Preserve any extra columns while ensuring case columns exist
    note_fields = list(CASE_DAILY_NOTES_FIELDNAMES)
    for r in base_notes:
        for k in r:
            if k not in note_fields:
                note_fields.append(k)
    cpt_fields = list(CASE_CPT_CODES_FIELDNAMES)
    for r in base_cpt:
        for k in r:
            if k not in cpt_fields:
                cpt_fields.append(k)

    _write_csv(notes_path, base_notes, note_fields)
    _write_csv(cpt_path, base_cpt, cpt_fields)

    return {
        "notes_added": notes_added,
        "cpt_added": cpt_added,
        "note_collisions": note_collisions,
        "cpt_collisions": cpt_collisions,
        "rejected_no_case": rejected_no_case,
        "notes_total": len(base_notes),
        "cpt_total": len(base_cpt),
        "side": str(side),
    }


def copy_case_extracted_seed(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("daily_notes.csv", "cpt_codes.csv"):
        s = src / name
        if s.is_file():
            shutil.copy2(s, dest / name)
