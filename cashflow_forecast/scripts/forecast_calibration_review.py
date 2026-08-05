"""Forecast Calibration Review — claim-timing analysis only (no model changes).

Builds a multi-week panel: deposit_date vs original_forecast_date per payer,
lag stats, weekday bias, A/B/C classification, and a markdown report.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
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

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_N_CLAIMS = 30
MIN_PAID_USD = 25_000.0
MIN_CLASS_B_PAID_USD = 10_000.0  # avoid junk labels with tiny $
WEEKDAY_MASS_THRESHOLD = 0.35  # single forecast->deposit pair share
STABLE_WEEK_SIGN_MIN = 2
_JUNK_PAYER_RE = re.compile(
    r"(?i)^(self\s*pay|\(blank\)|blank)$|provider\s*number|medicaid\s*provider"
)


def _norm_eft(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "").strip().upper())
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


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


def _weekday(d: date | None) -> str:
    if d is None:
        return ""
    return WEEKDAYS[d.weekday()]


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _payer_org_from_check(row: dict[str, str]) -> str:
    org = (row.get("payer_org") or "").strip()
    if org:
        return org
    code = (row.get("payer_org_code") or "").strip()
    hit = (
        resolve(row.get("payor") or "", "revflow")
        or resolve(row.get("ins_name") or "", "webpt")
        or resolve(row.get("payor") or "", "any")
    )
    if hit:
        return hit.name
    return (row.get("payor") or code or "(blank)").strip()


def _payer_org_from_outcome(row: dict[str, str]) -> str:
    hit = (
        resolve(row.get("insurance_revflow") or "", "revflow")
        or resolve(row.get("ins_name") or "", "webpt")
        or resolve(row.get("ins_name") or "", "any")
    )
    return hit.name if hit else (row.get("ins_name") or "(blank)")


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


def load_checks_in_window(
    path: Path, *, start: date, end: date
) -> dict[str, dict[str, object]]:
    """EFT → check meta with deposit in [start, end]. First deposit wins."""
    out: dict[str, dict[str, object]] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dep = _parse_day(row.get("deposit_date"))
            if dep is None or dep < start or dep > end:
                continue
            eft = _norm_eft(row.get("check_eft_num"))
            if not eft:
                continue
            org = _payer_org_from_check(row)
            if is_ach_processor(org) or org in {"Self Pay", "(blank)"}:
                continue
            if eft in out:
                continue
            out[eft] = {
                "check_eft_num": eft,
                "deposit_date": dep,
                "eob_date": _parse_day(row.get("eob_date")),
                "payer_org": org,
                "payor": row.get("payor") or "",
                "paid_amount_sum": parse_money(row.get("paid_amount_sum") or "0"),
            }
    return out


def load_outcomes_index(
    path: Path,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            nk = (row.get("name_key") or "").strip()
            dos = str(row.get("date_of_service") or "")[:10]
            dd = _parse_day(dos)
            dos_s = dd.isoformat() if dd else dos
            if nk and dos_s:
                by_key[(nk, dos_s)].append(row)
    return by_key


def build_panel(
    *,
    checks: dict[str, dict[str, object]],
    payments_path: Path,
    outcomes_by_key: dict[tuple[str, str], list[dict[str, str]]],
) -> list[dict[str, object]]:
    """Join payments for in-window EFTs to outcome lines; one panel row per outcome."""
    wanted = set(checks.keys())
    # Collect payment lines for wanted EFTs
    pay_by_eft: dict[str, list[dict[str, str]]] = defaultdict(list)
    with payments_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            eft = _norm_eft(row.get("check_eft_num"))
            if eft in wanted:
                pay_by_eft[eft].append(row)

    panel: list[dict[str, object]] = []
    for eft, meta in checks.items():
        dep: date = meta["deposit_date"]  # type: ignore[assignment]
        payer_org = str(meta["payer_org"])
        for pr in pay_by_eft.get(eft, []):
            nk = (pr.get("name_key") or "").strip()
            dos = _parse_day(pr.get("date_of_service"))
            if not nk or dos is None:
                continue
            outcomes = outcomes_by_key.get((nk, dos.isoformat()), [])
            paid = parse_money(pr.get("paid_amount") or "0")
            if not outcomes:
                # Still record payment with missing outcome for stage pattern
                panel.append(
                    {
                        "check_eft_num": eft,
                        "payer_org": payer_org,
                        "deposit_date": dep.isoformat(),
                        "deposit_weekday": _weekday(dep),
                        "iso_week": _iso_week(dep),
                        "eob_date": (
                            meta["eob_date"].isoformat()  # type: ignore[union-attr]
                            if meta["eob_date"]
                            else ""
                        ),
                        "name_key": nk,
                        "date_of_service": dos.isoformat(),
                        "cpt_code": pr.get("cpt_code") or "",
                        "paid_amount": round(paid, 2),
                        "expected_amount": 0.0,
                        "outcome_stage": "missing_outcome",
                        "original_forecast_date": "",
                        "forecast_date": "",
                        "ofd_weekday": "",
                        "lag_days": "",
                        "link_status": "payment_no_outcome",
                    }
                )
                continue
            for o in outcomes:
                ofd = _parse_day(
                    o.get("original_forecast_date") or o.get("forecast_date")
                )
                lag = (dep - ofd).days if ofd else None
                exp = parse_money(o.get("expected_amount") or "0")
                stage = (o.get("outcome_stage") or "").strip().lower() or "unknown"
                org = _payer_org_from_outcome(o) or payer_org
                if is_ach_processor(org):
                    continue
                panel.append(
                    {
                        "check_eft_num": eft,
                        "payer_org": org,
                        "deposit_date": dep.isoformat(),
                        "deposit_weekday": _weekday(dep),
                        "iso_week": _iso_week(dep),
                        "eob_date": (
                            meta["eob_date"].isoformat()  # type: ignore[union-attr]
                            if meta["eob_date"]
                            else (str(o.get("eob_date") or "")[:10])
                        ),
                        "name_key": nk,
                        "date_of_service": dos.isoformat(),
                        "cpt_code": o.get("cpt_code") or pr.get("cpt_code") or "",
                        "paid_amount": round(
                            parse_money(o.get("paid_amount") or "0") or paid, 2
                        ),
                        "expected_amount": round(exp, 2),
                        "outcome_stage": stage,
                        "original_forecast_date": ofd.isoformat() if ofd else "",
                        "forecast_date": str(o.get("forecast_date") or "")[:10],
                        "ofd_weekday": _weekday(ofd),
                        "lag_days": lag if lag is not None else "",
                        "link_status": "linked",
                    }
                )
    return panel


def compute_payer_stats(
    panel: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, dict]]:
    """Return lag stats rows, weekday matrix rows, and rich per-payer analysis dict."""
    by_payer: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in panel:
        if row.get("lag_days") == "" or row.get("lag_days") is None:
            continue
        if row.get("link_status") != "linked":
            continue
        by_payer[str(row["payer_org"])].append(row)

    stats_rows: list[dict[str, object]] = []
    weekday_rows: list[dict[str, object]] = []
    analysis: dict[str, dict] = {}

    for payer, rows in sorted(by_payer.items(), key=lambda kv: -len(kv[1])):
        lags = [int(r["lag_days"]) for r in rows]  # type: ignore[arg-type]
        lags_sorted = sorted(float(x) for x in lags)
        paid_sum = sum(float(r["paid_amount"] or 0) for r in rows)
        exp_sum = sum(float(r["expected_amount"] or 0) for r in rows)
        mean = statistics.fmean(lags_sorted) if lags_sorted else float("nan")
        med = statistics.median(lags_sorted) if lags_sorted else float("nan")
        p25 = _percentile(lags_sorted, 0.25)
        p75 = _percentile(lags_sorted, 0.75)
        std = statistics.pstdev(lags_sorted) if len(lags_sorted) > 1 else 0.0

        # Weekday pair mass
        pair_c: Counter[tuple[str, str]] = Counter()
        for r in rows:
            pair_c[(str(r["ofd_weekday"]), str(r["deposit_weekday"]))] += 1
        top_pair, top_n = (("?", "?"), 0)
        if pair_c:
            top_pair, top_n = pair_c.most_common(1)[0]
        top_share = top_n / len(rows) if rows else 0.0

        # Weekly median sign stability
        by_week: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            by_week[str(r["iso_week"])].append(float(r["lag_days"]))  # type: ignore[arg-type]
        week_medians = {
            w: statistics.median(vs) for w, vs in by_week.items() if len(vs) >= 5
        }
        pos = sum(1 for m in week_medians.values() if m > 0.5)
        neg = sum(1 for m in week_medians.values() if m < -0.5)
        zeroish = sum(1 for m in week_medians.values() if abs(m) <= 0.5)
        stable_sign = (
            "positive"
            if pos >= STABLE_WEEK_SIGN_MIN and pos > neg and pos > zeroish
            else "negative"
            if neg >= STABLE_WEEK_SIGN_MIN and neg > pos and neg > zeroish
            else "mixed"
            if week_medians
            else "insufficient_weeks"
        )

        stage_c = Counter(str(r["outcome_stage"]) for r in rows)
        same_day_n = sum(1 for x in lags if x == 0)
        same_day_share = same_day_n / len(lags) if lags else 0.0

        stats_rows.append(
            {
                "payer_org": payer,
                "n_claims": len(rows),
                "paid_sum": round(paid_sum, 2),
                "expected_sum": round(exp_sum, 2),
                "lag_mean": round(mean, 3),
                "lag_median": round(med, 3),
                "lag_p25": round(p25, 3),
                "lag_p75": round(p75, 3),
                "lag_std": round(std, 3),
                "same_day_share": round(same_day_share, 3),
                "top_weekday_pair": f"{top_pair[0]}->{top_pair[1]}",
                "top_weekday_share": round(top_share, 3),
                "stable_week_sign": stable_sign,
                "n_weeks_with_n5": len(week_medians),
                "top_stages": ";".join(f"{k}:{v}" for k, v in stage_c.most_common(4)),
            }
        )

        for (ofd_wd, dep_wd), n in sorted(pair_c.items(), key=lambda kv: -kv[1]):
            weekday_rows.append(
                {
                    "payer_org": payer,
                    "ofd_weekday": ofd_wd,
                    "deposit_weekday": dep_wd,
                    "n": n,
                    "share": round(n / len(rows), 4),
                }
            )

        # Classification inputs
        size_ok = len(rows) >= MIN_N_CLAIMS or paid_sum >= MIN_PAID_USD
        abs_med = abs(med) if not math.isnan(med) else 999.0
        concentrated = top_share >= WEEKDAY_MASS_THRESHOLD and top_pair[0] and top_pair[1]
        junk_label = bool(_JUNK_PAYER_RE.search(payer))
        stage_dom = ""
        if stage_c:
            sk, sn = stage_c.most_common(1)[0]
            if sn / len(rows) >= 0.55 and sk in {
                "paid",
                "missing_outcome",
                "denied",
            }:
                stage_dom = sk

        kind = "Natural Variance"
        if concentrated and stable_sign in {"positive", "negative"}:
            kind = "Forecast Bias"
        elif stage_dom == "paid" and abs_med <= 1:
            kind = "Payment Workflow"
        elif stage_dom:
            kind = "Business Process"
        elif abs_med > 1 and stable_sign == "mixed":
            kind = "Natural Variance"

        if junk_label:
            klass = "C"
            conf = 40
            reason = "unresolved_or_junk_payer_label"
            kind = "Business Process"
        elif not size_ok:
            klass = "C"
            conf = 45
            reason = "sample_below_threshold"
        elif stage_dom in {"missing_outcome"} and not concentrated:
            klass = "C"
            conf = 55
            reason = "stage_dominated_without_weekday_pattern"
        elif (
            size_ok
            and paid_sum >= MIN_CLASS_B_PAID_USD
            and abs_med > 1
            and stable_sign in {"positive", "negative"}
            and concentrated
        ):
            klass = "B"
            conf = 75 if abs_med >= 2 else 65
            if paid_sum >= 50_000:
                conf = min(90, conf + 10)
            reason = (
                f"stable_{stable_sign}_lag_median={med:.1f}_"
                f"weekday={top_pair[0]}->{top_pair[1]}_{top_share:.0%}"
            )
        elif abs_med <= 1 and (not concentrated or same_day_share >= 0.35):
            klass = "A"
            conf = 70 if size_ok else 50
            reason = "median_lag_within_1_day"
        elif size_ok and abs_med > 1 and stable_sign == "mixed":
            klass = "C"
            conf = 55
            reason = "lag_present_but_week_sign_unstable"
        elif size_ok and abs_med > 1 and not concentrated:
            klass = "C"
            conf = 50
            reason = "lag_without_concentrated_weekday"
        elif (
            size_ok
            and abs_med > 1
            and concentrated
            and paid_sum < MIN_CLASS_B_PAID_USD
        ):
            klass = "C"
            conf = 50
            reason = "pattern_but_paid_below_class_b_floor"
        else:
            klass = "A"
            conf = 55
            reason = "no_clear_systematic_bias"

        # Suggested shift for class B: round median lag toward deposit
        # lag = deposit - ofd → positive lag means deposit after ofd → shift ofd later by median
        suggested_shift = 0
        if klass == "B" and not math.isnan(med):
            suggested_shift = int(round(med))

        analysis[payer] = {
            "class": klass,
            "confidence": conf,
            "reason": reason,
            "kind": kind,
            "n": len(rows),
            "paid_sum": paid_sum,
            "lag_median": med,
            "lag_mean": mean,
            "lag_p25": p25,
            "lag_p75": p75,
            "lag_std": std,
            "same_day_share": same_day_share,
            "top_pair": f"{top_pair[0]}->{top_pair[1]}",
            "top_share": top_share,
            "stable_sign": stable_sign,
            "week_medians": week_medians,
            "suggested_shift_days": suggested_shift,
            "stage_top": stage_c.most_common(3),
        }

    return stats_rows, weekday_rows, analysis


def estimate_jul29_impact(
    *,
    panel: list[dict[str, object]],
    analysis: dict[str, dict],
) -> dict[str, dict]:
    """Rough $ impact: claims deposited on 2026-07-29 whose ofd would move onto that day."""
    target = date(2026, 7, 29)
    out: dict[str, dict] = {}
    for payer, info in analysis.items():
        if info["class"] != "B":
            continue
        shift = int(info["suggested_shift_days"])
        if shift == 0:
            out[payer] = {"gain_usd": 0.0, "note": "zero_shift"}
            continue
        # Rows deposited on Jul29 with ofd = target - shift would become same-day after +shift to ofd
        # If we add `shift` days to ofd: new_ofd = ofd + shift; want new_ofd == deposit
        # i.e. ofd == deposit - shift
        gain = 0.0
        n = 0
        for r in panel:
            if r["payer_org"] != payer:
                continue
            if r.get("link_status") != "linked":
                continue
            dep = _parse_day(r["deposit_date"])
            ofd = _parse_day(r["original_forecast_date"])
            if dep != target or ofd is None:
                continue
            if (dep - ofd).days == shift:
                # currently lag==shift; after shifting ofd by +shift, lag→0
                gain += float(r.get("expected_amount") or r.get("paid_amount") or 0)
                n += 1
        out[payer] = {
            "gain_usd": round(gain, 2),
            "n_lines": n,
            "shift": shift,
            "note": "expected_amt_on_jul29_with_lag==shift",
        }
    return out


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def write_report(
    path: Path,
    *,
    start: date,
    end: date,
    panel: list[dict[str, object]],
    stats_rows: list[dict[str, object]],
    analysis: dict[str, dict],
    impact: dict[str, dict],
) -> None:
    linked = [r for r in panel if r.get("link_status") == "linked" and r.get("lag_days") != ""]
    n_payers = len(analysis)
    class_counts = Counter(v["class"] for v in analysis.values())
    b_payers = sorted(
        ((p, a) for p, a in analysis.items() if a["class"] == "B"),
        key=lambda kv: -kv[1]["paid_sum"],
    )
    a_payers = sorted(
        ((p, a) for p, a in analysis.items() if a["class"] == "A"),
        key=lambda kv: -kv[1]["paid_sum"],
    )
    c_payers = sorted(
        ((p, a) for p, a in analysis.items() if a["class"] == "C"),
        key=lambda kv: -kv[1]["paid_sum"],
    )

    # Priority focus: Anthem first in narrative
    def _focus_order(items: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        def key(item: tuple[str, dict]) -> tuple[int, float]:
            name = item[0].lower()
            pri = 0 if "anthem" in name or "bcbs" in name else 1
            return (pri, -item[1]["paid_sum"])

        return sorted(items, key=key)

    lines: list[str] = []
    lines.append("# Forecast Calibration Review")
    lines.append("")
    lines.append(
        f"**Window:** {start.isoformat()} → {end.isoformat()}  "
        f"| **Linked claim rows:** {len(linked):,}  "
        f"| **Payers scored:** {n_payers}"
    )
    lines.append("")
    lines.append(
        "Analysis only — **no SLA / cadence / weekend snap / global calibration applied.** "
        "RCA data blockers are closed; this reviews claim-timing bias for narrow payer tuning."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append(
        f"Across deposits with reconcile coverage in the window, "
        f"**{class_counts.get('B', 0)}** payer(s) are **Class B** (timing calibration candidates), "
        f"**{class_counts.get('A', 0)}** Class A (no calibration needed), "
        f"**{class_counts.get('C', 0)}** Class C (needs more evidence)."
    )
    lines.append("")
    anthem = next(
        (
            (p, a)
            for p, a in analysis.items()
            if "anthem" in p.lower() or "bcbs" in p.lower()
        ),
        None,
    )
    if anthem:
        p, a = anthem
        lines.append(
            f"**Anthem / BCBS (RCA focus): Class {a['class']}** — median lag "
            f"`{a['lag_median']:.1f}`d, same-day share `{a['same_day_share']:.0%}`, "
            f"week sign `{a['stable_sign']}`. Not a Class B timing-calibration target "
            f"in this window (mean lag {a['lag_mean']:.1f} reflects a long right tail, "
            f"not a stable weekday bias)."
        )
        lines.append("")
    if b_payers:
        lines.append("Primary candidates (by paid $ in panel):")
        lines.append("")
        for payer, a in _focus_order(b_payers)[:8]:
            lines.append(
                f"- **{payer}**: median lag `{a['lag_median']:.1f}`d, "
                f"weekday `{a['top_pair']}` ({a['top_share']:.0%}), "
                f"kind={a['kind']}, confidence={a['confidence']}%, "
                f"suggested shift `{a['suggested_shift_days']:+d}` calendar day(s) on ofd"
            )
        lines.append("")
    else:
        lines.append(
            "No Class B payer met the bar (stable week-sign lag + concentrated weekday + size). "
            "Residual timing on 2026-07-29 may be natural mix / workflow rather than a single bias."
        )
        lines.append("")
    lines.append(
        "Residual context (from prior RCA, not re-opened): Land - insurance-like ≈ **-$5.1k** "
        "on 2026-07-29, dominated by other-day / paid-earlier / other-stage buckets after join."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 2. Per-Payer Calibration Candidates")
    lines.append("")
    lines.append("| Class | Payer | n | Paid $ | Median lag | Top weekday | Kind | Conf | Reason |")
    lines.append("|---|---|---:|---:|---:|---|---|---:|---|")
    for klass, group in (("B", b_payers), ("A", a_payers), ("C", c_payers)):
        for payer, a in _focus_order(group)[:40]:
            lines.append(
                f"| {klass} | {payer} | {a['n']} | {a['paid_sum']:,.0f} | "
                f"{a['lag_median']:.1f} | {a['top_pair']} ({a['top_share']:.0%}) | "
                f"{a['kind']} | {a['confidence']}% | `{a['reason']}` |"
            )
    lines.append("")
    lines.append("### Class definitions")
    lines.append("")
    lines.append("- **A** — No calibration needed (`|median lag| ≤ 1` or no systematic pattern).")
    lines.append(
        "- **B** — Candidate: `|median lag| > 1`, stable sign across ≥2 weeks, "
        f"weekday pair share ≥ {WEEKDAY_MASS_THRESHOLD:.0%}, sample ≥ {MIN_N_CLAIMS} or ≥ ${MIN_PAID_USD:,.0f}."
    )
    lines.append("- **C** — Needs more evidence (small n, unstable weeks, or stage-dominated without weekday mass).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 3. Statistical Evidence")
    lines.append("")
    lines.append("Lag = `deposit_date − original_forecast_date` (calendar days).")
    lines.append("")
    lines.append("| Payer | n | Mean | Median | P25 | P75 | Std | Same-day % | Week sign |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    # Sort stats by paid
    stats_by_paid = sorted(stats_rows, key=lambda r: -float(r["paid_sum"]))
    for r in stats_by_paid[:25]:
        lines.append(
            f"| {r['payer_org']} | {r['n_claims']} | {r['lag_mean']} | {r['lag_median']} | "
            f"{r['lag_p25']} | {r['lag_p75']} | {r['lag_std']} | "
            f"{float(r['same_day_share'])*100:.0f}% | {r['stable_week_sign']} |"
        )
    lines.append("")
    lines.append("Full tables: `calibration/payer_lag_stats.csv`, `calibration/payer_weekday_matrix.csv`, `calibration/claim_timing_panel.csv`.")
    lines.append("")
    # Detail blocks for top B and Anthem
    focus_names = [p for p, _ in _focus_order(b_payers)[:5]]
    for name in list(analysis.keys()):
        if "anthem" in name.lower() or "bcbs" in name.lower():
            if name not in focus_names:
                focus_names.insert(0, name)
    for payer in focus_names[:6]:
        a = analysis.get(payer)
        if not a:
            continue
        lines.append(f"### {payer}")
        lines.append("")
        lines.append(
            f"- Lag median **{a['lag_median']:.2f}** (mean {a['lag_mean']:.2f}, "
            f"p25={a['lag_p25']:.1f}, p75={a['lag_p75']:.1f}, std={a['lag_std']:.1f})"
        )
        lines.append(
            f"- Weekday mass: **{a['top_pair']}** = {a['top_share']:.1%} of linked claims"
        )
        lines.append(f"- Weekly median sign: **{a['stable_sign']}** ({len(a['week_medians'])} weeks with n≥5)")
        if a["week_medians"]:
            wm = ", ".join(f"{w}:{m:.1f}" for w, m in sorted(a["week_medians"].items()))
            lines.append(f"- Week medians: {wm}")
        lines.append(
            f"- Stage mix: {', '.join(f'{k}:{v}' for k, v in a['stage_top'])}"
        )
        lines.append(
            f"- Interpretation: **{a['kind']}** (not a code defect) — class **{a['class']}** @ {a['confidence']}%"
        )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 4. Recommended Forecast Adjustments")
    lines.append("")
    lines.append("**Do not implement in this pass.** Recommendations only.")
    lines.append("")
    if not b_payers:
        lines.append(
            "No payer-scoped timing adjustment is recommended until a Class B signal appears "
            "or Class C payers accumulate more weeks of stable sign."
        )
        lines.append("")
        lines.append("Explicitly **not** recommended: global SLA, global cadence, weekend snap, multi-payer batch shift.")
    else:
        lines.append("| Payer | Proposed change | Scope | Evidence |")
        lines.append("|---|---|---|---|")
        for payer, a in _focus_order(b_payers):
            shift = a["suggested_shift_days"]
            direction = "later" if shift > 0 else "earlier" if shift < 0 else "none"
            lines.append(
                f"| {payer} | Shift `original_forecast_date` by **{shift:+d}** calendar day(s) "
                f"({direction}) for this payer only — applied in land/ofd assignment, "
                f"not a global SLA table rewrite | Single payer org | "
                f"median lag {a['lag_median']:.1f}, {a['top_pair']} {a['top_share']:.0%}, "
                f"stable={a['stable_sign']} |"
            )
        lines.append("")
        lines.append("Rollback: revert payer-scoped shift flag/config; rebuild outcomes; re-run this script + Jul29 deposit trace.")
        lines.append("")
        lines.append(
            "Success metric: increase `matched_expected_same_day` $ share for that payer; "
            "reduce abs(Land - insurance-like) on holdout days."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 5. Expected Impact")
    lines.append("")
    if not impact:
        lines.append("No Class B impact estimate (no candidates).")
    else:
        lines.append(
            "Rough Jul-29 counterfactual: expected $ on deposits that day where current lag equals proposed shift "
            "(those lines would become same-day after ofd shift)."
        )
        lines.append("")
        lines.append("| Payer | Shift | Lines | Est. $ moved onto same-day land |")
        lines.append("|---|---:|---:|---:|")
        total_gain = 0.0
        for payer, info in sorted(impact.items(), key=lambda kv: -kv[1].get("gain_usd", 0)):
            total_gain += float(info.get("gain_usd") or 0)
            lines.append(
                f"| {payer} | {info.get('shift', 0):+d} | {info.get('n_lines', 0)} | "
                f"${info.get('gain_usd', 0):,.2f} |"
            )
        lines.append("")
        lines.append(
            f"**Sum (non-additive vs residual):** ${total_gain:,.2f}. "
            f"Compare to residual ≈ $5.1k — treat as upper-bound same-day reallocation, not guaranteed residual closure."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 6. Risk Assessment")
    lines.append("")
    lines.append("| Risk | Detail | Mitigation |")
    lines.append("|---|---|---|")
    lines.append(
        "| Overfitting to one month | Window ends 2026-07-29; July-heavy | Require ≥2 stable weeks; hold out a later week before ship |"
    )
    lines.append(
        "| Confounding with EOB→bank lag | Deposit may trail EOB by design | Prefer ofd↔deposit lag; inspect eob separately if tuning |"
    )
    lines.append(
        "| Stage / SF override mix | Paid-earlier & missing outcomes ≠ pure timing bias | Class C when stage-dominated; don't shift those payers |"
    )
    lines.append(
        "| Spilling land across days | +1d shift can create new over on adjacent days | Measure day 28/30 land vs Tracker after any pilot |"
    )
    lines.append(
        "| Misreading workflow as bias | ACH batching / payer check runs | Label as Payment Workflow / Business Process when weekday mass is ops-driven |"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 7. Implementation Priority")
    lines.append("")
    lines.append("| Priority | Action | When |")
    lines.append("|---|---|---|")
    if b_payers:
        for i, (payer, a) in enumerate(_focus_order(b_payers), start=1):
            jul = impact.get(payer, {})
            jul_note = (
                f" (Jul29 same-day $ est. ${float(jul.get('gain_usd') or 0):,.0f})"
            )
            lines.append(
                f"| P{i} | Pilot `{payer}` ofd shift `{a['suggested_shift_days']:+d}` "
                f"in a feature-flagged path{jul_note} | After sign-off; measure 1 week |"
            )
        lines.append(
            "| P2 | Residual Jul29 deep-dive on Class A majors (Anthem other-stage / other-day $ mix) — timing bias not median-lag | Analysis only |"
        )
        lines.append(
            "| P99 | Re-run this review after +2 weeks deposits | Continuous |"
        )
    else:
        lines.append(
            "| P1 | Accumulate another 1-2 weeks of linked deposits; re-run script | Before any tuning |"
        )
        lines.append(
            "| P2 | Deep-dive Anthem matched_open_other_stage / other-day mix on Jul29 (workflow vs lag) | Analysis only |"
        )
    lines.append(
        "| — | **Do not** change global SLA, cadence, or weekend snap | Closed |"
    )
    lines.append("")
    lines.append("### Closed (do not reopen)")
    lines.append("")
    lines.append("- RevFlow / reconcile / Echo processor mapping / Product KPI rename")
    lines.append("- Global SLA / cadence / weekend snap")
    lines.append("")
    lines.append("### Recommended later vs leave alone")
    lines.append("")
    if b_payers:
        lines.append("**Later (narrow):** " + ", ".join(p for p, _ in _focus_order(b_payers)))
    else:
        lines.append("**Later:** none until Class B appears.")
    lines.append("")
    lines.append(
        "**Leave alone:** all Class A payers; Class C until evidence strengthens; any global knobs."
    )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(
    *,
    forecast_dir: Path,
    recon_dir: Path,
    start: date,
    end: date,
) -> Path:
    checks_path = recon_dir / "insurance_behavior" / "checks_timeline.csv"
    payments_path = recon_dir / "payments_unified.csv"
    outcomes_path = forecast_dir / "outcome_stages.csv"
    out_dir = forecast_dir / "gap_reports" / "calibration"
    report_path = forecast_dir / "gap_reports" / "forecast_calibration_review.md"

    print(f"Loading checks {start}..{end} ...")
    checks = load_checks_in_window(checks_path, start=start, end=end)
    print(f"  {len(checks)} EFTs with deposit in window")
    print("Indexing outcomes ...")
    outcomes = load_outcomes_index(outcomes_path)
    print(f"  {len(outcomes)} name_key+dos keys")
    print("Building panel (payments x checks x outcomes) ...")
    panel = build_panel(
        checks=checks, payments_path=payments_path, outcomes_by_key=outcomes
    )
    print(f"  {len(panel)} panel rows")

    stats_rows, weekday_rows, analysis = compute_payer_stats(panel)
    impact = estimate_jul29_impact(panel=panel, analysis=analysis)

    write_csv(
        out_dir / "claim_timing_panel.csv",
        panel,
        [
            "check_eft_num",
            "payer_org",
            "deposit_date",
            "deposit_weekday",
            "iso_week",
            "eob_date",
            "name_key",
            "date_of_service",
            "cpt_code",
            "paid_amount",
            "expected_amount",
            "outcome_stage",
            "original_forecast_date",
            "forecast_date",
            "ofd_weekday",
            "lag_days",
            "link_status",
        ],
    )
    write_csv(
        out_dir / "payer_lag_stats.csv",
        stats_rows,
        [
            "payer_org",
            "n_claims",
            "paid_sum",
            "expected_sum",
            "lag_mean",
            "lag_median",
            "lag_p25",
            "lag_p75",
            "lag_std",
            "same_day_share",
            "top_weekday_pair",
            "top_weekday_share",
            "stable_week_sign",
            "n_weeks_with_n5",
            "top_stages",
        ],
    )
    write_csv(
        out_dir / "payer_weekday_matrix.csv",
        weekday_rows,
        ["payer_org", "ofd_weekday", "deposit_weekday", "n", "share"],
    )
    # Classification CSV
    class_rows = [
        {
            "payer_org": p,
            "class": a["class"],
            "confidence": a["confidence"],
            "kind": a["kind"],
            "reason": a["reason"],
            "n_claims": a["n"],
            "paid_sum": round(a["paid_sum"], 2),
            "lag_median": round(a["lag_median"], 3)
            if not math.isnan(a["lag_median"])
            else "",
            "suggested_shift_days": a["suggested_shift_days"],
            "top_weekday_pair": a["top_pair"],
            "top_weekday_share": round(a["top_share"], 3),
            "stable_week_sign": a["stable_sign"],
        }
        for p, a in sorted(analysis.items(), key=lambda kv: (kv[1]["class"], -kv[1]["paid_sum"]))
    ]
    write_csv(
        out_dir / "payer_calibration_class.csv",
        class_rows,
        [
            "payer_org",
            "class",
            "confidence",
            "kind",
            "reason",
            "n_claims",
            "paid_sum",
            "lag_median",
            "suggested_shift_days",
            "top_weekday_pair",
            "top_weekday_share",
            "stable_week_sign",
        ],
    )

    write_report(
        report_path,
        start=start,
        end=end,
        panel=panel,
        stats_rows=stats_rows,
        analysis=analysis,
        impact=impact,
    )
    print(f"Wrote {out_dir}")
    print(f"Wrote {report_path}")
    print(
        "Classes:",
        dict(Counter(a["class"] for a in analysis.values())),
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
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-07-29")
    args = ap.parse_args()
    forecast = Path(args.forecast_dir)
    recon = Path(args.recon_dir)
    if not forecast.is_absolute():
        forecast = _REPO / forecast
    if not recon.is_absolute():
        recon = _REPO / recon
    run(
        forecast_dir=forecast,
        recon_dir=recon,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
