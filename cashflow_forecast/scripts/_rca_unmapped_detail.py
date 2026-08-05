"""Detail unmapped ACH heads on Jul 28/29 + YTD context."""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger

TRACKER = _REPO / "webpt_edco_scraper/Transaction Tracker 2026.xlsx"
FOCUS = {date(2026, 7, 28), date(2026, 7, 29)}
HEADS = {
    "HNB ECHO",
    "PNC-ECHO",
    "PAY PLUS",
    "PayPlus",
    "MERCH SERV",
    "BANK OF AMERICA",
    "Counter",
    "Deposit",
    "ASHG",
    "Corvel Treasury",
    "LIBERTY MUTUAL",
    "HIC NY",
}


def main() -> int:
    ledger = load_deposit_ledger(TRACKER)
    print("=== Unmapped rows detail Jul 28/29 ===")
    for d in sorted(FOCUS):
        print(f"\n--- {d} ---")
        rows = [
            r
            for r in ledger
            if r["deposit_date"] == d
            and float(r["amount"] or 0) > 0
            and not r.get("payer_org")
        ]
        by_head: dict[str, list] = defaultdict(list)
        for r in rows:
            by_head[str(r.get("ach_payer_head") or "(blank)")].append(r)
        for head, rs in sorted(
            by_head.items(), key=lambda kv: -sum(float(x["amount"]) for x in kv[1])
        ):
            tot = sum(float(x["amount"]) for x in rs)
            print(f"  HEAD={head!r} n={len(rs)} tot=${tot:,.2f}")
            for r in sorted(rs, key=lambda x: -float(x["amount"]))[:4]:
                desc = str(r.get("description") or "")[:130]
                print(f"    ${float(r['amount']):,.2f} | {desc}")

    print("\n=== YTD totals for key ACH heads ===")
    ytd: dict[str, dict] = defaultdict(
        lambda: {"amt": 0.0, "n": 0, "mapped_org": set(), "sample": None}
    )
    for r in ledger:
        amt = float(r.get("amount") or 0)
        if amt <= 0:
            continue
        head = str(r.get("ach_payer_head") or "")
        org = str(r.get("payer_org") or "")
        if head in HEADS:
            ytd[head]["amt"] += amt
            ytd[head]["n"] += 1
            if org:
                ytd[head]["mapped_org"].add(org)
            if ytd[head]["sample"] is None:
                ytd[head]["sample"] = str(r.get("description") or "")[:110]

    for h, v in sorted(ytd.items(), key=lambda kv: -kv[1]["amt"]):
        orgs = v["mapped_org"] or {"NONE"}
        print(f"  {h}: YTD=${v['amt']:,.2f} n={v['n']} mapped_orgs={orgs}")
        print(f"    sample: {v['sample']}")

    tot = sum(float(r["amount"]) for r in ledger if float(r.get("amount") or 0) > 0)
    um = sum(
        float(r["amount"])
        for r in ledger
        if float(r.get("amount") or 0) > 0 and not r.get("payer_org")
    )
    print(f"\nYTD Tracker total=${tot:,.2f} unmapped=${um:,.2f} ({um/tot:.1%})")

    # Patient-like heads share on focus days
    patient_like = {"Counter", "MERCH SERV", "Deposit", "BANK OF AMERICA"}
    print("\n=== Patient/merchant-like vs insurer-processor heads on focus ===")
    for d in sorted(FOCUS):
        rows = [
            r
            for r in ledger
            if r["deposit_date"] == d and float(r["amount"] or 0) > 0 and not r.get("payer_org")
        ]
        pl = sum(
            float(r["amount"])
            for r in rows
            if (r.get("ach_payer_head") or "") in patient_like
        )
        other = sum(
            float(r["amount"])
            for r in rows
            if (r.get("ach_payer_head") or "") not in patient_like
        )
        print(f"  {d}: patient/merchant-like unmapped=${pl:,.2f} | other unmapped=${other:,.2f}")
        # break other by head
        by = defaultdict(float)
        for r in rows:
            h = r.get("ach_payer_head") or "(blank)"
            if h not in patient_like:
                by[h] += float(r["amount"])
        for h, a in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"    other {h}: ${a:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
