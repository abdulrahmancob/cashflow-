"""Match Jul 28/29 unmapped insurer ACH EFTs into payments_unified."""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger

TRACKER = _REPO / "webpt_edco_scraper/Transaction Tracker 2026.xlsx"
PAY = _REPO / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/payments_unified.csv"
CHECKS = (
    _REPO
    / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/insurance_behavior/checks_timeline.csv"
)
FOCUS = {date(2026, 7, 28), date(2026, 7, 29)}
INS_PROC = {
    "HNB ECHO",
    "PNC-ECHO",
    "PAY PLUS",
    "PayPlus",
    "ASHG",
    "Corvel Treasury",
    "LIBERTY MUTUAL",
    "HIC NY",
}


def main() -> int:
    ledger = load_deposit_ledger(TRACKER)
    focus_ins = [
        r
        for r in ledger
        if r["deposit_date"] in FOCUS
        and float(r.get("amount") or 0) > 0
        and (r.get("ach_payer_head") in INS_PROC)
    ]
    tot = sum(float(r["amount"]) for r in focus_ins)
    print(f"Insurer-proc focus deposits: n={len(focus_ins)} amt=${tot:,.2f}")

    eft_to_rows: dict[str, list] = defaultdict(list)
    for r in focus_ins:
        for k in ("eft_1", "eft_2", "check_reference"):
            v = str(r.get(k) or "").strip()
            if v:
                eft_to_rows[v].append(r)
    keys = set(eft_to_rows)
    print(f"EFT keys: {len(keys)}")

    with PAY.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = list(reader.fieldnames or [])
        print("payments_unified cols:", cols)
        check_cols = [
            c
            for c in cols
            if any(x in c.lower() for x in ("check", "eft", "trace", "ref", "payment"))
        ]
        print("check-like:", check_cols)

        matched: dict[str, list] = defaultdict(list)
        scanned = 0
        for row in reader:
            scanned += 1
            for c in check_cols:
                v = str(row.get(c) or "").strip()
                if v and v in keys:
                    matched[v].append(row)
            if scanned % 400000 == 0:
                print(f"  scanned {scanned} matched_keys={len(matched)}")
        print(f"Done payments_unified rows={scanned} matched_eft_keys={len(matched)}")

    if not matched:
        print("NO EFT matches in payments_unified")
    else:
        covered = 0.0
        for r in focus_ins:
            e1 = str(r.get("eft_1") or "")
            e2 = str(r.get("eft_2") or "")
            if e1 in matched or e2 in matched:
                covered += float(r["amount"])
        print(
            f"Insurer-proc deposit $ with EFT matched: ${covered:,.2f} / ${tot:,.2f}"
        )

        by_payor: dict[str, float] = defaultdict(float)
        for eft, rows in matched.items():
            for prow in rows:
                payor = ""
                for c in cols:
                    if c and "payor" in c.lower() and prow.get(c):
                        payor = str(prow[c])
                        break
                paid = 0.0
                for c in ("paid_amount", "amount", "payment_amount", "check_amount"):
                    if c in prow and prow[c]:
                        try:
                            paid = float(str(prow[c]).replace(",", "").replace("$", ""))
                            break
                        except ValueError:
                            pass
                by_payor[payor or "(unknown)"] += paid
            # show one sample
            sample = rows[0]
            print(
                f"  eft={eft} n_pay={len(rows)} sample="
                + str({k: sample.get(k) for k in cols[:10]})
            )

        print("Matched payment $ by payor:")
        for p, a in sorted(by_payor.items(), key=lambda kv: -kv[1])[:25]:
            print(f"  {p}: ${a:,.2f}")

    # checks_timeline
    print("\n--- checks_timeline ---")
    with CHECKS.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        print("cols:", reader.fieldnames)
        hits = 0
        by_payor2: dict[str, float] = defaultdict(float)
        for row in reader:
            hit_key = None
            for c, v in row.items():
                vv = str(v or "").strip()
                if vv in keys:
                    hit_key = vv
                    break
            if hit_key is None:
                continue
            hits += 1
            payor = str(row.get("payor") or row.get("Payor") or "")
            try:
                amt = float(str(row.get("paid_amount") or row.get("amount") or 0).replace(",", ""))
            except ValueError:
                amt = 0.0
            by_payor2[payor or "(unknown)"] += amt
            if hits <= 8:
                print(" hit", {k: row.get(k) for k in (reader.fieldnames or [])[:12]})
        print(f"checks_timeline hits: {hits}")
        for p, a in sorted(by_payor2.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {p}: ${a:,.2f}")

    # Also: SF compare — SF paid but pending in ours, for any focus-related?
    sf_paid = (
        _REPO
        / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/sf_compare/status_mismatch_sf_paid_denied.csv"
    )
    if sf_paid.exists():
        with sf_paid.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            print("\nsf_paid_denied mismatch cols:", reader.fieldnames)
            n = 0
            amt = 0.0
            for row in reader:
                st = str(row.get("sf_status") or row.get("status_sf") or "").lower()
                if "paid" not in st:
                    continue
                n += 1
                for c in row:
                    if "amount" in c.lower() or "paid" in c.lower():
                        try:
                            amt += float(str(row[c] or 0).replace(",", ""))
                            break
                        except ValueError:
                            pass
            print(f"SF-says-paid mismatches: n={n} (amount scan rough=${amt:,.2f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
