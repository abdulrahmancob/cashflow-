"""Facility/case directory layout for the Case-centric pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARTIFACT_SUBDIRS = (
    "chart",
    "daily_notes",
    "evaluations",
    "progress_notes",
    "edocs",
    "uploads",
    "other",
    "manifests",
    "raw",
    "parsed",
    "payments",
)

MANIFEST_FIELDNAMES = [
    "facility_id",
    "case_id",
    "patient_id",
    "dos",
    "doc_source",
    "artifact_id",
    "original_filename",
    "path",
    "source_url",
    "downloaded_at",
    "status",
    "size",
    "sha256",
]


def case_root(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    return Path(base_dir) / "cases" / str(facility_id) / str(case_id)


def ensure_case_layout(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    root = case_root(base_dir, facility_id, case_id)
    try:
        from snowflake_pull.case_forensics import io_span  # type: ignore

        with io_span("directory_create"):
            for name in ARTIFACT_SUBDIRS:
                (root / name).mkdir(parents=True, exist_ok=True)
    except Exception:
        for name in ARTIFACT_SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
    return root


def manifest_path(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    return case_root(base_dir, facility_id, case_id) / "manifests" / "artifacts_manifest.csv"


def meta_path(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    return case_root(base_dir, facility_id, case_id) / "meta.json"


def write_case_meta(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    meta: dict[str, Any],
) -> Path:
    ensure_case_layout(base_dir, facility_id, case_id)
    path = meta_path(base_dir, facility_id, case_id)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = {**existing, **meta}
    merged["facility_id"] = str(facility_id)
    merged["case_id"] = str(case_id)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return path


def parse_facility_case_from_path(path: Path, *, cases_root: Path | None = None) -> tuple[str, str]:
    """Derive (facility_id, case_id) from .../cases/{facility}/{case}/...

    Fail-closed: raises ValueError if path is not under a cases/ layout.
    """
    resolved = Path(path).resolve()
    parts = resolved.parts
    try:
        idx = parts.index("cases")
    except ValueError as exc:
        raise ValueError(f"path not under cases/: {path}") from exc
    if idx + 2 >= len(parts):
        raise ValueError(f"cases path missing facility/case segments: {path}")
    facility_id = parts[idx + 1]
    case_id = parts[idx + 2]
    if not facility_id or not case_id or facility_id in ARTIFACT_SUBDIRS:
        raise ValueError(f"invalid facility/case in path: {path}")
    if case_id in ARTIFACT_SUBDIRS:
        raise ValueError(f"invalid case_id segment in path: {path}")
    if cases_root is not None:
        expected = case_root(cases_root, facility_id, case_id).resolve()
        if resolved != expected and expected not in resolved.parents:
            raise ValueError(f"path escapes case root: {path}")
    return facility_id, case_id
