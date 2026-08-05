"""READ-ONLY RCA: Expected land underpredict vs bank/Tracker Jul 28-29 2026."""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money
from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger
from cashflow_reconcile.payer_registry import resolve, extract_ach_payer_head

FORECAST = _REPO / "webpt_edco_scraper/output/jun_jul_2026/forecast"
TRACKER = _REPO / "webpt_edco_scraper/Transaction Tracker 2026.xlsx"
FOCUS = {date(2026, 7, 28), date(2026, 7, 29)}
BANK = {date(2026, 7, 28): 38300.0, date(2026, 7, 29): 56800.0}
AS_OF = date(2026, 7, 30)
EOB_WINDOW = 1  # days around deposit for eob match


def _parse_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text or text.lower() in {"nan", "none"}:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _payer_org_outcome(ins: str, rev: str) -> str:
    hit = (
        resolve(rev, "revflow")
        or resolve(ins, "webpt")
        or resolve(ins, "any")
        or resolve(rev, "any")
    )
    if hit is not None:
        return hit.name
    return ins or rev or "(blank)"


def money(x: float) -> str:
    return f"${x:,.2f}"


def main() -> int:
    print("=" * 72)
    print("RCA: Expected land vs bank/Tracker 2026-07-28 / 2026-07-29")
    print(f"as_of={AS_OF}  tracker={TRACKER.name}")
    print("=" * 72)

    # --- 1/2 Tracker deposits ---
    ledger = load_deposit_ledger(TRACKER)
    focus_rows = [r for r in ledger if r.get("deposit_date") in FOCUS and float(r.get("amount") or 0) > 0]
    by_day: dict[date, list] = defaultdict(list)
    for r in focus_rows:
        by_day[r["deposit_date"]].append(r)

    print("\n### 1) Tracker deposits on Jul 28/29 vs bank")
    tracker_day_total: dict[date, float] = {}
    for d in sorted(FOCUS):
        rows = by_day.get(d, [])
        tot = sum(float(r["amount"]) for r in rows)
        tracker_day_total[d] = tot
        bank = BANK[d]
        print(
            f"  {d}: Tracker={money(tot)}  bank={money(bank)}  "
            f"delta_tracker_vs_bank={money(tot - bank)}  n_rows={len(rows)}"
        )
    if not focus_rows:
        print("  *** NO Tracker deposits found for Jul 28/29 ***")

    print("\n### 2) Payer breakdown (payer_registry)")
    day_payer: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    day_unmapped: dict[date, float] = defaultdict(float)
    day_unmapped_heads: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in focus_rows:
        d = r["deposit_date"]
        amt = float(r["amount"])
        org = str(r.get("payer_org") or "").strip()
        head = str(r.get("ach_payer_head") or "").strip()
        if not org:
            org = "(unmapped)"
            day_unmapped[d] += amt
            day_unmapped_heads[d][head or "(blank)"] += amt
        day_payer[d][org] += amt

    for d in sorted(FOCUS):
        print(f"\n  --- {d} ---")
        items = sorted(day_payer[d].items(), key=lambda kv: -kv[1])
        for name, amt in items:
            share = amt / tracker_day_total[d] if tracker_day_total[d] else 0
            print(f"    {name}: {money(amt)} ({share:.1%})")
        print(
            f"    UNMAPPED total: {money(day_unmapped[d])} "
            f"({day_unmapped[d]/tracker_day_total[d]:.1%} of day)"
            if tracker_day_total[d]
            else "    UNMAPPED: n/a"
        )
        if day_unmapped_heads[d]:
            print("    Unmapped ACH heads:")
            for h, a in sorted(day_unmapped_heads[d].items(), key=lambda kv: -kv[1])[:15]:
                print(f"      '{h}': {money(a)}")

    # --- Load outcomes (stream) ---
    print("\n### Loading outcome_stages (streaming)...")
    outcomes_path = FORECAST / "outcome_stages.csv"
    # paid by eob near focus / original_forecast on focus
    paid_near_eob: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    paid_near_eob_total: dict[date, float] = defaultdict(float)
    paid_on_orig_fd: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    paid_on_orig_fd_total: dict[date, float] = defaultdict(float)
    # expected land open AR by day+payer
    land_by_day_payer: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    land_by_day_stage: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    land_total: dict[date, float] = defaultdict(float)
    # SF / denied / paid without eob proxies
    sf_paid_no_eob_amt = 0.0
    sf_paid_no_eob_n = 0
    denied_amt_total = 0.0
    denied_n = 0
    denied_on_focus_land = 0.0  # denied with orig fd on focus (shouldn't land)
    stage_counts = defaultdict(int)
    dos_max = None
    dos_min = None
    pending_sf_like = 0.0  # reconcile pending but source sf?
    source_counts = defaultdict(lambda: defaultdict(float))  # source -> stage -> expected/paid

    # Also collect paid amounts by eob_date for focus ± window with payer
    paid_eob_exact: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            stage_counts[stage] += 1
            source = (row.get("source") or "").strip() or "(blank)"
            ins = (row.get("ins_name") or "").strip()
            rev = (row.get("insurance_revflow") or "").strip()
            org = _payer_org_outcome(ins, rev)
            dos = _parse_day(row.get("date_of_service"))
            if dos:
                if dos_max is None or dos > dos_max:
                    dos_max = dos
                if dos_min is None or dos < dos_min:
                    dos_min = dos
            paid = parse_money(row.get("paid_amount") or "0")
            exp = parse_money(row.get("expected_amount") or "0")
            denied = parse_money(row.get("denied_amount") or "0")
            eob = _parse_day(row.get("eob_date"))
            orig = _parse_day(row.get("original_forecast_date")) or _parse_day(
                row.get("forecast_date")
            )
            recon = (row.get("reconcile_status") or "").strip().lower()

            if denied > 0 or stage == "denied":
                denied_amt_total += denied if denied > 0 else exp
                denied_n += 1
                if orig in FOCUS:
                    denied_on_focus_land += denied if denied > 0 else exp

            if source == "sf_override" and stage == "paid" and eob is None and paid > 0:
                sf_paid_no_eob_amt += paid
                sf_paid_no_eob_n += 1

            # paid without eob more broadly (any source)
            if stage == "paid" and eob is None and paid > 0:
                source_counts[f"paid_no_eob:{source}"]["amt"] += paid
                source_counts[f"paid_no_eob:{source}"]["n"] += 1

            if stage in {"on_track", "overdue"} and exp > 0 and orig in FOCUS:
                land_by_day_payer[orig][org] += exp
                land_by_day_stage[orig][stage] += exp
                land_total[orig] += exp

            if stage == "paid" and paid > 0:
                if eob is not None:
                    for d in FOCUS:
                        if abs((eob - d).days) <= EOB_WINDOW:
                            paid_near_eob[d][org] += paid
                            paid_near_eob_total[d] += paid
                    if eob in FOCUS:
                        paid_eob_exact[eob][org] += paid
                if orig in FOCUS:
                    paid_on_orig_fd[orig][org] += paid
                    paid_on_orig_fd_total[orig] += paid

            if recon in {"pending", ""} and stage in {"on_track", "overdue"} and source.startswith("sf"):
                pending_sf_like += exp

    print(f"  DOS coverage: {dos_min} .. {dos_max}")
    print(f"  stage counts (sample): {dict(sorted(stage_counts.items(), key=lambda x: -x[1])[:10])}")

    print("\n### Expected land (on_track+overdue) on focus days")
    for d in sorted(FOCUS):
        print(
            f"  {d}: {money(land_total[d])}  "
            f"(on_track={money(land_by_day_stage[d]['on_track'])}, "
            f"overdue={money(land_by_day_stage[d]['overdue'])})"
        )
        bank = BANK[d]
        tr = tracker_day_total.get(d, 0)
        actual = tr if tr > 0 else bank
        print(
            f"    gap bank-land={money(bank - land_total[d])}  "
            f"gap tracker-land={money(tr - land_total[d])}"
        )

    # --- 3) Match Tracker deposits to paid outcomes ---
    print("\n### 3) Tracker deposits vs paid outcomes (by payer_org)")
    print("  Matching: paid with eob_date within ±1 day of deposit, OR paid with original_forecast_date on deposit day")
    for d in sorted(FOCUS):
        tr_payers = day_payer[d]
        tr_tot = tracker_day_total[d]
        matched_eob = 0.0
        matched_orig = 0.0
        # For each tracker payer, how much paid outcomes exist
        print(f"\n  --- {d} Tracker={money(tr_tot)} ---")
        payer_rows = []
        for org, dep_amt in sorted(tr_payers.items(), key=lambda kv: -kv[1]):
            eob_amt = paid_near_eob[d].get(org, 0.0) + (
                0 if org != "(unmapped)" else 0
            )
            # also try exact eob
            eob_exact = paid_eob_exact[d].get(org, 0.0)
            orig_amt = paid_on_orig_fd[d].get(org, 0.0)
            land_amt = land_by_day_payer[d].get(org, 0.0)
            # matched to AR: min(deposit, paid_eob±1) as upper bound attribution
            attr_eob = min(dep_amt, paid_near_eob[d].get(org, 0.0))
            attr_orig = min(dep_amt, orig_amt)
            matched_eob += attr_eob
            matched_orig += attr_orig
            payer_rows.append(
                (org, dep_amt, paid_near_eob[d].get(org, 0.0), eob_exact, orig_amt, land_amt)
            )
        # Also sum paid near eob for payers NOT in tracker that day
        paid_only = set(paid_near_eob[d]) - set(tr_payers)
        print(
            f"  Sum min(dep, paid_eob±1) by matching payer: {money(matched_eob)}"
        )
        print(
            f"  Sum min(dep, paid_orig_fd) by matching payer: {money(matched_orig)}"
        )
        print(
            f"  Total paid eob±1 all payers: {money(paid_near_eob_total[d])}"
        )
        print(
            f"  Total paid orig_fd all payers: {money(paid_on_orig_fd_total[d])}"
        )
        # Unmatched deposit $ = tracker - attributed eob (payer-level)
        unmatched_dep = max(0.0, tr_tot - matched_eob)
        print(
            f"  Tracker $ with NO matching paid eob±1 at same payer_org: "
            f"~{money(unmatched_dep)} ({unmatched_dep/tr_tot:.1%} of tracker)"
            if tr_tot
            else "  (no tracker)"
        )
        print("  Top payers: deposit | paid_eob±1 | paid_eob_exact | paid_orig_fd | open_land")
        for org, dep_amt, near, exact, orig_amt, land_amt in payer_rows[:20]:
            print(
                f"    {org}: dep={money(dep_amt)} | eob±1={money(near)} | "
                f"eob={money(exact)} | paid_orig={money(orig_amt)} | land={money(land_amt)}"
            )

    # --- 4) Missing/open AR: land vs deposit by depositing payers ---
    print("\n### 4) For payers that deposited: Expected land that day vs deposit")
    for d in sorted(FOCUS):
        print(f"\n  --- {d} ---")
        shortfall = 0.0
        excess_land = 0.0
        zero_land_dep = 0.0
        for org, dep_amt in sorted(day_payer[d].items(), key=lambda kv: -kv[1]):
            if org == "(unmapped)":
                continue
            land_amt = land_by_day_payer[d].get(org, 0.0)
            gap = dep_amt - land_amt
            if land_amt <= 1e-9 and dep_amt > 0:
                zero_land_dep += dep_amt
            if gap > 0:
                shortfall += gap
            else:
                excess_land += -gap
            if dep_amt >= 500 or abs(gap) >= 500:
                print(
                    f"    {org}: dep={money(dep_amt)} land={money(land_amt)} "
                    f"gap(dep-land)={money(gap)}"
                )
        print(
            f"  Payers with deposit but ZERO open land: {money(zero_land_dep)}"
        )
        print(
            f"  Sum positive (dep-land) for mapped depositing payers: {money(shortfall)}"
        )
        print(
            f"  Sum where land>dep (excess scheduled): {money(excess_land)}"
        )
        # land from payers that did NOT deposit
        land_no_dep = 0.0
        for org, land_amt in land_by_day_payer[d].items():
            if org not in day_payer[d] or day_payer[d][org] <= 0:
                land_no_dep += land_amt
        print(f"  Open land from payers with $0 tracker deposit that day: {money(land_no_dep)}")

    # --- 5) SF overrides ---
    print("\n### 5) SF overrides / paid without eob / denied")
    print(f"  sf_override paid without eob: n={sf_paid_no_eob_n} amt={money(sf_paid_no_eob_amt)}")
    print(f"  All paid without eob by source:")
    for k, v in sorted(source_counts.items(), key=lambda kv: -kv[1].get("amt", 0)):
        print(f"    {k}: n={int(v['n'])} amt={money(v['amt'])}")
    print(f"  Denied rows (any): n={denied_n} denied_amount_sum~={money(denied_amt_total)}")
    print(f"  Denied with orig_forecast on focus days: {money(denied_on_focus_land)}")

    # More precise: scan paid no eob with orig/forecast near focus? and open AR that SF says paid
    # Re-scan lightly for sf_override stages on focus
    sf_on_focus = defaultdict(float)
    paid_no_eob_focus_land_day = defaultdict(float)
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            source = (row.get("source") or "").strip()
            eob = _parse_day(row.get("eob_date"))
            orig = _parse_day(row.get("original_forecast_date")) or _parse_day(
                row.get("forecast_date")
            )
            paid = parse_money(row.get("paid_amount") or "0")
            exp = parse_money(row.get("expected_amount") or "0")
            if source == "sf_override":
                if orig in FOCUS:
                    sf_on_focus[(orig, stage)] += paid if stage == "paid" else exp
            if stage == "paid" and eob is None and paid > 0 and orig in FOCUS:
                paid_no_eob_focus_land_day[orig] += paid

    print("  SF override amounts on focus original_forecast_date by stage:")
    for (d, st), amt in sorted(sf_on_focus.items()):
        print(f"    {d} {st}: {money(amt)}")
    print("  Paid-no-eob with original_forecast_date on focus (would NOT be in Expected land):")
    for d in sorted(FOCUS):
        print(f"    {d}: {money(paid_no_eob_focus_land_day[d])}")

    # --- 6) Snowflake/reconcile gaps ---
    print("\n### 6) DOS coverage / reconcile gaps")
    print(f"  outcome_stages DOS max={dos_max} min={dos_min}")
    print(f"  as_of={AS_OF} → days after DOS max until as_of: {(AS_OF - dos_max).days if dos_max else 'n/a'}")

    # Visits pending that might have deposited: open AR overdue/on_track for depositing payers
    # already covered in 4. Check reconcile_status distribution for focus land
    recon_focus = defaultdict(lambda: defaultdict(float))
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            if stage not in {"on_track", "overdue"}:
                continue
            orig = _parse_day(row.get("original_forecast_date")) or _parse_day(
                row.get("forecast_date")
            )
            if orig not in FOCUS:
                continue
            recon = (row.get("reconcile_status") or "").strip() or "(blank)"
            exp = parse_money(row.get("expected_amount") or "0")
            recon_focus[orig][recon] += exp
    for d in sorted(FOCUS):
        print(f"  {d} open-land by reconcile_status:")
        for st, amt in sorted(recon_focus[d].items(), key=lambda kv: -kv[1]):
            print(f"    {st}: {money(amt)}")

    # --- 7 already partly done with unmapped ---
    print("\n### 7) Mapping errors summary")
    for d in sorted(FOCUS):
        tr = tracker_day_total[d]
        um = day_unmapped[d]
        print(
            f"  {d}: unmapped={money(um)} / tracker={money(tr)} = "
            f"{(um/tr if tr else 0):.1%}"
        )

    # --- Ranked gap contribution ---
    print("\n" + "=" * 72)
    print("### RANKED DATA FINDINGS (gap = bank - expected_land)")
    print("=" * 72)
    for d in sorted(FOCUS):
        bank = BANK[d]
        land = land_total[d]
        tr = tracker_day_total[d]
        gap = bank - land
        print(f"\n## {d}: bank={money(bank)} land={money(land)} gap={money(gap)} tracker={money(tr)}")

        findings = []

        # A: tracker missing entirely from forecast actuals
        findings.append(
            (
                abs(bank - tr) if tr > 0 else bank,
                "A_tracker_vs_bank_diff",
                f"Tracker total {money(tr)} vs bank {money(bank)} (diff {money(tr-bank)})",
            )
        )

        # B: deposits from payers with zero open land that day
        zero_land = sum(
            amt
            for org, amt in day_payer[d].items()
            if org != "(unmapped)" and land_by_day_payer[d].get(org, 0) <= 1e-9
        )
        findings.append(
            (
                zero_land,
                "B_deposit_payers_zero_open_land",
                f"Mapped depositing payers with $0 Expected land that day: {money(zero_land)}",
            )
        )

        # C: unmapped ACH
        findings.append(
            (
                day_unmapped[d],
                "C_unmapped_ach",
                f"Unmapped Tracker ACH heads: {money(day_unmapped[d])}",
            )
        )

        # D: deposit - land shortfall for payers that DID have some land
        short = 0.0
        for org, dep_amt in day_payer[d].items():
            if org == "(unmapped)":
                continue
            land_amt = land_by_day_payer[d].get(org, 0.0)
            if land_amt > 1e-9 and dep_amt > land_amt:
                short += dep_amt - land_amt
        findings.append(
            (
                short,
                "D_under_scheduled_known_payers",
                f"Deposit exceeds open land for payers with some land: {money(short)}",
            )
        )

        # E: open land scheduled but payer did not deposit (wrong day / over-schedule)
        land_no_dep = sum(
            amt
            for org, amt in land_by_day_payer[d].items()
            if day_payer[d].get(org, 0) <= 1e-9
        )
        findings.append(
            (
                land_no_dep,
                "E_land_payers_no_deposit",
                f"Open land from payers with no deposit that day (offsets gap): {money(land_no_dep)}",
            )
        )

        # F: paid already (eob near day) — money already classified paid, not in Expected land
        findings.append(
            (
                paid_near_eob_total[d],
                "F_already_paid_eob_near_day",
                f"Outcomes already paid with eob±1 of day (not in Expected land): {money(paid_near_eob_total[d])}",
            )
        )

        # G: paid no eob
        findings.append(
            (
                paid_no_eob_focus_land_day[d],
                "G_paid_no_eob_on_orig_fd",
                f"Paid-without-eob on orig_fd={d}: {money(paid_no_eob_focus_land_day[d])}",
            )
        )

        findings.sort(key=lambda x: -x[0])
        for amt, code, msg in findings:
            print(f"  [{code}] ~{money(amt)} — {msg}")

        # Net narrative
        if tr > 0:
            # How much of tracker is "explained" by open land of depositing payers
            explained = sum(
                min(dep, land_by_day_payer[d].get(org, 0.0))
                for org, dep in day_payer[d].items()
                if org != "(unmapped)"
            )
            print(
                f"\n  Attribution check: min(dep,land) for mapped depositing payers = {money(explained)}"
            )
            print(
                f"  Unexplained tracker (tracker - that - unmapped) ≈ "
                f"{money(tr - explained - day_unmapped[d])}"
            )
            print(
                f"  Gap decomposition approx: "
                f"zero_land_deps({money(zero_land)}) + under_sched({money(short)}) + "
                f"unmapped({money(day_unmapped[d])}) - land_no_dep_offset({money(land_no_dep)}) "
                f"≈ {money(zero_land + short + day_unmapped[d] - land_no_dep)} "
                f"vs gap {money(gap)}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
