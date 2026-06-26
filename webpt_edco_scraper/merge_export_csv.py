"""Merge patients_recent_10d.csv from multiple discovery output folders."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from export_utils import PATIENT_RECENT_FIELDNAMES


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _patient_key(row: dict[str, str]) -> str:
    fid = str(row.get("facility_id") or row.get("FacilityID") or "").strip()
    pid = str(row.get("patient_id") or row.get("PatientID") or "").strip()
    return f"{fid}:{pid}"


def find_recent_csvs(inputs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        if item.is_file() and item.name == "patients_recent_10d.csv":
            found.append(item)
            continue
        if item.is_dir():
            candidate = item / "patients_recent_10d.csv"
            if candidate.exists():
                found.append(candidate)
    return sorted(set(found))


def merge_patient_csvs(
    csv_paths: list[Path],
    *,
    output_dir: Path,
    output_name: str = "patients_recent_10d.csv",
) -> Path:
    if not csv_paths:
        raise RuntimeError("No patients_recent_10d.csv files found to merge")

    by_key: dict[str, dict[str, str]] = {}
    for path in csv_paths:
        for row in _read_csv_rows(path):
            key = _patient_key(row)
            if not key or key == ":":
                continue
            by_key[key] = row

    merged = list(by_key.values())
    out_path = output_dir / output_name
    _write_csv_rows(out_path, merged, PATIENT_RECENT_FIELDNAMES)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge patients_recent_10d.csv from discovery pool folders"
    )
    parser.add_argument(
        "--input",
        type=Path,
        nargs="+",
        required=True,
        help="Discovery output dirs or CSV paths (e.g. output/discover_30874)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Merged output directory (writes patients_recent_10d.csv)",
    )
    parser.add_argument(
        "--output-name",
        type=str,
        default="patients_recent_10d.csv",
    )
    args = parser.parse_args()

    csv_paths = find_recent_csvs(args.input)
    out_path = merge_patient_csvs(
        csv_paths,
        output_dir=args.output,
        output_name=args.output_name,
    )
    print(f"Merged {len(csv_paths)} file(s) -> {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
