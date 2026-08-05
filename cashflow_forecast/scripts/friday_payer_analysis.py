"""Friday-deposit payer operational analysis (read-only; no model changes).

Identifies Friday-pattern payers, compares historical Friday deposits vs Expected
land / Open AR schedule, classifies root cause A-F, and estimates hypothetical
eligible pull-forward dollars with claim-level rules.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money  # noqa: E402
from cashflow_reconcile.payer_registry import (  # noqa: E402
    is_ach_processor,
    resolve,
)

NEXT_FRIDAY = date(2026, 7, 31)
FRIDAYS = [NEXT_FRIDAY + timedelta(days=7 * i) for i in range(5)]  # +0..+4
HIST_FRIDAY_WEEKS = 12
FRI_SHARE_THRESHOLD = 0.50


def _parse_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(
                text[:10] if fmt.startswith("%Y") else text, fmt
            ).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def _friday_stats(amounts: list[float]) -> dict[str, float]:
    if not amounts:
        return {
            "n_fridays": 0,
            "mean": float("nan"),
            "median": float("nan"),
            "p25": float("nan"),
            "p75": float("nan"),
            "min": float("nan"),
            "max": float("nan"),
        }
    s = sorted(amounts)
    return {
        "n_fridays": len(s),
        "mean": statistics.fmean(s),
        "median": statistics.median(s),
        "p25": _percentile(s, 0.25),
        "p75": _percentile(s, 0.75),
        "min": s[0],
        "max": s[-1],
    }


def _org_from_check(row: dict[str, str]) -> str:
    org = (row.get("payer_org") or "").strip()
    if org and not is_ach_processor(org):
        return org
    hit = resolve(row.get("payor") or "", "revflow") or resolve(
        row.get("payor") or "", "any"
    )
    if hit and not is_ach_processor(hit):
        return hit.name
    return (row.get("payor") or "(blank)").strip()


def _org_from_outcome(row: dict[str, str]) -> str:
    hit = (
        resolve(row.get("insurance_revflow") or "", "revflow")
        or resolve(row.get("ins_name") or "", "webpt")
        or resolve(row.get("ins_name") or "", "any")
    )
    if hit and not is_ach_processor(hit):
        return hit.name
    return (row.get("ins_name") or "(blank)").strip()


def load_behavior_friday_orgs(path: Path) -> dict[str, dict[str, object]]:
    """payer_org -> meta from weekly_fri / fri-primary cadence labels."""
    out: dict[str, dict[str, object]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            cadence = (row.get("cadence") or "").strip().lower()
            top_dep = (row.get("top_deposit_weekday") or "").strip()
            top_pct = parse_money(row.get("top_deposit_weekday_pct") or "0")
            org = (row.get("payer_org") or "").strip() or _org_from_check(row)
            if is_ach_processor(org) or org in {"Self Pay", "(blank)", ""}:
                continue
            is_weekly_fri = "weekly_fri" in cadence or cadence.replace(" ", "") == "fri"
            # Strict Friday-primary multi only (used later with deposit fri_share gate)
            fri_primary_multi = (
                "fri" in cadence
                and "weekly_fri" not in cadence
                and top_dep == "Fri"
                and top_pct >= 50.0
            )
            if not is_weekly_fri and not fri_primary_multi:
                continue
            cur = out.get(org)
            paid = parse_money(row.get("paid_amount_sum") or "0")
            vel = parse_money(row.get("cash_velocity_median") or "0")
            eob_dep = parse_money(row.get("eob_to_deposit_median") or "0")
            if cur is None:
                out[org] = {
                    "cadence_labels": {cadence},
                    "behavior_paid": paid,
                    "cash_velocity_median": vel,
                    "eob_to_deposit_median": eob_dep,
                    "from_weekly_fri": is_weekly_fri,
                    "fri_primary_multi": fri_primary_multi,
                }
            else:
                cur["cadence_labels"].add(cadence)
                cur["behavior_paid"] = float(cur["behavior_paid"]) + paid
                if is_weekly_fri:
                    cur["from_weekly_fri"] = True
                if fri_primary_multi:
                    cur["fri_primary_multi"] = True
                if vel > 0 and (
                    float(cur["cash_velocity_median"]) <= 0
                    or vel < float(cur["cash_velocity_median"])
                ):
                    cur["cash_velocity_median"] = vel
    return out


@dataclass
class PayerFridayProfile:
    payer_org: str
    total_deposit_usd: float = 0.0
    friday_deposit_usd: float = 0.0
    fri_share: float = 0.0
    from_cadence: bool = False
    from_share: bool = False
    cadence_labels: set[str] = field(default_factory=set)
    cash_velocity_median: float = 0.0
    eob_to_deposit_median: float = 0.0
    friday_day_amounts: list[float] = field(default_factory=list)
    hist_stats: dict[str, float] = field(default_factory=dict)
    expected_by_friday: dict[str, float] = field(default_factory=dict)
    open_total: float = 0.0
    open_by_ofd_friday: dict[str, float] = field(default_factory=dict)
    open_by_fd_friday: dict[str, float] = field(default_factory=dict)
    open_other_ofd: float = 0.0
    pack_deferred_usd: float = 0.0  # ofd on/before next fri but fd later
    pack_deferred_n: int = 0
    later_friday_n: int = 0
    later_friday_usd: float = 0.0
    later_friday_past_dos_usd: float = 0.0  # ofd later Fri AND DOS < next Fri
    later_friday_future_dos_usd: float = 0.0  # forward volume on later Fridays
    timing_summary: dict[str, object] = field(default_factory=dict)
    cause: str = "F"
    cause_detail: str = ""
    eligible_n: int = 0
    eligible_usd: float = 0.0
    eligible_rules: str = ""
    recommendation: str = "Needs Investigation"


def load_checks_deposits(
    path: Path,
) -> tuple[dict[str, dict[str, float]], dict[str, float], dict[str, float]]:
    """org -> {deposit_date -> amt}, org->total, org->friday_total."""
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    totals: dict[str, float] = defaultdict(float)
    fri_totals: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            org = _org_from_check(row)
            if is_ach_processor(org) or org in {"Self Pay", "(blank)"}:
                continue
            dep = _parse_day(row.get("deposit_date"))
            if dep is None:
                continue
            amt = parse_money(row.get("paid_amount_sum") or "0")
            by_day[org][dep.isoformat()] += amt
            totals[org] += amt
            if dep.weekday() == 4:
                fri_totals[org] += amt
    return by_day, totals, fri_totals


def identify_friday_payers(
    *,
    by_day: dict[str, dict[str, float]],
    totals: dict[str, float],
    fri_totals: dict[str, float],
    behavior: dict[str, dict[str, object]],
) -> dict[str, PayerFridayProfile]:
    profiles: dict[str, PayerFridayProfile] = {}
    orgs = set(totals) | set(behavior)
    for org in orgs:
        total = float(totals.get(org, 0.0))
        fri = float(fri_totals.get(org, 0.0))
        share = (fri / total) if total > 0 else 0.0
        beh = behavior.get(org)
        from_weekly = bool(beh and beh.get("from_weekly_fri"))
        from_multi = bool(beh and beh.get("fri_primary_multi") and share >= 0.40)
        from_share = share >= FRI_SHARE_THRESHOLD and fri > 0
        if not from_weekly and not from_multi and not from_share:
            continue
        # Build last N Friday deposit day totals
        fri_days = sorted(
            (
                (d, a)
                for d, a in by_day.get(org, {}).items()
                if _parse_day(d) and _parse_day(d).weekday() == 4  # type: ignore[union-attr]
            ),
            key=lambda x: x[0],
        )
        recent = fri_days[-HIST_FRIDAY_WEEKS:]
        amounts = [a for _, a in recent]
        p = PayerFridayProfile(
            payer_org=org,
            total_deposit_usd=total,
            friday_deposit_usd=fri,
            fri_share=share,
            from_cadence=from_weekly or from_multi,
            from_share=from_share,
            cadence_labels=set(beh["cadence_labels"]) if beh else set(),
            cash_velocity_median=float(beh["cash_velocity_median"]) if beh else 0.0,
            eob_to_deposit_median=float(beh["eob_to_deposit_median"]) if beh else 0.0,
            friday_day_amounts=amounts,
            hist_stats=_friday_stats(amounts),
        )
        profiles[org] = p
    return profiles


def analyze_outcomes(
    path: Path, profiles: dict[str, PayerFridayProfile]
) -> None:
    fri_set = {d.isoformat() for d in FRIDAYS}
    next_s = NEXT_FRIDAY.isoformat()
    # Accumulators for later-friday claim timing
    later_dos_ages: dict[str, list[int]] = defaultdict(list)
    later_eob_present: dict[str, list[int]] = defaultdict(list)
    later_stages: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            org = _org_from_outcome(row)
            if org not in profiles:
                continue
            st = (row.get("outcome_stage") or "").strip().lower()
            if st not in {"on_track", "overdue"}:
                continue
            exp = parse_money(row.get("expected_amount") or "0")
            if exp <= 0:
                continue
            ofd = _parse_day(row.get("original_forecast_date") or row.get("forecast_date"))
            fd = _parse_day(row.get("forecast_date"))
            dos = _parse_day(row.get("date_of_service"))
            eob = _parse_day(row.get("eob_date"))
            p = profiles[org]
            p.open_total += exp
            ofd_s = ofd.isoformat() if ofd else ""
            fd_s = fd.isoformat() if fd else ""

            if ofd_s in fri_set:
                p.open_by_ofd_friday[ofd_s] = p.open_by_ofd_friday.get(ofd_s, 0) + exp
                p.expected_by_friday[ofd_s] = p.expected_by_friday.get(ofd_s, 0) + exp
            else:
                p.open_other_ofd += exp

            if fd_s in fri_set:
                p.open_by_fd_friday[fd_s] = p.open_by_fd_friday.get(fd_s, 0) + exp

            # Packing signal: ofd on/before next Friday but packed forecast later
            if ofd and fd and ofd <= NEXT_FRIDAY and fd > NEXT_FRIDAY:
                p.pack_deferred_usd += exp
                p.pack_deferred_n += 1

            # Later Fridays (not next)
            if ofd and ofd in FRIDAYS[1:]:
                p.later_friday_n += 1
                p.later_friday_usd += exp
                if dos is None or dos < NEXT_FRIDAY:
                    p.later_friday_past_dos_usd += exp
                else:
                    p.later_friday_future_dos_usd += exp
                if dos:
                    later_dos_ages[org].append((NEXT_FRIDAY - dos).days)
                later_eob_present[org].append(1 if eob else 0)
                later_stages[org][st] += 1

    for org, p in profiles.items():
        ages = later_dos_ages.get(org, [])
        eobs = later_eob_present.get(org, [])
        p.timing_summary = {
            "later_friday_claims": p.later_friday_n,
            "later_friday_usd": round(p.later_friday_usd, 2),
            "dos_age_median_vs_next_fri": (
                round(statistics.median(ages), 1) if ages else None
            ),
            "pct_with_eob": (
                round(100.0 * sum(eobs) / len(eobs), 1) if eobs else None
            ),
            "stages": dict(later_stages.get(org, {})),
        }


def classify_and_eligible(
    profiles: dict[str, PayerFridayProfile],
    outcomes_path: Path,
) -> None:
    """Assign cause A-F and compute hypothetical eligible pull-forward $."""
    # Preload open lines for C/D eligibility pass
    open_lines: dict[str, list[dict[str, object]]] = defaultdict(list)
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            org = _org_from_outcome(row)
            if org not in profiles:
                continue
            st = (row.get("outcome_stage") or "").strip().lower()
            if st not in {"on_track", "overdue"}:
                continue
            exp = parse_money(row.get("expected_amount") or "0")
            if exp <= 0:
                continue
            ofd = _parse_day(row.get("original_forecast_date") or row.get("forecast_date"))
            fd = _parse_day(row.get("forecast_date"))
            dos = _parse_day(row.get("date_of_service"))
            eob = _parse_day(row.get("eob_date"))
            open_lines[org].append(
                {
                    "exp": exp,
                    "ofd": ofd,
                    "fd": fd,
                    "dos": dos,
                    "eob": eob,
                    "stage": st,
                }
            )

    for org, p in profiles.items():
        next_land = p.expected_by_friday.get(NEXT_FRIDAY.isoformat(), 0.0)
        hist_med = p.hist_stats.get("median") or float("nan")
        hist_p25 = p.hist_stats.get("p25") or float("nan")
        later = p.later_friday_usd
        later_past = p.later_friday_past_dos_usd
        later_fwd = p.later_friday_future_dos_usd
        open_t = p.open_total
        material_hist = (
            p.hist_stats.get("n_fridays", 0) >= 4
            and (
                (not math.isnan(hist_med) and hist_med >= 5000)
                or p.friday_deposit_usd >= 50_000
            )
        )

        # Ratio next Friday land vs historical (comparison only)
        ratio = (
            next_land / hist_med
            if hist_med and not math.isnan(hist_med) and hist_med > 0
            else None
        )

        # Classification priority
        if (not material_hist) and open_t < 1000:
            p.cause = "A"
            p.cause_detail = "negligible_friday_payer_or_no_open_ar"
            p.recommendation = "Do Nothing"
        elif material_hist and ratio is not None and ratio >= 0.70:
            p.cause = "A"
            p.cause_detail = (
                f"next_fri_land=${next_land:,.0f} >= 70% of hist_median=${hist_med:,.0f}"
            )
            p.recommendation = "Do Nothing"
        elif material_hist and open_t < max(5000.0, hist_p25 * 0.25 if not math.isnan(hist_p25) else 0):
            p.cause = "B"
            p.cause_detail = f"open_ar=${open_t:,.0f} too small vs friday history"
            p.recommendation = "Needs Investigation"
        elif (
            material_hist
            and later_past >= max(next_land * 2, 25000)
            and later_past >= p.pack_deferred_usd
        ):
            p.cause = "C"
            p.cause_detail = (
                f"past-DOS open on Fri+1..+4=${later_past:,.0f} "
                f"(forward-DOS later Fri=${later_fwd:,.0f}) "
                f"vs next_fri_land=${next_land:,.0f}"
            )
            p.recommendation = "Candidate for Pull-forward"
        elif material_hist and p.pack_deferred_usd >= max(next_land * 2, 25000):
            p.cause = "D"
            p.cause_detail = (
                f"packing_deferred=${p.pack_deferred_usd:,.0f} "
                f"(n={p.pack_deferred_n}) ofd<=next_fri but forecast_date later; "
                f"later_fri_past_dos=${later_past:,.0f}"
            )
            p.recommendation = "Candidate for Pull-forward"
        elif (
            material_hist
            and ratio is not None
            and ratio < 0.70
            and later_past < 10000
            and p.pack_deferred_usd < 10000
        ):
            p.cause = "E"
            p.cause_detail = (
                "land_below_history_but_little_past-DOS_stock_on_later_fridays "
                f"(forward_later_fri=${later_fwd:,.0f}; workflow/batch/paid)"
            )
            p.recommendation = "Candidate for Business Rule"
        elif not material_hist:
            p.cause = "A"
            p.cause_detail = "small_friday_history_not_material"
            p.recommendation = "Do Nothing"
        else:
            p.cause = "F"
            p.cause_detail = (
                f"mixed: next=${next_land:,.0f} later_past=${later_past:,.0f} "
                f"later_fwd=${later_fwd:,.0f} pack=${p.pack_deferred_usd:,.0f}"
            )
            p.recommendation = "Needs Investigation"

        # Eligible hypothetical for C or D
        if p.cause not in {"C", "D"}:
            continue

        vel = p.cash_velocity_median if p.cash_velocity_median > 0 else 14.0
        min_age = max(5.0, vel - max(p.eob_to_deposit_median, 0) - 3)
        elig_n = 0
        elig_usd = 0.0
        rules_used = (
            f"DOS < next_fri; DOS age >= {min_age:.0f}d "
            f"(velocity_median={vel:.0f}); stage on_track|overdue; "
            f"ofd on Fri+1..+4 OR packed (ofd<=next_fri < forecast_date); "
            f"excludes forward-DOS"
        )
        for line in open_lines[org]:
            ofd: date | None = line["ofd"]  # type: ignore[assignment]
            fd: date | None = line["fd"]  # type: ignore[assignment]
            dos: date | None = line["dos"]  # type: ignore[assignment]
            exp = float(line["exp"])
            if dos is None or ofd is None:
                continue
            if dos >= NEXT_FRIDAY:
                continue  # forward volume not eligible for this Friday
            age = (NEXT_FRIDAY - dos).days
            if age < min_age:
                continue
            on_later_fri = ofd in FRIDAYS[1:]
            pack_case = (
                ofd <= NEXT_FRIDAY
                and fd is not None
                and fd > NEXT_FRIDAY
            )
            if not on_later_fri and not pack_case:
                continue
            elig_n += 1
            elig_usd += exp

        p.eligible_n = elig_n
        p.eligible_usd = round(elig_usd, 2)
        p.eligible_rules = rules_used

        if p.cause in {"C", "D"}:
            denom = max(later_past, p.pack_deferred_usd, 1)
            if elig_usd >= 25000 and elig_usd >= 0.15 * denom:
                p.recommendation = "Candidate for Pull-forward"
            elif elig_usd > 0:
                p.recommendation = "Needs Investigation"
            else:
                p.recommendation = "Candidate for Calibration"


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def write_report(path: Path, profiles: list[PayerFridayProfile]) -> None:
    ranked = sorted(profiles, key=lambda p: -p.friday_deposit_usd)
    cause_c = sum(1 for p in ranked if p.cause == "C")
    cause_d = sum(1 for p in ranked if p.cause == "D")
    cause_a = sum(1 for p in ranked if p.cause == "A")
    pull_cands = [p for p in ranked if p.recommendation == "Candidate for Pull-forward"]

    lines: list[str] = []
    lines.append("# Friday Payer Operational Analysis")
    lines.append("")
    lines.append(
        f"**Next Friday:** {NEXT_FRIDAY.isoformat()}  |  "
        f"**Fridays covered:** {', '.join(d.isoformat() for d in FRIDAYS)}  |  "
        f"**Friday payers:** {len(ranked)}"
    )
    lines.append("")
    lines.append(
        "Analysis only — **no model changes, no pull-forward applied, no Friday floor.** "
        "Historical Friday stats are for comparison, not targets."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(
        f"Identified **{len(ranked)}** Friday-pattern payers "
        f"(weekly_fri cadence and/or >=50% of deposit $ on Friday)."
    )
    lines.append("")
    lines.append(
        f"Root-cause mix: **A**={cause_a}, **C (deferred to later Fridays)**={cause_c}, "
        f"**D (packing)**={cause_d}, other={len(ranked)-cause_a-cause_c-cause_d}."
    )
    lines.append("")
    if pull_cands:
        lines.append("Strongest **Candidate for Pull-forward** (claim-level eligible $):")
        lines.append("")
        for p in sorted(pull_cands, key=lambda x: -x.eligible_usd)[:8]:
            nxt = p.expected_by_friday.get(NEXT_FRIDAY.isoformat(), 0)
            lines.append(
                f"- **{p.payer_org}**: next-Fri land `${nxt:,.0f}` vs hist median "
                f"`${p.hist_stats.get('median', float('nan')):,.0f}` (compare only); "
                f"later-Fri open `${p.later_friday_usd:,.0f}`; "
                f"**eligible** `{p.eligible_n}` lines / `${p.eligible_usd:,.0f}` "
                f"(cause {p.cause})"
            )
        lines.append("")
    else:
        lines.append(
            "No payer met claim-level Eligible $ bar for Pull-forward candidate "
            "in this pass; see per-payer sections."
        )
        lines.append("")
    # Highlight Healthfirst
    hf = next((p for p in ranked if "healthfirst" in p.payer_org.lower()), None)
    if hf:
        nxt = hf.expected_by_friday.get(NEXT_FRIDAY.isoformat(), 0)
        lines.append(
            f"**Healthfirst spotlight:** hist Friday median "
            f"`${hf.hist_stats.get('median', float('nan')):,.0f}` (compare); "
            f"Expected land next Fri `${nxt:,.0f}`; open AR `${hf.open_total:,.0f}`; "
            f"Fri+1..+4 total `${hf.later_friday_usd:,.0f}` "
            f"(past-DOS `${hf.later_friday_past_dos_usd:,.0f}` / "
            f"forward-DOS `${hf.later_friday_future_dos_usd:,.0f}`); "
            f"pack-deferred `${hf.pack_deferred_usd:,.0f}`; "
            f"cause **{hf.cause}**; eligible `${hf.eligible_usd:,.0f}`; "
            f"rec **{hf.recommendation}**."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 2. Friday Payers Ranking")
    lines.append("")
    lines.append(
        "| Rank | Payer | Fri deposit $ | Fri share | Source | Cadence |"
    )
    lines.append("|---:|---|---:|---:|---|---|")
    for i, p in enumerate(ranked, 1):
        src = []
        if p.from_cadence:
            src.append("cadence")
        if p.from_share:
            src.append("share>=50%")
        lines.append(
            f"| {i} | {p.payer_org} | {p.friday_deposit_usd:,.0f} | "
            f"{p.fri_share:.0%} | {','.join(src)} | "
            f"{';'.join(sorted(p.cadence_labels)) or '-'} |"
        )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 3. Historical Friday Behavior")
    lines.append("")
    lines.append(
        f"Per-Friday deposit totals over last up to {HIST_FRIDAY_WEEKS} Fridays "
        f"(checks_timeline). **Comparison only — not a target.**"
    )
    lines.append("")
    lines.append(
        "| Payer | n | Mean | Median | P25 | P75 | Min | Max |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for p in ranked:
        h = p.hist_stats
        if h.get("n_fridays", 0) == 0:
            continue
        lines.append(
            f"| {p.payer_org} | {int(h['n_fridays'])} | {h['mean']:,.0f} | "
            f"{h['median']:,.0f} | {h['p25']:,.0f} | {h['p75']:,.0f} | "
            f"{h['min']:,.0f} | {h['max']:,.0f} |"
        )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 4. Current Forecast Distribution")
    lines.append("")
    lines.append(
        "Expected land = open `on_track`+`overdue` `$` with "
        "`original_forecast_date` on each Friday."
    )
    lines.append("")
    hdr = "| Payer | " + " | ".join(d.isoformat() for d in FRIDAYS) + " |"
    lines.append(hdr)
    lines.append("|---|" + "|".join(["---:"] * len(FRIDAYS)) + "|")
    for p in ranked:
        cells = [
            f"{p.expected_by_friday.get(d.isoformat(), 0):,.0f}" for d in FRIDAYS
        ]
        lines.append(f"| {p.payer_org} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 5. Open AR Distribution")
    lines.append("")
    lines.append(
        "| Payer | Open AR | % next Fri | Fri+1..+4 (past DOS) | "
        "Fri+1..+4 (forward DOS) | % other ofd | Pack deferred $ |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for p in ranked:
        if p.open_total <= 0:
            continue
        nxt = p.open_by_ofd_friday.get(NEXT_FRIDAY.isoformat(), 0)
        other = p.open_other_ofd
        lines.append(
            f"| {p.payer_org} | {p.open_total:,.0f} | "
            f"{100*nxt/p.open_total:.1f}% | "
            f"{p.later_friday_past_dos_usd:,.0f} | "
            f"{p.later_friday_future_dos_usd:,.0f} | "
            f"{100*other/p.open_total:.1f}% | {p.pack_deferred_usd:,.0f} |"
        )
    lines.append("")
    lines.append(
        "Pack deferred = lines with `original_forecast_date` on/before next Friday "
        "but `forecast_date` after next Friday (capacity/pack signal)."
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 6. Root Cause per Payer")
    lines.append("")
    lines.append("| Payer | Cause | Detail | Later-Fri timing notes |")
    lines.append("|---|---|---|---|")
    for p in ranked:
        ts = p.timing_summary
        note = (
            f"n={ts.get('later_friday_claims')}, "
            f"DOS age med={ts.get('dos_age_median_vs_next_fri')}, "
            f"eob%={ts.get('pct_with_eob')}, stages={ts.get('stages')}"
        )
        lines.append(
            f"| {p.payer_org} | **{p.cause}** | {p.cause_detail} | {note} |"
        )
    lines.append("")
    lines.append(
        "A=OK · B=low open AR · C=deferred to later Fridays · D=packing · "
        "E=workflow · F=other"
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 7. Eligible Pull-forward Analysis")
    lines.append("")
    lines.append(
        "Hypothetical only — **no lines moved**. Eligibility is claim-level "
        "(age vs cash velocity, open stage, ofd on later Friday or packed past next Friday). "
        "Historical median is **not** used as a fill target."
    )
    lines.append("")
    lines.append(
        "| Payer | Cause | Later Fri $ | Eligible n | Eligible $ | "
        "Next Fri land | Hist median (compare) | Rules |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for p in ranked:
        if p.cause not in {"C", "D"} and p.eligible_usd <= 0:
            continue
        lines.append(
            f"| {p.payer_org} | {p.cause} | {p.later_friday_usd:,.0f} | "
            f"{p.eligible_n} | {p.eligible_usd:,.0f} | "
            f"{p.expected_by_friday.get(NEXT_FRIDAY.isoformat(), 0):,.0f} | "
            f"{p.hist_stats.get('median', float('nan')):,.0f} | "
            f"{p.eligible_rules} |"
        )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 8. Recommendations")
    lines.append("")
    lines.append("| Payer | Recommendation | Why |")
    lines.append("|---|---|---|")
    for p in ranked:
        lines.append(
            f"| {p.payer_org} | **{p.recommendation}** | cause {p.cause}; "
            f"eligible ${p.eligible_usd:,.0f}; {p.cause_detail[:120]} |"
        )
    lines.append("")
    lines.append("### Labels")
    lines.append("")
    lines.append("- **Do Nothing** — Expected Friday looks consistent with stock/history.")
    lines.append(
        "- **Needs Investigation** — signal unclear or eligible $ weak vs deferred stock."
    )
    lines.append(
        "- **Candidate for Pull-forward** — deferred Friday-snapped (or packed) open AR "
        "passes claim-level age/stage rules; any future change must use eligibility, "
        "not historical median targeting."
    )
    lines.append(
        "- **Candidate for Business Rule** — ops/workflow pattern more than schedule lag."
    )
    lines.append(
        "- **Candidate for Calibration** — timing parameters may be wrong; still no change in this phase."
    )
    lines.append("")
    lines.append("### Closed")
    lines.append("")
    lines.append(
        "No model edits, floors, or pull-forward were applied in this analysis phase."
    )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    forecast_dir: Path,
    recon_dir: Path,
) -> Path:
    checks_path = recon_dir / "insurance_behavior" / "checks_timeline.csv"
    behavior_path = recon_dir / "insurance_behavior" / "payor_behavior_summary.csv"
    outcomes_path = forecast_dir / "outcome_stages.csv"
    out_dir = forecast_dir / "gap_reports" / "friday_analysis"
    report_path = forecast_dir / "gap_reports" / "friday_payer_analysis.md"

    print("Loading behavior + checks ...")
    behavior = load_behavior_friday_orgs(behavior_path)
    by_day, totals, fri_totals = load_checks_deposits(checks_path)
    profiles = identify_friday_payers(
        by_day=by_day, totals=totals, fri_totals=fri_totals, behavior=behavior
    )
    print(f"  Friday payers: {len(profiles)}")

    print("Analyzing outcomes open AR / land ...")
    analyze_outcomes(outcomes_path, profiles)
    print("Classifying causes + eligible ...")
    classify_and_eligible(profiles, outcomes_path)

    ranked = sorted(profiles.values(), key=lambda p: -p.friday_deposit_usd)

    write_csv(
        out_dir / "friday_payers_ranking.csv",
        [
            {
                "payer_org": p.payer_org,
                "friday_deposit_usd": round(p.friday_deposit_usd, 2),
                "total_deposit_usd": round(p.total_deposit_usd, 2),
                "fri_share": round(p.fri_share, 4),
                "from_cadence": p.from_cadence,
                "from_share": p.from_share,
                "cadence": ";".join(sorted(p.cadence_labels)),
            }
            for p in ranked
        ],
        [
            "payer_org",
            "friday_deposit_usd",
            "total_deposit_usd",
            "fri_share",
            "from_cadence",
            "from_share",
            "cadence",
        ],
    )
    write_csv(
        out_dir / "friday_hist_and_land.csv",
        [
            {
                "payer_org": p.payer_org,
                **{f"hist_{k}": (round(v, 2) if isinstance(v, float) else v) for k, v in p.hist_stats.items()},
                **{
                    f"land_{d.isoformat()}": round(
                        p.expected_by_friday.get(d.isoformat(), 0), 2
                    )
                    for d in FRIDAYS
                },
                "open_total": round(p.open_total, 2),
                "later_friday_usd": round(p.later_friday_usd, 2),
                "pack_deferred_usd": round(p.pack_deferred_usd, 2),
                "cause": p.cause,
                "eligible_n": p.eligible_n,
                "eligible_usd": p.eligible_usd,
                "recommendation": p.recommendation,
            }
            for p in ranked
        ],
        ["payer_org"]
        + [
            "hist_n_fridays",
            "hist_mean",
            "hist_median",
            "hist_p25",
            "hist_p75",
            "hist_min",
            "hist_max",
        ]
        + [f"land_{d.isoformat()}" for d in FRIDAYS]
        + [
            "open_total",
            "later_friday_usd",
            "pack_deferred_usd",
            "cause",
            "eligible_n",
            "eligible_usd",
            "recommendation",
        ],
    )

    write_report(report_path, ranked)
    print(f"Wrote {out_dir}")
    print(f"Wrote {report_path}")
    for p in ranked[:8]:
        print(
            f"  {p.payer_org}: cause={p.cause} next="
            f"{p.expected_by_friday.get(NEXT_FRIDAY.isoformat(), 0):.0f} "
            f"later={p.later_friday_usd:.0f} elig={p.eligible_usd:.0f} "
            f"rec={p.recommendation}"
        )
    return report_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--forecast-dir",
        default="webpt_edco_scraper/output/jun_jul_2026/forecast",
    )
    ap.add_argument(
        "--recon-dir",
        default="webpt_edco_scraper/output/jun_jul_2026/reconciliation",
    )
    args = ap.parse_args()
    forecast = Path(args.forecast_dir)
    recon = Path(args.recon_dir)
    if not forecast.is_absolute():
        forecast = _REPO / forecast
    if not recon.is_absolute():
        recon = _REPO / recon
    run(forecast_dir=forecast, recon_dir=recon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
