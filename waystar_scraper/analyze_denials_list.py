"""Analyze scraped denial list data (denials_merged_clean.csv)."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from denials_normalize import parse_money


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def month_key(date_str: str) -> str:
    value = (date_str or "").strip()
    return value[:7] if len(value) >= 7 else "?"


def summarize(rows: list[dict]) -> dict:
    total_denied = sum(parse_money(r.get("denied_amount", "")) for r in rows)
    total_billed = sum(parse_money(r.get("billed_amount", "")) for r in rows)
    total_allowed = sum(parse_money(r.get("allowed_amount", "")) for r in rows)

    def bucket(field: str, top: int | None = None) -> list[tuple[str, int, float]]:
        counts: Counter[str] = Counter()
        amounts: dict[str, float] = defaultdict(float)
        for row in rows:
            key = (row.get(field) or "?").strip() or "?"
            counts[key] += 1
            amounts[key] += parse_money(row.get("denied_amount", ""))
        items = [(name, counts[name], amounts[name]) for name, _ in counts.most_common(top)]
        return items

    months: Counter[str] = Counter()
    month_amounts: dict[str, float] = defaultdict(float)
    for row in rows:
        month = month_key(row.get("denial_date", ""))
        months[month] += 1
        month_amounts[month] += parse_money(row.get("denied_amount", ""))

    return {
        "denial_count": len(rows),
        "total_denied": total_denied,
        "total_billed": total_billed,
        "total_allowed": total_allowed,
        "category": bucket("denial_category"),
        "stage": bucket("stage"),
        "status": bucket("denial_status"),
        "aging": bucket("aging_bucket"),
        "sla": bucket("sla_status"),
        "claim_type": bucket("claim_type"),
        "appeal": bucket("level_of_appeal"),
        "payer": bucket("payer_name", top=15),
        "months": [
            (month, months[month], month_amounts[month])
            for month in sorted(months)
            if month != "?"
        ],
        "can_resubmit": bucket("can_resubmit"),
        "escalation": bucket("escalation_flag"),
        "recommended_action": bucket("recommended_action"),
    }


def print_section(title: str, items: list[tuple[str, int, float]]) -> None:
    print(f"\n== {title} ==")
    for name, count, amount in items:
        print(f"{count}\t${amount:,.0f}\t{name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze scraped denial list data (from denials_merged_clean.csv)"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("output/denials_2026_all/denials_merged_clean.csv"),
        help="Clean denial list CSV (from transform_denials.py)",
    )
    args = parser.parse_args()

    path = args.input.resolve()
    if not path.exists():
        print(f"File not found: {path}")
        print("Run: python transform_denials.py --merged output/denials_2026_all/denials_merged.csv --output-dir output/denials_2026_all")
        raise SystemExit(1)

    rows = load_rows(path)
    if not rows:
        print("No rows found.")
        raise SystemExit(1)

    stats = summarize(rows)

    print(f"UNIQUE_DENIALS={stats['denial_count']}")
    print(f"TOTAL_DENIED=${stats['total_denied']:,.2f}")
    print(f"TOTAL_BILLED=${stats['total_billed']:,.2f}")
    print(f"TOTAL_ALLOWED=${stats['total_allowed']:,.2f}")

    print_section("DENIAL CATEGORY", stats["category"])
    print_section("WORKFLOW STAGE", stats["stage"])
    print_section("DENIAL STATUS", stats["status"])
    print_section("AGING BUCKET", stats["aging"])
    print_section("SLA STATUS", stats["sla"])
    print_section("CLAIM TYPE", stats["claim_type"])
    print_section("APPEAL LEVEL", stats["appeal"])
    print_section("PAYER (top 15)", stats["payer"])

    print("\n== DENIAL MONTH ==")
    for month, count, amount in stats["months"]:
        print(f"{count}\t${amount:,.0f}\t{month}")

    print_section("CAN RESUBMIT", stats["can_resubmit"])
    print_section("ESCALATION FLAG", stats["escalation"])
    print_section("RECOMMENDED ACTION", stats["recommended_action"])


if __name__ == "__main__":
    main()
