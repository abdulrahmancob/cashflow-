"""Gap report: Expected land vs historical Tracker deposit share by payer_org.

Writes late-July daily payer gap CSVs under the forecast directory.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money  # noqa: E402
from cashflow_reconcile.payer_registry import resolve  # noqa: E402

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _payer_org_from_outcome(row: dict[str, str]) -> str:
    ins = (row.get("ins_name") or "").strip()
    rev = (row.get("insurance_revflow") or "").strip()
    hit = (
        resolve(rev, "revflow")
        or resolve(ins, "webpt")
        or resolve(ins, "any")
        or resolve(rev, "any")
    )
    if hit is not None:
        return hit.name
    return ins or rev or "(blank)"


def load_expected_land_by_day_payer(
    outcomes_path: Path,
    *,
    start: date,
    end: date,
) -> dict[date, dict[str, float]]:
    """on_track+overdue expected_amount by original_forecast_date, keyed by payer_org."""
    out: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stage = (row.get("outcome_stage") or "").strip().lower()
            if stage not in {"on_track", "overdue"}:
                continue
            amt = parse_money(row.get("expected_amount") or "0")
            if amt <= 0:
                continue
            d = _parse_day(row.get("original_forecast_date") or "") or _parse_day(
                row.get("forecast_date") or ""
            )
            if d is None or d < start or d > end:
                continue
            out[d][_payer_org_from_outcome(row)] += amt
    return out


def load_tracker_deposits_by_weekday_payer(
    tracker_path: Path,
) -> tuple[dict[int, dict[str, float]], dict[int, float]]:
    """Historical deposit totals by weekday idx and payer_org from Tracker."""
    from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger

    by_wd_payer: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_wd_total: dict[int, float] = defaultdict(float)
    for row in load_deposit_ledger(tracker_path):
        dep = _parse_day(row.get("deposit_date"))
        if dep is None:
            continue
        amt = float(row.get("amount") or 0)
        if amt <= 0:
            continue
        org = str(row.get("payer_org") or "").strip()
        if not org:
            head = str(row.get("ach_payer_head") or "").strip()
            hit = resolve(head, "tracker") or resolve(head, "any") if head else None
            org = hit.name if hit else (head or "(unmapped)")
        wd = dep.weekday()
        by_wd_payer[wd][org] += amt
        by_wd_total[wd] += amt
    return by_wd_payer, by_wd_total


def _share(part: float, total: float) -> float:
    return (part / total) if total > 1e-9 else 0.0


def write_gap_report(
    *,
    forecast_dir: Path,
    tracker_path: Path,
    start: date,
    end: date,
) -> Path:
    outcomes_path = forecast_dir / "outcome_stages.csv"
    land = load_expected_land_by_day_payer(outcomes_path, start=start, end=end)
    hist_wd_payer, hist_wd_total = load_tracker_deposits_by_weekday_payer(tracker_path)

    daily_rows: list[dict[str, object]] = []
    flag_rows: list[dict[str, object]] = []

    d = start
    while d <= end:
        day_land = land.get(d, {})
        land_total = sum(day_land.values())
        wd = d.weekday()
        hist_payers = hist_wd_payer.get(wd, {})
        hist_total = hist_wd_total.get(wd, 0.0)

        top_hist = sorted(hist_payers.items(), key=lambda kv: kv[1], reverse=True)[:20]
        names = sorted(
            set(day_land) | {k for k, _ in top_hist},
            key=lambda n: (-float(day_land.get(n, 0.0)), n),
        )

        for name in names:
            land_amt = float(day_land.get(name, 0.0))
            hist_amt = float(hist_payers.get(name, 0.0))
            land_share = _share(land_amt, land_total)
            hist_share = _share(hist_amt, hist_total)
            gap_share = hist_share - land_share
            daily_rows.append(
                {
                    "date": d.isoformat(),
                    "weekday": WEEKDAY_NAMES[wd],
                    "payer_org": name,
                    "expected_land": round(land_amt, 2),
                    "expected_land_share": round(land_share, 4),
                    "hist_weekday_deposit": round(hist_amt, 2),
                    "hist_weekday_share": round(hist_share, 4),
                    "share_gap": round(gap_share, 4),
                    "day_expected_land_total": round(land_total, 2),
                    "hist_weekday_deposit_total": round(hist_total, 2),
                }
            )
            if hist_share >= 0.03 and land_amt <= 1e-9 and wd < 5:
                flag_rows.append(
                    {
                        "date": d.isoformat(),
                        "weekday": WEEKDAY_NAMES[wd],
                        "payer_org": name,
                        "expected_land": 0.0,
                        "hist_weekday_share": round(hist_share, 4),
                        "hist_weekday_deposit": round(hist_amt, 2),
                        "note": "hist_deposits_this_weekday_but_zero_expected_land",
                    }
                )

        daily_rows.append(
            {
                "date": d.isoformat(),
                "weekday": WEEKDAY_NAMES[wd],
                "payer_org": "__DAY_TOTAL__",
                "expected_land": round(land_total, 2),
                "expected_land_share": 1.0 if land_total else 0.0,
                "hist_weekday_deposit": round(hist_total, 2),
                "hist_weekday_share": 1.0 if hist_total else 0.0,
                "share_gap": 0.0,
                "day_expected_land_total": round(land_total, 2),
                "hist_weekday_deposit_total": round(hist_total, 2),
            }
        )
        d += timedelta(days=1)

    out_dir = forecast_dir / "gap_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    daily_path = out_dir / f"land_gap_by_payer_{start.isoformat()}_to_{end.isoformat()}.csv"
    flags_path = out_dir / f"zero_land_hist_payers_{start.isoformat()}_to_{end.isoformat()}.csv"
    summary_path = out_dir / "land_gap_summary_tue_wed.txt"

    fields = [
        "date",
        "weekday",
        "payer_org",
        "expected_land",
        "expected_land_share",
        "hist_weekday_deposit",
        "hist_weekday_share",
        "share_gap",
        "day_expected_land_total",
        "hist_weekday_deposit_total",
    ]
    with daily_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(daily_rows)

    flag_fields = [
        "date",
        "weekday",
        "payer_org",
        "expected_land",
        "hist_weekday_share",
        "hist_weekday_deposit",
        "note",
    ]
    with flags_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=flag_fields)
        w.writeheader()
        w.writerows(flag_rows)

    focus = [row for row in daily_rows if row["date"] in {"2026-07-28", "2026-07-29"}]
    with summary_path.open("w", encoding="utf-8") as fh:
        fh.write("Expected land vs historical weekday deposit share (payer_org)\n")
        fh.write(f"Window: {start} .. {end}\n")
        fh.write(f"Daily detail: {daily_path.name}\n")
        fh.write(f"Zero-land flags: {flags_path.name} ({len(flag_rows)} rows)\n\n")
        for day in ("2026-07-28", "2026-07-29"):
            rows = [r for r in focus if r["date"] == day and r["payer_org"] != "__DAY_TOTAL__"]
            total = next(
                (r for r in focus if r["date"] == day and r["payer_org"] == "__DAY_TOTAL__"),
                None,
            )
            fh.write(f"=== {day} ===\n")
            if total:
                fh.write(
                    f"Expected land total: ${float(total['expected_land']):,.2f} | "
                    f"Hist {total['weekday']} deposit pool: "
                    f"${float(total['hist_weekday_deposit_total']):,.2f}\n"
                )
            rows_sorted = sorted(rows, key=lambda r: float(r["share_gap"]), reverse=True)
            fh.write("Top under-scheduled vs hist weekday share:\n")
            for r in rows_sorted[:12]:
                fh.write(
                    f"  {r['payer_org']}: land=${float(r['expected_land']):,.2f} "
                    f"({float(r['expected_land_share']):.1%}) | "
                    f"hist_share={float(r['hist_weekday_share']):.1%} | "
                    f"gap={float(r['share_gap']):+.1%}\n"
                )
            fh.write("Top expected land payers:\n")
            for r in sorted(rows, key=lambda x: float(x["expected_land"]), reverse=True)[:8]:
                if float(r["expected_land"]) <= 0:
                    continue
                fh.write(
                    f"  {r['payer_org']}: ${float(r['expected_land']):,.2f} "
                    f"({float(r['expected_land_share']):.1%})\n"
                )
            fh.write("\n")

        tue_wed_flags = [r for r in flag_rows if r["weekday"] in {"Tue", "Wed"}]
        by_payer: dict[str, float] = defaultdict(float)
        for r in tue_wed_flags:
            by_payer[str(r["payer_org"])] += float(r["hist_weekday_share"])
        fh.write("Top Tue/Wed zero-land payers by summed hist share flags:\n")
        for name, score in sorted(by_payer.items(), key=lambda kv: kv[1], reverse=True)[:15]:
            fh.write(f"  {name}: flag_share_sum={score:.3f}\n")

        # Explicit Healthfirst note
        fri = [
            r
            for r in daily_rows
            if r["date"] == "2026-07-31" and "Healthfirst" in str(r["payer_org"])
        ]
        fh.write("\nHealthfirst on Fri 2026-07-31 (Tracker deposit cadence weekly_fri):\n")
        for r in fri:
            fh.write(f"  {r['payer_org']}: land=${float(r['expected_land']):,.2f}\n")

    print(f"Wrote {daily_path}")
    print(f"Wrote {flags_path}")
    print(f"Wrote {summary_path}")
    return summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forecast-dir",
        default="webpt_edco_scraper/output/jun_jul_2026/forecast",
    )
    parser.add_argument(
        "--tracker",
        default="webpt_edco_scraper/Transaction Tracker 2026.xlsx",
    )
    parser.add_argument("--from", dest="date_from", default="2026-07-20")
    parser.add_argument("--to", dest="date_to", default="2026-07-31")
    args = parser.parse_args()

    forecast_dir = Path(args.forecast_dir)
    if not forecast_dir.is_absolute():
        forecast_dir = _REPO / forecast_dir
    tracker = Path(args.tracker)
    if not tracker.is_absolute():
        tracker = _REPO / tracker

    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_to)
    write_gap_report(
        forecast_dir=forecast_dir,
        tracker_path=tracker,
        start=start,
        end=end,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
