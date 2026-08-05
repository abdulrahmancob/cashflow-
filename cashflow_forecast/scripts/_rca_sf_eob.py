"""SF paid overlap + eob freshness for Jul 28/29 RCA."""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money
from cashflow_reconcile.payer_registry import resolve

FORECAST = _REPO / "webpt_edco_scraper/output/jun_jul_2026/forecast"
SF = (
    _REPO
    / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/sf_compare/status_mismatch_sf_paid_denied.csv"
)
FOCUS = {"2026-07-28", "2026-07-29"}


def main() -> int:
    max_eob = None
    paid_by_eob_day: dict[str, float] = defaultdict(float)
    n_paid = 0
    with (FORECAST / "outcome_stages.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            eob = (row.get("eob_date") or "").strip()[:10]
            if eob and eob[:1].isdigit():
                if max_eob is None or eob > max_eob:
                    max_eob = eob
            stage = (row.get("outcome_stage") or "").strip().lower()
            if stage == "paid":
                n_paid += 1
                paid = parse_money(row.get("paid_amount") or 0)
                if eob:
                    paid_by_eob_day[eob] += paid
    print("max eob_date in outcome_stages:", max_eob)
    print("paid rows:", n_paid)
    print("paid by eob last days:")
    for d in sorted(paid_by_eob_day)[-15:]:
        print(f"  {d}: ${paid_by_eob_day[d]:,.2f}")

    sf_paid_keys: dict[tuple[str, str], float] = {}
    with SF.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            st = str(row.get("sf_status") or "").strip().lower()
            if st != "paid":
                continue
            key = (
                (row.get("name_key") or "").strip().upper(),
                (row.get("date_of_service") or "")[:10],
            )
            try:
                amt = float(str(row.get("sf_total_paid") or 0).replace(",", ""))
            except ValueError:
                amt = 0.0
            sf_paid_keys[key] = amt
    print(
        f"\nSF-says-paid mismatch keys: {len(sf_paid_keys)} "
        f"total_sf_paid=${sum(sf_paid_keys.values()):,.2f}"
    )

    overlap: dict[str, float] = defaultdict(float)
    overlap_n = 0
    open_focus: dict[str, float] = defaultdict(float)
    with (FORECAST / "outcome_stages.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            if stage not in {"on_track", "overdue"}:
                continue
            orig = (row.get("original_forecast_date") or row.get("forecast_date") or "")[:10]
            if orig not in FOCUS:
                continue
            exp = parse_money(row.get("expected_amount") or 0)
            if exp <= 0:
                continue
            open_focus[orig] += exp
            key = (
                (row.get("name_key") or "").strip().upper(),
                (row.get("date_of_service") or "")[:10],
            )
            if key in sf_paid_keys:
                overlap[orig] += exp
                overlap_n += 1
    print("Open Expected land on focus vs SF-says-paid:")
    for d in sorted(FOCUS):
        print(
            f"  {d}: land=${open_focus[d]:,.2f} "
            f"SF-paid-still-open=${overlap[d]:,.2f}"
        )
    print(f"overlap line count: {overlap_n}")

    den: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    with (FORECAST / "outcome_stages.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            if stage != "denied":
                continue
            orig = (row.get("original_forecast_date") or row.get("forecast_date") or "")[:10]
            if orig not in FOCUS:
                continue
            amt = parse_money(row.get("denied_amount") or 0) or parse_money(
                row.get("expected_amount") or 0
            )
            ins = (row.get("ins_name") or "").strip()
            rev = (row.get("insurance_revflow") or "").strip()
            hit = (
                resolve(rev, "revflow")
                or resolve(ins, "webpt")
                or resolve(ins, "any")
                or resolve(rev, "any")
            )
            org = hit.name if hit else (ins or rev or "(blank)")
            den[orig][org] += amt
    print("\nDenied with orig_fd on focus:")
    for d in sorted(FOCUS):
        tot = sum(den[d].values())
        print(f"  {d}: total=${tot:,.2f}")
        for org, a in sorted(den[d].items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {org}: ${a:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
