"""Merge batch/checkpoint CSV/JSON exports into a single deduplicated file."""

import argparse
import csv
import json
import re
from pathlib import Path

from logging_config import get_logger, setup_logging

log = get_logger("merge")

EXPORT_CSV_PATTERN = re.compile(r"^(batch|checkpoint)_(\d+)\.csv$", re.IGNORECASE)
EXPORT_JSON_PATTERN = re.compile(r"^(batch|checkpoint)_(\d+)\.json$", re.IGNORECASE)


def claim_key(row: dict) -> tuple[str, str]:
    return (str(row.get("claim_id", "")), str(row.get("instance_id", "")))


def merged_output_names(export_dir: Path) -> tuple[Path, Path]:
    name = export_dir.name
    if name.startswith("claims_rejected"):
        base = "claims_rejected_merged"
    elif name.startswith("claims_90d"):
        base = "claims_90d_merged"
    else:
        base = "claims_merged"
    return export_dir / f"{base}.csv", export_dir / f"{base}.json"


def load_export_csvs(export_dir: Path) -> list[tuple[str, int, Path]]:
    exports: list[tuple[str, int, Path]] = []
    for path in export_dir.glob("*.csv"):
        match = EXPORT_CSV_PATTERN.match(path.name)
        if match:
            kind, num = match.group(1).lower(), int(match.group(2))
            exports.append((kind, num, path))
    return sorted(exports, key=lambda item: (item[0], item[1]))


def merge_csv_batches(export_dir: Path, output_path: Path) -> dict:
    exports = load_export_csvs(export_dir)
    if not exports:
        raise FileNotFoundError(
            f"No batch_*.csv or checkpoint_*.csv files found in {export_dir}"
        )

    merged: dict[tuple[str, str], dict] = {}
    total_read = 0

    for kind, export_num, csv_path in exports:
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                total_read += 1
                merged[claim_key(row)] = row
        log.info("Read %s_%03d.csv (%s)", kind, export_num, csv_path.name)

    rows = list(merged.values())
    if rows:
        fieldnames = list(rows[0].keys())
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    duplicates = total_read - len(rows)
    log.info(
        "Merged CSV: %s rows from %s file(s) → %s unique (%s duplicates removed)",
        total_read,
        len(exports),
        len(rows),
        duplicates,
    )
    return {
        "files_merged": len(exports),
        "total_rows_read": total_read,
        "unique_rows": len(rows),
        "duplicates_removed": duplicates,
        "output_csv": str(output_path),
    }


def merge_json_batches(export_dir: Path, output_path: Path, summary: dict) -> None:
    exports: list[tuple[str, int, Path]] = []
    for path in export_dir.glob("*.json"):
        if path.name == "batch_manifest.json":
            continue
        match = EXPORT_JSON_PATTERN.match(path.name)
        if match:
            kind, num = match.group(1).lower(), int(match.group(2))
            exports.append((kind, num, path))

    exports.sort(key=lambda item: (item[0], item[1]))
    all_claims: dict[tuple[str, str], dict] = {}
    meta: dict = {}

    for kind, export_num, json_path in exports:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if not meta:
            meta = {k: v for k, v in payload.items() if k != "claims"}
        for claim in payload.get("claims", []):
            all_claims[claim_key(claim)] = claim
        log.info("Read %s_%03d.json (%s)", kind, export_num, json_path.name)

    merged_payload = {
        **meta,
        "merged_at": summary.get("merged_at"),
        "files_merged": summary.get("files_merged"),
        "total_rows_read": summary.get("total_rows_read"),
        "unique_rows": len(all_claims),
        "duplicates_removed": summary.get("duplicates_removed"),
        "claims_count": len(all_claims),
        "claims": list(all_claims.values()),
    }

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(merged_payload, handle, indent=2, ensure_ascii=False)

    log.info("Merged JSON: %s unique claims → %s", len(all_claims), output_path)


def merge_exports(export_dir: Path) -> dict:
    export_dir = export_dir.resolve()
    if not export_dir.is_dir():
        raise FileNotFoundError(f"Export directory not found: {export_dir}")

    from datetime import datetime, timezone

    csv_out, json_out = merged_output_names(export_dir)

    summary = merge_csv_batches(export_dir, csv_out)
    summary["merged_at"] = datetime.now(timezone.utc).isoformat()
    merge_json_batches(export_dir, json_out, summary)
    summary["output_json"] = str(json_out)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge Waystar batch/checkpoint CSV/JSON exports into one deduplicated file"
    )
    parser.add_argument(
        "export_dir",
        type=Path,
        help="Directory containing batch_001.csv, checkpoint_001.csv, ...",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    args = parser.parse_args()
    setup_logging(level=args.log_level)

    try:
        summary = merge_exports(args.export_dir)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc

    print(
        f"Merged {summary['unique_rows']} unique claims from {summary['files_merged']} file(s)"
    )
    print(f"  CSV:  {summary['output_csv']}")
    print(f"  JSON: {summary['output_json']}")
    if summary["duplicates_removed"]:
        print(f"  ({summary['duplicates_removed']} duplicate rows removed)")


if __name__ == "__main__":
    main()
