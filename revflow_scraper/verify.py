"""Verify RevFlow export completeness against EOB catalog."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from export import (
    export_file_exists,
    export_filename,
    legacy_export_filename,
    selection_key,
)
from reports_api import load_selections

VERIFY_REPORT_FILENAME = "verify_report.json"


def _selection_from_entry(entry: dict) -> dict:
    return {
        k: entry.get(k)
        for k in [
            "company_id",
            "from_date",
            "to_date",
            "clinic_code",
            "eob_key",
            "check_eft_num",
            "payor",
            "eob_date",
            "detail_rid",
        ]
    }


def load_catalog_entries(catalog_paths: list[Path]) -> list[dict]:
    entries: list[dict] = []
    seen_keys: set[str] = set()
    for path in catalog_paths:
        for entry in load_selections(path):
            key = selection_key(entry)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entries.append(entry)
    return entries


def verify_exports(
    output_dir: Path,
    catalog_paths: list[Path],
    *,
    manifest_path: Path | None = None,
) -> dict:
    exports_dir = output_dir / "exports"
    entries = load_catalog_entries(catalog_paths)

    missing: list[dict] = []
    present_new: list[dict] = []
    present_legacy_only: list[dict] = []
    collision_groups: list[dict] = []
    collision_missing: list[dict] = []
    orphans: list[str] = []

    by_legacy_name: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        sel = _selection_from_entry(entry)
        legacy = legacy_export_filename(sel, ".csv")
        by_legacy_name[legacy].append(sel)

    for legacy_name, group in sorted(by_legacy_name.items()):
        if len(group) <= 1:
            continue
        collision_groups.append(
            {
                "legacy_filename": legacy_name,
                "count": len(group),
                "eob_keys": [s.get("eob_key") for s in group],
                "selections": group,
            }
        )

    matched_files: set[str] = set()
    for entry in entries:
        sel = _selection_from_entry(entry)
        new_path = export_file_exists(exports_dir, sel, include_legacy=False)
        legacy_path = export_file_exists(exports_dir, sel, include_legacy=True)
        if new_path is not None:
            present_new.append(sel)
            matched_files.add(new_path.name)
            continue
        if legacy_path is not None:
            present_legacy_only.append(sel)
            matched_files.add(legacy_path.name)
            legacy_name = legacy_export_filename(sel, ".csv")
            group = by_legacy_name.get(legacy_name, [])
            if len(group) > 1:
                collision_missing.append(
                    {
                        "selection": sel,
                        "legacy_filename": legacy_name,
                        "reason": "legacy_shared_filename",
                    }
                )
            continue
        missing.append(sel)

    if exports_dir.exists():
        for path in sorted(exports_dir.glob("*.csv")) + sorted(exports_dir.glob("*.xlsx")):
            if path.name not in matched_files:
                orphans.append(path.name)

    manifest_errors: list[dict] = []
    if manifest_path and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_errors = [
            e for e in manifest.get("exports", []) if e.get("status") == "error"
        ]

    summary = {
        "catalog_entries": len(entries),
        "present_new_format": len(present_new),
        "present_legacy_only": len(present_legacy_only),
        "missing": len(missing),
        "collision_groups": len(collision_groups),
        "collision_missing": len(collision_missing),
        "orphan_files": len(orphans),
        "manifest_errors": len(manifest_errors),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "catalog_paths": [str(p) for p in catalog_paths],
        "summary": summary,
        "missing": missing,
        "present_legacy_only": present_legacy_only,
        "collision_groups": collision_groups,
        "collision_missing": collision_missing,
        "orphan_files": orphans,
        "manifest_errors": manifest_errors,
    }


def write_missing_selections(report: dict, path: Path) -> int:
    """Write selections that need export (missing + collision victims)."""
    need_export: list[dict] = []
    seen: set[str] = set()
    for item in report.get("missing", []) + [
        c.get("selection") for c in report.get("collision_missing", [])
    ]:
        if not item:
            continue
        key = selection_key(item)
        if key in seen:
            continue
        seen.add(key)
        need_export.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(need_export, indent=2), encoding="utf-8")
    return len(need_export)


def write_verify_report(report: dict, output_dir: Path) -> Path:
    path = output_dir / VERIFY_REPORT_FILENAME
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def print_verify_summary(report: dict) -> None:
    s = report["summary"]
    print("Verify exports summary:")
    print(f"  catalog entries:      {s['catalog_entries']}")
    print(f"  present (new format): {s['present_new_format']}")
    print(f"  present (legacy only):{s['present_legacy_only']}")
    print(f"  missing:              {s['missing']}")
    print(f"  collision groups:     {s['collision_groups']}")
    print(f"  collision missing:    {s['collision_missing']}")
    print(f"  orphan files:         {s['orphan_files']}")
    print(f"  manifest errors:      {s['manifest_errors']}")
