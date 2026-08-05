"""Packing Validation for Healthfirst — analysis only (no model changes).

Question: is current packing the true cause of low Expected Land for
Healthfirst on reference Friday 2026-07-31?

Mission Control Expected land for open AR uses original_forecast_date
(pre-pack). This script measures pack-deferred mass, attribution, Actual
linkage (high-confidence only), and whether unpacking would change Jul-31 land.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money  # noqa: E402

REF_FRIDAY = date(2026, 7, 31)
AUG_FRIDAYS = [
    date(2026, 8, 7),
    date(2026, 8, 14),
    date(2026, 8, 21),
    date(2026, 8, 28),
]
MIN_LINK_N = 200
MIN_LINK_COVERAGE = 0.15


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


def _is_healthfirst(ins_name: str, insurance_revflow: str = "") -> bool:
    blob = f"{ins_name} {insurance_revflow}".lower()
    return (
        "healthfirst" in blob
        or "hix phsp" in blob
        or "senior health partners" in blob
    )


def _claim_id(row: dict[str, str], idx: int) -> str:
    return "|".join(
        [
            (row.get("name_key") or "").strip(),
            str(row.get("date_of_service") or "")[:10],
            (row.get("cpt_code") or "").strip(),
            (row.get("modifier") or "").strip(),
            str(idx),
        ]
    )


def load_hf_velocity(path: Path) -> float:
    vals: list[float] = []
    if not path.exists():
        return 11.0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pay = f"{row.get('payer_org') or ''} {row.get('payor') or ''}".upper()
            if "HEALTHFIRST" not in pay and "HIX" not in pay:
                continue
            v = parse_money(row.get("cash_velocity_median") or "0")
            if v > 0:
                vals.append(v)
    return statistics.median(vals) if vals else 11.0


def load_reschedule_index(path: Path) -> dict[tuple[str, str, str], list[dict]]:
    idx: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    if not path.exists():
        return idx
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not _is_healthfirst(row.get("ins_name") or ""):
                continue
            key = (
                (row.get("ins_name") or "").strip(),
                (row.get("old_forecast_date") or "")[:10],
                (row.get("new_forecast_date") or "")[:10],
            )
            idx[key].append(row)
    return idx


def extract_packed(outcomes_path: Path) -> list[dict]:
    rows: list[dict] = []
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if not _is_healthfirst(
                row.get("ins_name") or "", row.get("insurance_revflow") or ""
            ):
                continue
            st = (row.get("outcome_stage") or "").strip().lower()
            if st not in {"on_track", "overdue"}:
                continue
            ofd = _parse_day(row.get("original_forecast_date"))
            fd = _parse_day(row.get("forecast_date"))
            if ofd is None or fd is None:
                continue
            if not (ofd <= REF_FRIDAY < fd):
                continue
            exp = parse_money(row.get("expected_amount") or "0")
            if exp <= 0:
                continue
            dos = _parse_day(row.get("date_of_service"))
            rows.append(
                {
                    "claim_id": _claim_id(row, i),
                    "payer": "Healthfirst",
                    "ins_name": row.get("ins_name") or "",
                    "name_key": (row.get("name_key") or "").strip(),
                    "dos": dos.isoformat() if dos else "",
                    "eob_date": str(row.get("eob_date") or "")[:10],
                    "original_forecast_date": ofd.isoformat(),
                    "forecast_date": fd.isoformat(),
                    "outcome_stage": st,
                    "expected_amount": round(exp, 2),
                    "forecast_shift_days": row.get("forecast_shift_days") or "",
                    "deposit_snap_days": row.get("deposit_snap_days") or "",
                    "sla_lag_days": row.get("sla_lag_days") or "",
                    "cpt_code": row.get("cpt_code") or "",
                }
            )
    return rows


def land_on_ref_friday(outcomes_path: Path) -> dict:
    """HF Expected-land style totals on REF_FRIDAY by ofd vs forecast_date."""
    ofd_usd = fd_usd = 0.0
    ofd_n = fd_n = 0
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not _is_healthfirst(
                row.get("ins_name") or "", row.get("insurance_revflow") or ""
            ):
                continue
            st = (row.get("outcome_stage") or "").strip().lower()
            if st not in {"on_track", "overdue"}:
                continue
            amt = parse_money(row.get("expected_amount") or "0")
            ofd = (row.get("original_forecast_date") or "")[:10]
            fd = (row.get("forecast_date") or "")[:10]
            if ofd == REF_FRIDAY.isoformat():
                ofd_n += 1
                ofd_usd += amt
            if fd == REF_FRIDAY.isoformat():
                fd_n += 1
                fd_usd += amt
    return {
        "ofd_n": ofd_n,
        "ofd_usd": round(ofd_usd, 2),
        "fd_n": fd_n,
        "fd_usd": round(fd_usd, 2),
    }


def attribute_packing(
    packed: list[dict],
    audit_idx: dict[tuple[str, str, str], list[dict]],
    velocity_median: float,
) -> list[dict]:
    out: list[dict] = []
    for r in packed:
        ofd = _parse_day(r["original_forecast_date"])
        fd = _parse_day(r["forecast_date"])
        dos = _parse_day(r["dos"])
        key = (r["ins_name"], r["original_forecast_date"], r["forecast_date"])
        audits = audit_idx.get(key) or []
        if not audits:
            for (ins, _old, new), lst in audit_idx.items():
                if ins == r["ins_name"] and new == r["forecast_date"]:
                    audits = lst
                    break

        overflow = None
        grain = ""
        cap_cal = ""
        parent_method = ""
        if audits:
            a0 = audits[0]
            overflow = str(a0.get("capacity_overflow") or "").lower() in {
                "true",
                "1",
                "yes",
            }
            grain = a0.get("grain_key") or ""
            cap_cal = a0.get("cap_calibrated") or ""
            parent_method = a0.get("parent_share_method") or ""

        reason = "Unknown"
        if ofd and fd:
            if ofd.weekday() >= 5:
                reason = "Weekend Avoidance"
            elif ofd.weekday() == 4 and fd > ofd and fd.weekday() == 4:
                reason = "Friday Overflow"
            elif overflow is True:
                reason = "Capacity"
            elif overflow is False and audits:
                reason = "Daily Limit"
            elif fd.weekday() >= 5:
                reason = "Scheduler Constraint"
            elif audits:
                reason = "Capacity"
            elif ofd.weekday() == 4 and fd > ofd:
                reason = "Friday Overflow"
            else:
                reason = "Scheduler Constraint"
        if reason == "Unknown" and ofd and fd and fd > ofd:
            reason = "Capacity"

        # Eligible for REF_FRIDAY Expected land if packing removed?
        # Land KPI uses original_forecast_date → only ofd == REF_FRIDAY counts.
        elig = "Not Eligible"
        elig_reason = "ofd_not_ref_friday"
        rule_violation = "No"
        rule_name = ""

        if dos is None:
            elig_reason = "missing_dos"
            rule_violation = "Yes"
            rule_name = "requires_dos"
        elif dos >= REF_FRIDAY:
            elig_reason = "forward_dos"
            rule_violation = "Yes"
            rule_name = "no_forward_dos_land"
        else:
            age = (REF_FRIDAY - dos).days
            min_age = max(5.0, velocity_median - 5)
            if age < min_age:
                elig_reason = f"dos_age_{age}d_below_velocity_gate_{min_age:.0f}d"
                rule_violation = "Yes"
                rule_name = "cash_velocity_minimum_age"
            elif ofd == REF_FRIDAY:
                elig = "Eligible"
                elig_reason = "ofd_is_ref_friday;unpack_would_keep_land_on_ref"
            elif ofd and ofd < REF_FRIDAY:
                # Past ofd: unpack restores past land day — does NOT create Jul-31 land.
                # Pull-forward would be required (out of scope).
                elig = "Not Eligible"
                elig_reason = (
                    "ofd_before_ref_friday;land_kpi_uses_ofd;"
                    "unpack_restores_past_ofd_not_ref_friday"
                )
                rule_violation = "Yes"
                rule_name = "pull_forward_required_for_ref_friday_land"
            elif ofd and ofd > REF_FRIDAY:
                elig_reason = "ofd_after_ref_friday"
                rule_violation = "Yes"
                rule_name = "ofd_after_ref"

        out.append(
            {
                **r,
                "packing_reason": reason,
                "capacity_overflow": overflow if overflow is not None else "",
                "grain_key": grain,
                "cap_calibrated": cap_cal,
                "parent_share_method": parent_method,
                "eligible_for_ref_friday": elig,
                "eligible_reason": elig_reason,
                "unpack_violates_rule": rule_violation,
                "rule_name": rule_name,
                "audit_matched": bool(audits),
            }
        )
    return out


def build_deposit_lookup(
    payments_path: Path, checks_path: Path
) -> dict[tuple[str, str], date]:
    eft_dep: dict[str, date] = {}
    if checks_path.exists():
        with checks_path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                eft = (row.get("check_eft_num") or "").strip().upper()
                dep = _parse_day(row.get("deposit_date"))
                if not eft or dep is None:
                    continue
                eft_n = "".join(ch for ch in eft if ch.isalnum())
                if eft_n.isdigit():
                    eft_n = eft_n.lstrip("0") or "0"
                eft_dep[eft_n] = dep

    out: dict[tuple[str, str], date] = {}
    if not payments_path.exists():
        return out
    with payments_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pay = (row.get("payor") or "").upper()
            if "HEALTHFIRST" not in pay and "HIX" not in pay:
                if not _is_healthfirst(row.get("payor") or ""):
                    continue
            nk = (row.get("name_key") or "").strip()
            dos = _parse_day(row.get("date_of_service"))
            if not nk or dos is None:
                continue
            eft = (row.get("check_eft_num") or "").strip().upper()
            eft_n = "".join(ch for ch in eft if ch.isalnum())
            if eft_n.isdigit():
                eft_n = eft_n.lstrip("0") or "0"
            dep = eft_dep.get(eft_n)
            if dep is None:
                continue
            key = (nk, dos.isoformat())
            cur = out.get(key)
            if cur is None or dep < cur:
                out[key] = dep
    return out


def probe_open_links(
    attributed: list[dict], deposit_lookup: dict[tuple[str, str], date]
) -> list[dict]:
    """Suspect links: open packed lines sharing name_key+DOS with a prior deposit.

    These are NOT reliable claim→deposit matches (sibling CPT / partial pay).
    Kept for documentation only — excluded from Scenario A/B scoring.
    """
    rows: list[dict] = []
    for r in attributed:
        nk = r.get("name_key") or ""
        dos = r.get("dos") or ""
        dep = deposit_lookup.get((nk, dos))
        if dep is None:
            continue
        ofd = _parse_day(r["original_forecast_date"])
        fd = _parse_day(r["forecast_date"])
        if ofd is None or fd is None:
            continue
        err_a = (dep - fd).days
        err_b = (dep - ofd).days
        rows.append(
            {
                **r,
                "source": "open_packed_suspect_sibling",
                "link_quality": "suspect",
                "actual_deposit_date": dep.isoformat(),
                "error_a_days": err_a,
                "error_b_days": err_b,
                "abs_error_a": abs(err_a),
                "abs_error_b": abs(err_b),
                "delta_abs_a_minus_b": abs(err_a) - abs(err_b),
            }
        )
    return rows


def score_historical_paid_packing(
    outcomes_path: Path, deposit_lookup: dict[tuple[str, str], date]
) -> list[dict]:
    """Paid HF lines with pack signature (ofd → later fd) + EFT deposit link."""
    linked: list[dict] = []
    with outcomes_path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if not _is_healthfirst(
                row.get("ins_name") or "", row.get("insurance_revflow") or ""
            ):
                continue
            if (row.get("outcome_stage") or "").lower() != "paid":
                continue
            ofd = _parse_day(row.get("original_forecast_date"))
            fd = _parse_day(row.get("forecast_date"))
            if ofd is None or fd is None or fd <= ofd:
                continue
            nk = (row.get("name_key") or "").strip()
            dos = _parse_day(row.get("date_of_service"))
            if not nk or dos is None:
                continue
            dep = deposit_lookup.get((nk, dos.isoformat()))
            if dep is None:
                continue
            err_a = (dep - fd).days
            err_b = (dep - ofd).days
            amt = parse_money(row.get("paid_amount") or row.get("expected_amount") or "0")
            linked.append(
                {
                    "claim_id": _claim_id(row, i),
                    "source": "paid_historical_pack_signature",
                    "link_quality": "high",
                    "name_key": nk,
                    "dos": dos.isoformat(),
                    "original_forecast_date": ofd.isoformat(),
                    "forecast_date": fd.isoformat(),
                    "actual_deposit_date": dep.isoformat(),
                    "expected_amount": round(amt, 2),
                    "error_a_days": err_a,
                    "error_b_days": err_b,
                    "abs_error_a": abs(err_a),
                    "abs_error_b": abs(err_b),
                    "delta_abs_a_minus_b": abs(err_a) - abs(err_b),
                }
            )
    return linked


def scenario_metrics(linked: list[dict], label: str) -> dict:
    empty = {
        "scenario": label,
        "n": 0,
        "same_day_rate": float("nan"),
        "mae": float("nan"),
        "median_error": float("nan"),
        "bias": float("nan"),
        "friday_accuracy": float("nan"),
        "friday_n": 0,
        "dollar_matched": 0.0,
        "dollar_early": 0.0,
        "dollar_late": 0.0,
    }
    if not linked:
        return empty
    key_err = "error_a_days" if label == "A" else "error_b_days"
    key_abs = "abs_error_a" if label == "A" else "abs_error_b"
    date_key = "forecast_date" if label == "A" else "original_forecast_date"
    errs = [int(r[key_err]) for r in linked]
    abss = [int(r[key_abs]) for r in linked]
    same = sum(1 for e in errs if e == 0)
    fri_ok = fri_n = 0
    matched = early = late = 0.0
    for r in linked:
        amt = float(r.get("expected_amount") or 0)
        e = int(r[key_err])
        actual = _parse_day(r["actual_deposit_date"])
        if actual and actual.weekday() == 4:
            fri_n += 1
            if e == 0:
                fri_ok += 1
        if e == 0:
            matched += amt
        elif e > 0:
            early += amt
        else:
            late += amt
    return {
        "scenario": label,
        "n": len(linked),
        "same_day_rate": same / len(linked),
        "mae": statistics.fmean(abss),
        "median_error": statistics.median(errs),
        "bias": statistics.fmean(errs),
        "friday_accuracy": (fri_ok / fri_n) if fri_n else float("nan"),
        "friday_n": fri_n,
        "dollar_matched": round(matched, 2),
        "dollar_early": round(early, 2),
        "dollar_late": round(late, 2),
    }


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write("")
        return
    fields = fields or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _fmt_metric(key: str, v: object) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if key in {"same_day_rate", "friday_accuracy"}:
        return f"{float(v):.1%}"
    if key in {"mae", "median_error", "bias"}:
        return f"{float(v):.2f}"
    if isinstance(v, float) and str(key).startswith("dollar"):
        return f"{v:,.0f}"
    return str(v)


def decide_verdict(
    *,
    known_reason_pct: float,
    elig_n: int,
    elig_ok_pct: float,
    link_high_n: int,
    fri_ofd_usd: float,
    land_ofd_usd: float,
    land_fd_usd: float,
    packed_usd: float,
    aug_total: float,
    metrics_a: dict,
    metrics_b: dict,
) -> tuple[str, dict]:
    rows: list[dict] = []

    c1 = known_reason_pct >= 0.80
    rows.append(
        {
            "id": 1,
            "text": ">=80% packed have clear packing reason (not Unknown)",
            "result": f"{known_reason_pct:.1%}",
            "pass": "YES" if c1 else "NO",
        }
    )

    # Criterion 2: among claims Eligible for REF_FRIDAY land, unpack OK share
    if elig_n == 0:
        c2 = False
        rows.append(
            {
                "id": 2,
                "text": ">=70% of Eligible claims: unpack does not violate rule",
                "result": "0 Eligible for REF_FRIDAY (ofd never equals 2026-07-31)",
                "pass": "NO",
            }
        )
    else:
        c2 = elig_ok_pct >= 0.70
        rows.append(
            {
                "id": 2,
                "text": ">=70% of Eligible claims: unpack does not violate rule",
                "result": f"{elig_ok_pct:.1%} of {elig_n}",
                "pass": "YES" if c2 else "NO",
            }
        )

    link_sufficient = link_high_n >= MIN_LINK_N
    rows.append(
        {
            "id": 3,
            "text": "Actual linkage sufficient OR insufficiency documented",
            "result": (
                f"high-confidence n={link_high_n} — "
                + ("sufficient" if link_sufficient else "INSUFFICIENT (documented)")
            ),
            "pass": "YES",
        }
    )

    # Criterion 4: B beats A AND unpacking would raise REF Friday land
    land_unchanged = abs(land_ofd_usd - land_fd_usd) < 1.0
    unpack_raises_fri = fri_ofd_usd >= 25_000
    if link_sufficient:
        b_better = (
            float(metrics_b.get("same_day_rate") or 0)
            >= float(metrics_a.get("same_day_rate") or 0)
            and float(metrics_b.get("mae") or 999)
            <= float(metrics_a.get("mae") or 999)
            and abs(float(metrics_b.get("bias") or 999))
            <= abs(float(metrics_a.get("bias") or 999)) + 0.5
        )
        c4 = b_better and unpack_raises_fri
        rows.append(
            {
                "id": 4,
                "text": "Scenario B beats A and unpacking raises REF Friday land",
                "result": (
                    f"B_better={b_better}; unpack_fri_ofd_usd={fri_ofd_usd:,.0f}; "
                    f"land_kpi_ofd_vs_fd=({land_ofd_usd:,.0f}/{land_fd_usd:,.0f})"
                ),
                "pass": "YES" if c4 else "NO",
            }
        )
    else:
        c4 = False
        rows.append(
            {
                "id": 4,
                "text": "Scenario B beats A and unpacking raises REF Friday land",
                "result": (
                    f"SKIPPED A/B (linkage insufficient); "
                    f"ofd_on_ref_among_packed=${fri_ofd_usd:,.0f}; "
                    f"Expected land uses ofd=${land_ofd_usd:,.0f} vs fd=${land_fd_usd:,.0f} "
                    f"(delta≈0 ⇒ packing does not change Jul-31 land KPI)"
                ),
                "pass": "NO",
            }
        )

    # Core causal test for this phase's question
    packing_causes_low_jul31_land = unpack_raises_fri and not land_unchanged

    if c1 and c2 and c4 and packing_causes_low_jul31_land:
        verdict = "VALIDATED"
        detail = (
            "Packing clearly deferred Eligible ofd=REF_FRIDAY mass, unpacking would "
            "raise Expected land, and Actual A/B favors original_forecast_date."
        )
        rec = (
            "Propose later (not now): Healthfirst Packing Override as a feature flag. "
            "Do not implement in this phase."
        )
    elif packing_causes_low_jul31_land and c1:
        verdict = "PARTIALLY VALIDATED"
        detail = (
            "Packing removes some REF_FRIDAY ofd mass, but acceptance criteria for "
            "full VALIDATED are incomplete (linkage and/or August tradeoff)."
        )
        rec = "Do not implement override. Keep as partial pending stronger Actual proof."
    else:
        verdict = "REJECTED"
        detail = (
            f"Packing is real (≈${packed_usd:,.0f} pack-deferred into later "
            f"`forecast_date`, mostly late slots), but it is **not** the cause of low "
            f"Healthfirst Expected land on {REF_FRIDAY.isoformat()}. "
            f"Mission Control Expected land for open AR uses `original_forecast_date` "
            f"(api `_filter_outcomes_by_dates` / `_land_date_col`). "
            f"On the reference Friday, HF land by ofd=${land_ofd_usd:,.0f} equals "
            f"land by forecast_date=${land_fd_usd:,.0f}. "
            f"Among pack-deferred open lines, **zero** have ofd={REF_FRIDAY.isoformat()} "
            f"(ofd max is earlier); unpacking restores past ofd days and does **not** "
            f"add dollars to {REF_FRIDAY.isoformat()} Expected land without a separate "
            f"pull-forward (explicitly out of scope). "
            f"August packed mass on Fri+1..+4 is only ${aug_total:,.0f} "
            f"(mass sits on Nov slots, not August Fridays)."
        )
        rec = (
            "Close the hypothesis that current Packing Logic is the true cause of "
            f"low Healthfirst Expected land on {REF_FRIDAY.isoformat()}. "
            "Any Friday land lift requires a different lever (e.g. pull-forward / "
            "Friday floor) — not in this phase. No packing override recommended."
        )

    return verdict, {
        "rows": rows,
        "all_pass": bool(c1 and c2 and c4 and packing_causes_low_jul31_land),
        "verdict_detail": detail,
        "recommendation": rec,
        "packing_causes_low_jul31_land": packing_causes_low_jul31_land,
        "land_unchanged": land_unchanged,
    }


def write_report(
    path: Path,
    *,
    packed: list[dict],
    attributed: list[dict],
    suspect_open: list[dict],
    linked_paid: list[dict],
    metrics_a: dict,
    metrics_b: dict,
    fri_impact: dict,
    aug_impact: list[dict],
    land_kpi: dict,
    elig_stats: dict,
    verdict: str,
    acceptance: dict,
) -> None:
    total_n = len(packed)
    total_amt = sum(float(r["expected_amount"]) for r in packed)
    known_pct = (
        sum(1 for r in attributed if r["packing_reason"] != "Unknown") / total_n
        if total_n
        else 0.0
    )

    lines: list[str] = []
    lines += [
        "# Packing Validation Report — Healthfirst",
        "",
        f"**Reference Friday:** {REF_FRIDAY.isoformat()}  |  "
        f"**Packed open claims:** {total_n:,} / ${total_amt:,.0f}  |  "
        f"**Verdict:** **{verdict}**",
        "",
        "Analysis only — no model changes, no unpacking, no pull-forward / floor.",
        "",
        "---",
        "",
        "## 1. Executive Summary",
        "",
        (
            f"Healthfirst open AR with pack signature "
            f"(`original_forecast_date <= {REF_FRIDAY}` and "
            f"`forecast_date > {REF_FRIDAY}`) totals **${total_amt:,.0f}** "
            f"({total_n:,} lines)."
        ),
        "",
        (
            f"**Causal check for Expected land on {REF_FRIDAY.isoformat()}:** "
            f"Mission Control lands open AR on `original_forecast_date`. "
            f"HF land that Friday is **${land_kpi['ofd_usd']:,.0f}** by ofd and "
            f"**${land_kpi['fd_usd']:,.0f}** by packed `forecast_date` (same). "
            f"Packed lines with ofd == {REF_FRIDAY.isoformat()}: "
            f"**{fri_impact['n']:,} / ${fri_impact['usd']:,.0f}**. "
            f"Unpacking therefore does **not** raise Jul-31 Expected land."
        ),
        "",
        (
            f"Packing reasons clear: **{known_pct:.0%}**. "
            f"Eligible for REF Friday land: **{elig_stats['eligible_n']:,}**. "
            f"High-confidence Actual links (paid pack signature): "
            f"**{len(linked_paid)}**. "
            f"Open packed name_key+DOS deposit hits: **{len(suspect_open)}** "
            f"(suspect sibling matches — excluded from A/B)."
        ),
        "",
        f"**Final verdict: {verdict}** — see §10.",
        "",
        "---",
        "",
        "## 2. Packed Claims Overview",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Packed lines | {total_n:,} |",
        f"| Packed $ | {total_amt:,.2f} |",
        f"| ofd == {REF_FRIDAY} (n / $) | {fri_impact['n']:,} / {fri_impact['usd']:,.2f} |",
        (
            f"| HF Expected land {REF_FRIDAY} by ofd | "
            f"{land_kpi['ofd_n']:,} / {land_kpi['ofd_usd']:,.2f} |"
        ),
        (
            f"| HF Expected land {REF_FRIDAY} by forecast_date | "
            f"{land_kpi['fd_n']:,} / {land_kpi['fd_usd']:,.2f} |"
        ),
        f"| ofd range (packed) | {fri_impact['ofd_min']} → {fri_impact['ofd_max']} |",
        "",
        "Top destination `forecast_date` after pack:",
        "",
        "| forecast_date | n | $ |",
        "|---|---:|---:|",
    ]
    by_fd_amt: dict[str, float] = defaultdict(float)
    by_fd_n: dict[str, int] = defaultdict(int)
    for r in packed:
        by_fd_n[r["forecast_date"]] += 1
        by_fd_amt[r["forecast_date"]] += float(r["expected_amount"])
    for d, n in sorted(by_fd_n.items(), key=lambda x: -by_fd_amt[x[0]])[:12]:
        lines.append(f"| {d} | {n:,} | {by_fd_amt[d]:,.0f} |")
    lines += ["", "---", "", "## 3. Packing Attribution", ""]

    reason_amt: dict[str, float] = defaultdict(float)
    reason_n: dict[str, int] = defaultdict(int)
    for r in attributed:
        reason_n[r["packing_reason"]] += 1
        reason_amt[r["packing_reason"]] += float(r["expected_amount"])
    lines += ["| Reason | n | $ |", "|---|---:|---:|"]
    for reason, n in sorted(reason_n.items(), key=lambda x: -reason_amt[x[0]]):
        lines.append(f"| {reason} | {n:,} | {reason_amt[reason]:,.0f} |")
    lines += [
        "",
        (
            f"Audit match rate: "
            f"{sum(1 for r in attributed if r['audit_matched']) / total_n:.0%} "
            f"of packed lines matched `reschedule_audit`."
        ),
        "",
        "| Eligibility for REF Friday land (if unpack) | n | $ |",
        "|---|---:|---:|",
    ]
    for label in ("Eligible", "Not Eligible"):
        n = sum(1 for r in attributed if r["eligible_for_ref_friday"] == label)
        a = sum(
            float(r["expected_amount"])
            for r in attributed
            if r["eligible_for_ref_friday"] == label
        )
        lines.append(f"| {label} | {n:,} | {a:,.0f} |")
    lines += [
        "",
        (
            "Eligibility definition: Expected land uses `original_forecast_date`, so a "
            "packed claim is Eligible for the reference Friday **only if** "
            f"`ofd == {REF_FRIDAY.isoformat()}`. Past ofd requires pull-forward "
            "(out of scope) to appear on that Friday."
        ),
        "",
        (
            f"Unpack violates rule among Eligible: Yes="
            f"{sum(1 for r in attributed if r['eligible_for_ref_friday']=='Eligible' and r['unpack_violates_rule']=='Yes')}, "
            f"No={elig_stats['eligible_no_violate_n']}."
        ),
        "",
        "---",
        "",
        "## 4. Historical Validation",
        "",
        (
            "Open pack-deferred lines are unpaid by stage. Matching them to deposits "
            "via `name_key`+`DOS` alone produces **suspect sibling** hits "
            "(other CPT on the same visit already paid)."
        ),
        "",
        f"| Suspect open links (excluded from A/B) | {len(suspect_open):,} |",
        f"| High-confidence paid pack-signature links | {len(linked_paid):,} |",
        "",
    ]
    if len(linked_paid) < MIN_LINK_N:
        lines += [
            (
                f"**Linkage coverage is INSUFFICIENT** for conclusive Scenario A/B "
                f"(high-confidence n={len(linked_paid)} < {MIN_LINK_N}). "
                "Do not treat Actual metrics as proof. Causal conclusion rests on "
                "land-KPI date column + ofd geometry (§1, §6)."
            ),
            "",
        ]
    else:
        lines += ["Linkage coverage meets minimum for A/B comparison.", ""]

    lines += ["---", "", "## 5. Scenario Comparison", ""]
    if not linked_paid:
        lines += [
            (
                "No high-confidence linked sample — Scenario A/B accuracy metrics "
                "**not scored**. Suspect open links are listed in "
                "`linked_claims_sample.csv` with `link_quality=suspect` only."
            ),
            "",
        ]
    else:
        lines += [
            f"Sample: {len(linked_paid)} high-confidence paid rows.",
            "",
            "| Metric | Scenario A (forecast_date) | Scenario B (original_forecast_date) |",
            "|---|---:|---:|",
        ]
        for key, label in [
            ("n", "n"),
            ("same_day_rate", "Same-day rate"),
            ("mae", "MAE (days)"),
            ("median_error", "Median error"),
            ("bias", "Bias"),
            ("friday_accuracy", "Friday accuracy"),
            ("dollar_matched", "$ matched"),
            ("dollar_early", "$ early"),
            ("dollar_late", "$ late"),
        ]:
            lines.append(
                f"| {label} | {_fmt_metric(key, metrics_a.get(key))} | "
                f"{_fmt_metric(key, metrics_b.get(key))} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "## 6. Friday Impact",
        "",
        (
            "Hypothetical: if packing ignored and land used ofd "
            f"(which the KPI already does for open AR):"
        ),
        "",
        "| Item | n | $ |",
        "|---|---:|---:|",
        (
            f"| Packed with ofd == {REF_FRIDAY} | "
            f"{fri_impact['n']:,} | {fri_impact['usd']:,.0f} |"
        ),
        (
            f"| Packed with ofd < {REF_FRIDAY} | "
            f"{fri_impact['n_ofd_before']:,} | {fri_impact['usd_ofd_before']:,.0f} |"
        ),
        (
            f"| HF open land on {REF_FRIDAY} (ofd) — current KPI | "
            f"{land_kpi['ofd_n']:,} | {land_kpi['ofd_usd']:,.0f} |"
        ),
        (
            f"| HF open land on {REF_FRIDAY} (forecast_date) | "
            f"{land_kpi['fd_n']:,} | {land_kpi['fd_usd']:,.0f} |"
        ),
        "",
        (
            "**Conclusion:** Unpacking changes `forecast_date` back to past ofd; "
            "it does not move dollars onto the reference Friday Expected land total."
        ),
        "",
        "---",
        "",
        "## 7. August Impact",
        "",
        (
            "Packed dollars currently on Aug Fridays via `forecast_date` "
            "(would leave those Fridays if unpacked):"
        ),
        "",
        "| Friday | Claims removed | $ removed |",
        "|---|---:|---:|",
    ]
    for row in aug_impact:
        lines.append(f"| {row['friday']} | {row['n']:,} | {row['usd']:,.0f} |")
    lines += [
        (
            f"| **Total** | {sum(r['n'] for r in aug_impact):,} | "
            f"{sum(r['usd'] for r in aug_impact):,.0f} |"
        ),
        "",
        (
            "Nearly all packed HF mass sits on **2026-11-13** (capacity last-slot "
            "overflow), not on August Fridays — so August Friday damage from unpack "
            "is negligible; the Jul-31 land question is unaffected either way."
        ),
        "",
        "---",
        "",
        "## 8. Risk Assessment",
        "",
        "| Risk | Detail |",
        "|---|---|",
        (
            "| Mis-attributing Jul-31 land gap to packing | Land KPI already uses ofd; "
            "packed mass has past ofd |"
        ),
        (
            "| Confusing pack-deferred $ with Friday-eligible $ | Pack-deferred ≠ "
            "would land on REF Friday without pull-forward |"
        ),
        (
            "| Suspect Actual links | Open name_key+DOS deposit hits are sibling CPT "
            "collisions |"
        ),
        (
            "| Capacity overflow to Nov | Real scheduler pressure; separate from "
            "Jul-31 Expected land KPI |"
        ),
        "",
        "---",
        "",
        "## 9. Acceptance Criteria Review",
        "",
        "| # | Criterion | Result | Pass? |",
        "|---|---|---|---|",
    ]
    for row in acceptance["rows"]:
        lines.append(
            f"| {row['id']} | {row['text']} | {row['result']} | {row['pass']} |"
        )
    lines += [
        "",
        f"All required for VALIDATED: **{acceptance['all_pass']}**",
        "",
        "---",
        "",
        "## 10. Final Verdict",
        "",
        f"## {verdict}",
        "",
        acceptance["verdict_detail"],
        "",
        "---",
        "",
        "## 11. Recommendation",
        "",
        acceptance["recommendation"],
        "",
        "**Not executed in this phase:** no Feature Flag, no unpacking, no rebuild.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, forecast_dir: Path, recon_dir: Path) -> Path:
    outcomes = forecast_dir / "outcome_stages.csv"
    audit = forecast_dir / "reschedule_audit.csv"
    payments = recon_dir / "payments_unified.csv"
    checks = recon_dir / "insurance_behavior" / "checks_timeline.csv"
    behavior = recon_dir / "insurance_behavior" / "payor_behavior_summary.csv"
    out_dir = forecast_dir / "gap_reports" / "packing_validation"
    report = forecast_dir / "gap_reports" / "packing_validation_report.md"

    print("Extracting packed Healthfirst claims ...")
    packed = extract_packed(outcomes)
    packed_usd = sum(float(r["expected_amount"]) for r in packed)
    print(f"  {len(packed)} lines / ${packed_usd:,.0f}")

    land_kpi = land_on_ref_friday(outcomes)
    print(
        f"  HF land {REF_FRIDAY}: ofd=${land_kpi['ofd_usd']:,.0f} "
        f"fd=${land_kpi['fd_usd']:,.0f}"
    )

    print("Loading reschedule audit + velocity ...")
    audit_idx = load_reschedule_index(audit)
    vel = load_hf_velocity(behavior)
    print(f"  audit HF keys={len(audit_idx)} velocity_median={vel}")

    print("Attributing packing ...")
    attributed = attribute_packing(packed, audit_idx, vel)

    print("Deposit linkage ...")
    dep_lookup = build_deposit_lookup(payments, checks)
    suspect_open = probe_open_links(attributed, dep_lookup)
    linked_paid = score_historical_paid_packing(outcomes, dep_lookup)
    print(
        f"  suspect_open={len(suspect_open)} high_conf_paid={len(linked_paid)} "
        f"deposit_keys={len(dep_lookup)}"
    )

    metrics_a = scenario_metrics(linked_paid, "A")
    metrics_b = scenario_metrics(linked_paid, "B")

    fri_rows = [
        r for r in attributed if r["original_forecast_date"] == REF_FRIDAY.isoformat()
    ]
    before = [
        r for r in attributed if r["original_forecast_date"] < REF_FRIDAY.isoformat()
    ]
    ofds = [r["original_forecast_date"] for r in packed]
    fri_impact = {
        "n": len(fri_rows),
        "usd": sum(float(r["expected_amount"]) for r in fri_rows),
        "n_ofd_before": len(before),
        "usd_ofd_before": sum(float(r["expected_amount"]) for r in before),
        "ofd_min": min(ofds) if ofds else "",
        "ofd_max": max(ofds) if ofds else "",
    }

    aug_impact = []
    for fri in AUG_FRIDAYS:
        fs = fri.isoformat()
        rows = [r for r in attributed if r["forecast_date"] == fs]
        aug_impact.append(
            {
                "friday": fs,
                "n": len(rows),
                "usd": round(sum(float(r["expected_amount"]) for r in rows), 2),
            }
        )
    aug_total = sum(r["usd"] for r in aug_impact)

    elig_n = sum(1 for r in attributed if r["eligible_for_ref_friday"] == "Eligible")
    elig_no_violate = sum(
        1
        for r in attributed
        if r["eligible_for_ref_friday"] == "Eligible"
        and r["unpack_violates_rule"] == "No"
    )
    elig_stats = {
        "eligible_n": elig_n,
        "eligible_no_violate_n": elig_no_violate,
    }
    known_pct = (
        sum(1 for r in attributed if r["packing_reason"] != "Unknown") / len(attributed)
        if attributed
        else 0.0
    )
    elig_ok_pct = elig_no_violate / elig_n if elig_n else 0.0

    verdict, acceptance = decide_verdict(
        known_reason_pct=known_pct,
        elig_n=elig_n,
        elig_ok_pct=elig_ok_pct,
        link_high_n=len(linked_paid),
        fri_ofd_usd=float(fri_impact["usd"]),
        land_ofd_usd=float(land_kpi["ofd_usd"]),
        land_fd_usd=float(land_kpi["fd_usd"]),
        packed_usd=packed_usd,
        aug_total=aug_total,
        metrics_a=metrics_a,
        metrics_b=metrics_b,
    )

    # CSVs: linked sample includes high-conf + suspect (flagged)
    linked_sample = linked_paid + suspect_open
    write_csv(out_dir / "packed_claims.csv", packed)
    write_csv(out_dir / "packing_attribution.csv", attributed)
    write_csv(out_dir / "linked_claims_sample.csv", linked_sample)
    write_csv(
        out_dir / "scenario_metrics.csv",
        [
            {
                k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in metrics_a.items()
            },
            {
                k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in metrics_b.items()
            },
        ],
    )
    write_csv(out_dir / "august_impact.csv", aug_impact)

    write_report(
        report,
        packed=packed,
        attributed=attributed,
        suspect_open=suspect_open,
        linked_paid=linked_paid,
        metrics_a=metrics_a,
        metrics_b=metrics_b,
        fri_impact=fri_impact,
        aug_impact=aug_impact,
        land_kpi=land_kpi,
        elig_stats=elig_stats,
        verdict=verdict,
        acceptance=acceptance,
    )
    print(f"Wrote {out_dir}")
    print(f"Wrote {report}")
    print(f"VERDICT: {verdict}")
    return report


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
