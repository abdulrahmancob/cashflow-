"""Apply Snowflake paid/denied status onto reconciliation_visits.csv.

Reads status_mismatch.csv; for rows where sf_status is paid or denied,
overwrites matching visit rows with SF amounts and check fields from
all_billing_data.csv.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import (  # noqa: E402
    format_money,
    name_key_from_webpt,
    parse_money,
)
from snowflake_pull.compare_visits import load_snowflake  # noqa: E402

APPLY_STATUSES = frozenset({"paid", "denied"})


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else _REPO / p


def load_target_keys(mismatch_path: Path) -> dict[tuple[str, str], str]:
    """Return (name_key, dos) -> sf_status for paid/denied mismatches."""
    targets: dict[tuple[str, str], str] = {}
    with mismatch_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            status = (row.get("sf_status") or "").strip().lower()
            if status not in APPLY_STATUSES:
                continue
            key = ((row.get("name_key") or "").strip(), (row.get("date_of_service") or "").strip())
            if not key[0] or not key[1]:
                continue
            targets[key] = status
    return targets


def apply_overrides(
    *,
    mismatch_path: Path,
    snowflake_path: Path,
    visits_path: Path,
    audit_path: Path | None = None,
) -> dict[str, int]:
    targets = load_target_keys(mismatch_path)
    if not targets:
        print("No paid/denied mismatches to apply.")
        return {"targets": 0, "updated_rows": 0, "updated_keys": 0, "missing_keys": 0}

    sf = load_snowflake(snowflake_path, start=None, end=None)

    # Index visit rows by (name_key, dos)
    with visits_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        visits = list(reader)

    by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, row in enumerate(visits):
        nk = name_key_from_webpt(row.get("patient_name") or "")
        dos = (row.get("date_of_service") or "").strip()
        if nk and dos:
            by_key[(nk, dos)].append(i)

    audit_rows: list[dict[str, str]] = []
    updated_rows = 0
    updated_keys = 0
    missing_keys = 0
    multi_keys = 0
    by_status: dict[str, int] = defaultdict(int)

    for key, sf_status_from_mismatch in targets.items():
        sf_row = sf.get(key)
        if sf_row is None:
            missing_keys += 1
            audit_rows.append(
                {
                    "name_key": key[0],
                    "date_of_service": key[1],
                    "old_status": "",
                    "new_status": sf_status_from_mismatch,
                    "old_paid": "",
                    "new_paid": "",
                    "rows_updated": "0",
                    "note": "missing_in_snowflake_aggregate",
                }
            )
            continue

        new_status = sf_row.status if sf_row.status in APPLY_STATUSES else sf_status_from_mismatch
        if new_status not in APPLY_STATUSES:
            continue

        paid_str = format_money(sf_row.total_paid)
        idxs = by_key.get(key, [])
        if not idxs:
            missing_keys += 1
            audit_rows.append(
                {
                    "name_key": key[0],
                    "date_of_service": key[1],
                    "old_status": "",
                    "new_status": new_status,
                    "old_paid": "",
                    "new_paid": paid_str,
                    "rows_updated": "0",
                    "note": "missing_in_visits",
                }
            )
            continue

        if len(idxs) > 1:
            multi_keys += 1

        raw = sf_row.raw
        for i in idxs:
            row = visits[i]
            old_status = (row.get("visit_status") or "").strip()
            old_paid = row.get("visit_paid_total") or row.get("total_paid") or ""

            row["visit_status"] = new_status
            row["total_paid"] = paid_str
            row["visit_paid_total"] = paid_str
            row["matched_paid"] = paid_str
            row["bonus_paid"] = format_money(0.0)
            row["unmatched_paid"] = format_money(0.0)
            row["primary_check_number"] = raw.get("PRIMARY_CHECK_NUMBER") or ""
            row["primary_check_date"] = raw.get("PRIMARY_CHECK_DATE") or ""
            row["primary_check_amount"] = raw.get("PRIMARY_CHECK_AMOUNT") or ""
            row["secondary_check_number"] = raw.get("SECONDARY_CHECK_NUMBER") or ""
            row["secondary_check_date"] = raw.get("SECONDARY_CHECK_DATE") or ""
            row["secondary_check_amount"] = raw.get("SECONDARY_CHECK_AMOUNT") or ""

            updated_rows += 1
            audit_rows.append(
                {
                    "name_key": key[0],
                    "date_of_service": key[1],
                    "old_status": old_status,
                    "new_status": new_status,
                    "old_paid": old_paid,
                    "new_paid": paid_str,
                    "rows_updated": "1",
                    "note": "ok" if len(idxs) == 1 else "multi_match",
                }
            )

        updated_keys += 1
        by_status[new_status] += 1

    # Backup then write
    backup = visits_path.with_suffix(visits_path.suffix + ".bak")
    shutil.copy2(visits_path, backup)

    with visits_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(visits)

    if audit_path is None:
        audit_path = visits_path.parent / "sf_status_overrides_applied.csv"
    audit_fields = [
        "name_key",
        "date_of_service",
        "old_status",
        "new_status",
        "old_paid",
        "new_paid",
        "rows_updated",
        "note",
    ]
    with audit_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=audit_fields)
        writer.writeheader()
        writer.writerows(audit_rows)

    stats = {
        "targets": len(targets),
        "updated_rows": updated_rows,
        "updated_keys": updated_keys,
        "missing_keys": missing_keys,
        "multi_keys": multi_keys,
        "applied_paid": by_status.get("paid", 0),
        "applied_denied": by_status.get("denied", 0),
    }
    print(f"Targets (paid/denied mismatches): {stats['targets']}")
    print(f"Updated visit rows:               {stats['updated_rows']}")
    print(f"Updated keys:                     {stats['updated_keys']}")
    print(f"  -> paid:   {stats['applied_paid']}")
    print(f"  -> denied: {stats['applied_denied']}")
    print(f"Missing keys (no SF or visit):    {stats['missing_keys']}")
    print(f"Multi-match keys:                 {stats['multi_keys']}")
    print(f"Backup:  {backup}")
    print(f"Wrote:   {visits_path}")
    print(f"Audit:   {audit_path}")
    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Apply SF paid/denied status mismatches onto reconciliation_visits.csv"
    )
    p.add_argument(
        "--mismatch",
        default=str(
            _REPO
            / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/sf_compare/status_mismatch.csv"
        ),
    )
    p.add_argument(
        "--snowflake",
        default=str(_REPO / "snowflake_pull/output/all_billing_data.csv"),
    )
    p.add_argument(
        "--visits",
        default=str(
            _REPO
            / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/reconciliation_visits.csv"
        ),
    )
    p.add_argument(
        "--audit",
        default=None,
        help="Audit CSV path (default: next to visits as sf_status_overrides_applied.csv)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mismatch = _resolve(args.mismatch)
    snowflake = _resolve(args.snowflake)
    visits = _resolve(args.visits)
    audit = _resolve(args.audit) if args.audit else None

    for path, label in (
        (mismatch, "mismatch"),
        (snowflake, "snowflake"),
        (visits, "visits"),
    ):
        if not path.exists():
            print(f"Missing {label}: {path}", file=sys.stderr)
            return 1

    apply_overrides(
        mismatch_path=mismatch,
        snowflake_path=snowflake,
        visits_path=visits,
        audit_path=audit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
