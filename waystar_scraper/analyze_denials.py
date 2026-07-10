"""Analyze enriched denial line data: CARC/RARC, payers, resolutions."""

import argparse
import csv
from pathlib import Path

from denial_categories import analyze_denial_lines


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze denial remit lines")
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Long-format denials lines CSV (from enrich_denials.py)",
    )
    args = parser.parse_args()

    rows = load_rows(args.input.resolve())
    if not rows:
        print("No rows found.")
        raise SystemExit(1)

    stats = analyze_denial_lines(rows)
    unique_denials = len({r.get("denial_id", "") for r in rows})

    print(f"UNIQUE_DENIALS={unique_denials}")
    print(f"TOTAL_LINES={stats['line_count']}")

    print("\n== CARC CODES (top 15) ==")
    for code, count in stats["carc_counts"].most_common(15):
        print(f"{count}\t{code}")

    print("\n== REMARK CODES (top 10) ==")
    for code, count in stats["remark_counts"].most_common(10):
        print(f"{count}\t{code}")

    print("\n== PROCEDURE CODES (top 10) ==")
    for code, count in stats["proc_counts"].most_common(10):
        print(f"{count}\t{code}")

    print("\n== DENIAL CATEGORY ==")
    for name, count in stats["category_counts"].most_common():
        amount = stats["category_amounts"][name]
        print(f"{count}\t${amount:,.0f}\t{name}")

    print("\n== PAYER (top 10) ==")
    for name, count in stats["payer_counts"].most_common(10):
        amount = stats["payer_amounts"][name]
        print(f"{count}\t${amount:,.0f}\t{name}")

    print("\n== RESOLUTION ==")
    for name, count in stats["resolution_counts"].most_common():
        print(f"{count}\t{name}")


if __name__ == "__main__":
    main()
