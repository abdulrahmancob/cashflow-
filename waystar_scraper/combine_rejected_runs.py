"""Combine multiple rejected-claim export runs into one deduplicated file with stats."""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from logging_config import get_logger, setup_logging
from merge_exports import claim_key
from rejection_categories import ENRICHED_COLUMNS, analyze_rejection_reasons

log = get_logger("combine")

DEFAULT_OUTPUT_DIR = Path("output/claims_rejected_all")
MERGED_CSV_NAME = "claims_rejected_merged.csv"
ENRICHED_CSV_NAME = "claims_rejected_merged_enriched.csv"


def run_label(export_dir: Path) -> str:
    name = export_dir.name
    if name.startswith("claims_rejected_"):
        return name.removeprefix("claims_rejected_")
    return name


def resolve_merged_csv(export_dir: Path) -> Path:
    merged = export_dir / MERGED_CSV_NAME
    if merged.exists():
        return merged
    enriched = export_dir / ENRICHED_CSV_NAME
    if enriched.exists():
        return enriched
    raise FileNotFoundError(
        f"No {MERGED_CSV_NAME} or {ENRICHED_CSV_NAME} in {export_dir}"
    )


def load_csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_enriched_lookup(export_dir: Path) -> dict[tuple[str, str], dict]:
    enriched_path = export_dir / ENRICHED_CSV_NAME
    if not enriched_path.exists():
        return {}
    lookup: dict[tuple[str, str], dict] = {}
    for row in load_csv_rows(enriched_path):
        lookup[claim_key(row)] = row
    return lookup


def apply_enriched_columns(row: dict, enriched: dict | None) -> dict:
    if not enriched:
        return row
    merged = dict(row)
    for col in ENRICHED_COLUMNS:
        if col not in merged:
            merged[col] = ""
        if not str(merged.get(col, "")).strip() and str(enriched.get(col, "")).strip():
            merged[col] = enriched[col]
    return merged


