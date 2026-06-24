"""Analyze enriched rejected-claims sheets: categories, payers, providers, charges at risk."""

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from rejection_categories import (
    A3_CODE,
    X12_INFO,
    analysis_text,
    categorize,
    month_key,
    parse_money,
)

DEFAULT_SHEETS = [
    Path("output/claims_rejected_rejected_2024_2026/claims_rejected_merged_enriched.csv"),
    Path("output/claims_rejected_rejected_2025_2026/claims_rejected_merged_enriched.csv"),
]


def load_rows_from_sheets(sheets: list[Path]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for sheet in sheets:
        if not sheet.exists():
            continue
        with sheet.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (row.get("claim_id", ""), row.get("instance_id", ""))
                existing = merged.get(key)
                if existing is None or (
                    not existing.get("rejection_messages") and row.get("rejection_messages")
                ):
                    merged[key] = row
    return list(merged.values())


def load_rows_from_input(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze rejected claims: categories, payers, charges"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Single combined CSV (e.g. output/claims_rejected_all/claims_rejected_all_merged.csv)",
    )
    args = parser.parse_args()

    if args.input:
        rows = load_rows_from_input(args.input.resolve())
    else:
        rows = load_rows_from_sheets(DEFAULT_SHEETS)

    if not rows:
        print("No rows found.")
        raise SystemExit(1)

    total_charges = sum(parse_money(r.get("charges", "")) for r in rows)

    print(f"UNIQUE_CLAIMS={len(rows)}")
    print(f"TOTAL_CHARGES={total_charges:,.2f}")

    # rejection level
    print("\n== LEVEL ==")
    level = Counter(r.get("status", "?") for r in rows)
    level_charges = defaultdict(float)
    for r in rows:
        level_charges[r.get("status", "?")] += parse_money(r.get("charges", ""))
    for name, count in level.most_common():
        print(f"{count}\t${level_charges[name]:,.0f}\t{name}")

    # categories (use editor messages if present, else status_detail)
    print("\n== CATEGORY ==")
    cat_count = Counter()
    cat_charges = defaultdict(float)
    for r in rows:
        text = analysis_text(r)
        cat = categorize(text)
        r["_category"] = cat
        cat_count[cat] += 1
        cat_charges[cat] += parse_money(r.get("charges", ""))
    for name, count in cat_count.most_common():
        print(f"{count}\t${cat_charges[name]:,.0f}\t{name}")

    # payers
    print("\n== PAYER ==")
    payer_count = Counter()
    payer_charges = defaultdict(float)
    for r in rows:
        payer = (r.get("payer") or "?").strip()
        payer_count[payer] += 1
        payer_charges[payer] += parse_money(r.get("charges", ""))
    for name, count in payer_count.most_common(10):
        print(f"{count}\t${payer_charges[name]:,.0f}\t{name}")

    # providers
    print("\n== PROVIDER ==")
    prov = Counter((r.get("provider") or "?").strip() for r in rows)
    for name, count in prov.most_common(10):
        print(f"{count}\t{name}")

    # months
    print("\n== MONTH (transaction date) ==")
    months = Counter(month_key(r.get("transaction_date", "")) for r in rows)
    for name in sorted(months):
        print(f"{months[name]}\t{name}")

    # X12 loops
    print("\n== X12 SEGMENTS (top 12) ==")
    segs = Counter()
    for r in rows:
        text = analysis_text(r)
        for m in X12_INFO.findall(text):
            segs[" ".join(m.split()).strip(" .")] += 1
    for name, count in segs.most_common(12):
        print(f"{count}\t{name}")

    # A3 codes
    print("\n== A3 STATUS CODES ==")
    codes = Counter()
    for r in rows:
        for m in A3_CODE.findall(r.get("rejection_original_message", "") or ""):
            codes[m] += 1
    for name, count in codes.most_common(12):
        print(f"{count}\t{name}")

    # repeat offenders: same patient+payer rejected multiple times
    print("\n== REPEATED PATIENTS (>=3 rejected claims) ==")
    pat = Counter()
    pat_charges = defaultdict(float)
    for r in rows:
        key = f"{r.get('patient_name','?')} | {r.get('payer','?')}"
        pat[key] += 1
        pat_charges[key] += parse_money(r.get("charges", ""))
    for name, count in pat.most_common(12):
        if count >= 3:
            print(f"{count}\t${pat_charges[name]:,.0f}\t{name}")

    # category x payer for top categories
    print("\n== TOP CATEGORY PER PAYER (top 6 payers) ==")
    for payer, _ in payer_count.most_common(6):
        cats = Counter(r["_category"] for r in rows if (r.get("payer") or "").strip() == payer)
        top = ", ".join(f"{c} ({n})" for c, n in cats.most_common(2))
        print(f"{payer}: {top}")


if __name__ == "__main__":
    main()