def parse_scraped_at(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def has_enrichment(row: dict) -> bool:
    return bool(str(row.get("rejection_messages", "")).strip())


def merge_source_runs(*run_strings: str) -> str:
    parts: list[str] = []
    for raw in run_strings:
        for label in (raw or "").split("|"):
            label = label.strip()
            if label and label not in parts:
                parts.append(label)
    return "|".join(parts)


def pick_better_row(existing: dict, candidate: dict) -> dict:
    existing_ts = parse_scraped_at(existing.get("scraped_at", ""))
    candidate_ts = parse_scraped_at(candidate.get("scraped_at", ""))

    winner = candidate
    loser = existing
    if existing_ts > candidate_ts:
        winner, loser = existing, candidate
    elif existing_ts == candidate_ts:
        if has_enrichment(existing) and not has_enrichment(candidate):
            winner, loser = existing, candidate
        else:
            winner, loser = candidate, existing

    merged = dict(winner)
    merged["source_runs"] = merge_source_runs(
        existing.get("source_runs", ""),
        candidate.get("source_runs", ""),
    )

    for col in ENRICHED_COLUMNS:
        if col not in merged:
            merged[col] = ""
        if not str(merged.get(col, "")).strip() and str(loser.get(col, "")).strip():
            merged[col] = loser[col]

    return merged


def load_run(export_dir: Path) -> tuple[str, list[dict], set[tuple[str, str]]]:
    label = run_label(export_dir)
    merged_path = resolve_merged_csv(export_dir)
    enriched_lookup = load_enriched_lookup(export_dir)

    rows: list[dict] = []
    keys: set[tuple[str, str]] = set()
    for row in load_csv_rows(merged_path):
        key = claim_key(row)
        enriched = enriched_lookup.get(key)
        tagged = apply_enriched_columns(row, enriched)
        tagged["source_runs"] = label
        rows.append(tagged)
        keys.add(key)

    log.info("Loaded %s rows from %s (%s)", len(rows), label, merged_path.name)
    return label, rows, keys


def combine_runs(export_dirs: list[Path]) -> tuple[list[dict], dict]:
    per_source: dict[str, int] = {}
    source_keys: dict[str, set[tuple[str, str]]] = {}
    all_rows: list[dict] = []
    total_read = 0

    for export_dir in export_dirs:
        label, rows, keys = load_run(export_dir.resolve())
        per_source[label] = len(rows)
        source_keys[label] = keys
        all_rows.extend(rows)
        total_read += len(rows)

    combined: dict[tuple[str, str], dict] = {}
    for row in all_rows:
        key = claim_key(row)
        if key in combined:
            combined[key] = pick_better_row(combined[key], row)
        else:
            combined[key] = row

    unique_rows = list(combined.values())
    duplicates_removed = total_read - len(unique_rows)
    duplicate_rate_pct = round(
        (duplicates_removed / total_read * 100) if total_read else 0.0, 2
    )

    overlap_matrix: dict[str, int] = {}
    labels = list(source_keys.keys())
    for left, right in combinations(labels, 2):
        overlap = len(source_keys[left] & source_keys[right])
        overlap_matrix[f"{left}_vs_{right}"] = overlap

    summary = {
        "combined_at": datetime.now(timezone.utc).isoformat(),
        "source_dirs": [str(d.resolve()) for d in export_dirs],
        "per_source": per_source,
        "total_rows_read": total_read,
        "unique_claims": len(unique_rows),
        "duplicates_removed": duplicates_removed,
        "duplicate_rate_pct": duplicate_rate_pct,
        "overlap_matrix": overlap_matrix,
    }
    return unique_rows, summary


def write_csv(rows: list[dict], output_path: Path) -> None:
    if not rows:
        raise ValueError("No rows to write")
    base_fields = [k for k in rows[0].keys() if k != "source_runs"]
    fieldnames = base_fields + (["source_runs"] if "source_runs" in rows[0] else [])
    for col in ENRICHED_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    if "source_runs" not in fieldnames:
        fieldnames.append("source_runs")

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(rows: list[dict], summary: dict, output_path: Path) -> None:
    payload = {
        "combined_at": summary.get("combined_at"),
        "source_dirs": summary.get("source_dirs"),
        "total_rows_read": summary.get("total_rows_read"),
        "unique_claims": summary.get("unique_claims"),
        "duplicates_removed": summary.get("duplicates_removed"),
        "claims_count": len(rows),
        "claims": rows,
    }
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def print_report(summary: dict) -> None:
    print(f"\n=== COMBINE SUMMARY ===")
    print(f"Total rows read:     {summary['total_rows_read']}")
    print(f"Unique claims:       {summary['unique_claims']}")
    print(f"Duplicates removed:  {summary['duplicates_removed']} ({summary['duplicate_rate_pct']}%)")
    print("\nPer source:")
    for label, count in summary["per_source"].items():
        print(f"  {label}: {count}")
    print("\nOverlap matrix:")
    for pair, count in summary["overlap_matrix"].items():
        print(f"  {pair}: {count} shared claims")

    reasons = summary.get("rejection_analysis", {})
    print(f"\nTotal charges at risk: ${reasons.get('total_charges', 0):,.2f}")

    print("\n== TOP REJECTION REASONS (status_detail) ==")
    for item in reasons.get("top_status_detail", [])[:15]:
        charges = item.get("charges", 0)
        print(f"  {item['count']:4d}  ${charges:,.0f}  {item['name'][:120]}")

    print("\n== TOP CATEGORIES ==")
    for item in reasons.get("top_categories", [])[:15]:
        charges = item.get("charges", 0)
        print(f"  {item['count']:4d}  ${charges:,.0f}  {item['name']}")

    print("\n== TOP PAYERS ==")
    for item in reasons.get("top_payers", [])[:10]:
        charges = item.get("charges", 0)
        print(f"  {item['count']:4d}  ${charges:,.0f}  {item['name']}")

    print("\n== TOP X12 SEGMENTS ==")
    for item in reasons.get("top_x12_segments", [])[:10]:
        print(f"  {item['count']:4d}  {item['name']}")


def combine_and_export(export_dirs: list[Path], output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, summary = combine_runs(export_dirs)

    analysis = analyze_rejection_reasons(rows)
    summary["rejection_analysis"] = analysis

    csv_path = output_dir / "claims_rejected_all_merged.csv"
    json_path = output_dir / "claims_rejected_all_merged.json"
    summary_path = output_dir / "summary.json"

    write_csv(rows, csv_path)
    write_json(rows, summary, json_path)
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)

    summary["output_csv"] = str(csv_path.resolve())
    summary["output_json"] = str(json_path.resolve())
    summary["output_summary"] = str(summary_path.resolve())
    log.info(
        "Wrote %s unique claims → %s",
        len(rows),
        csv_path,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Combine multiple rejected-claim runs into one deduplicated export"
    )
    parser.add_argument(
        "export_dirs",
        nargs="+",
        type=Path,
        help="Directories containing claims_rejected_merged.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    args = parser.parse_args()
    setup_logging(level=args.log_level)

    try:
        summary = combine_and_export(args.export_dirs, args.output_dir.resolve())
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc

    print_report(summary)
    print(f"\nCSV:     {summary['output_csv']}")
    print(f"JSON:    {summary['output_json']}")
    print(f"Summary: {summary['output_summary']}")


if __name__ == "__main__":
    main()
