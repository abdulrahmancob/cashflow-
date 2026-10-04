"""Assemble finance-page payloads from aggregate rows. No database access."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from cashflow_forecast.deposit_capacity import age_factor

# Upper bound of each overdue_grid bucket. age_factor is a step curve on these days,
# so every day inside a bucket shares this factor.
_BUCKET_DAY = {
    "0_14": 14,
    "15_30": 30,
    "31_60": 60,
    "61_90": 90,
    "91_180": 180,
    "180_plus": 10_000,
}
_AGING_KEY = {
    "0_14": "b0_30",
    "15_30": "b0_30",
    "31_60": "b31_60",
    "61_90": "b61_90",
    "91_180": "b90_plus",
    "180_plus": "b90_plus",
}
_TOP_N = 6
_LOSS_STAGES = ("denied", "rejected", "zero_pay")


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _round2(value: float) -> float:
    return round(value, 2)


def _name(value: Any, fallback: str = "(blank)") -> str:
    text = str(value or "").strip()
    return text or fallback


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _delta_pct(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return round(100.0 * (current - prior) / abs(prior), 1)


def _factor(bucket: str) -> float:
    return age_factor(_BUCKET_DAY.get(bucket, 10_000))


def empty_overdue_analysis() -> dict[str, Any]:
    return {
        "by_month": [],
        "aging": [],
        "recovery": [],
        "pareto": {"rows": [], "top3_share": 0.0, "top5_share": 0.0},
        "days_to_pay": [],
        "by_clinic": [],
        "chase_list": [],
        "trend": [],
        "kpi": {
            "overdue_90_plus": 0.0,
            "expected_recovery": 0.0,
            "recovery_pct": 0.0,
            "face": 0.0,
            "overdue_change_4w": None,
            "overdue_change_4w_pct": None,
            "top3_share": 0.0,
        },
    }


def _ins_rollups(grid: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per insurer: face, recovery, aging buckets, day sums, claim counts."""
    out: dict[str, dict[str, float]] = {}
    for row in grid:
        ins = _name(row.get("ins_name"))
        slot = out.setdefault(
            ins,
            {
                "face": 0.0,
                "recovery": 0.0,
                "b0_30": 0.0,
                "b31_60": 0.0,
                "b61_90": 0.0,
                "b90_plus": 0.0,
                "claims": 0.0,
                "days_sum": 0.0,
                "sla_sum": 0.0,
                "sla_n": 0.0,
            },
        )
        amount = _f(row.get("amount"))
        count = _f(row.get("line_count"))
        bucket = str(row.get("age_bucket") or "")
        slot["face"] += amount
        slot["recovery"] += amount * _factor(bucket)
        slot[_AGING_KEY.get(bucket, "b90_plus")] += amount
        slot["claims"] += count
        slot["days_sum"] += _f(row.get("overdue_days_sum"))
        slot["sla_sum"] += _f(row.get("sla_lag_sum"))
        slot["sla_n"] += _f(row.get("sla_lag_n"))
    return out


def _fold_months(grid: list[dict[str, Any]], month_key: str, basis: str) -> list[dict[str, Any]]:
    cells: dict[tuple[str, str], list[float]] = {}
    totals: dict[str, float] = {}
    for row in grid:
        period = str(row.get(month_key) or "").strip()
        if len(period) < 7:
            continue
        ins = _name(row.get("ins_name"))
        amount = _f(row.get("amount"))
        count = _f(row.get("line_count"))
        slot = cells.setdefault((period, ins), [0.0, 0.0])
        slot[0] += amount
        slot[1] += count
        totals[ins] = totals.get(ins, 0.0) + amount
    ranked = sorted(totals, key=lambda name: totals[name], reverse=True)
    keep = set(ranked[:_TOP_N])
    folded: dict[tuple[str, str], list[float]] = {}
    for (period, ins), (amount, count) in cells.items():
        label = ins if ins in keep else "Other"
        slot = folded.setdefault((period, label), [0.0, 0.0])
        slot[0] += amount
        slot[1] += count
    rows = [
        {
            "period": period,
            "basis": basis,
            "ins_name": ins,
            "amount": _round2(amount),
            "count": int(count),
        }
        for (period, ins), (amount, count) in folded.items()
        if amount > 0
    ]
    rows.sort(key=lambda r: (r["period"], -r["amount"]))
    return rows


def _pareto(rollups: dict[str, dict[str, float]]) -> dict[str, Any]:
    ranked = sorted(rollups.items(), key=lambda item: item[1]["face"], reverse=True)
    total = sum(slot["face"] for _, slot in ranked)
    running = 0.0
    rows: list[dict[str, Any]] = []
    for ins, slot in ranked:
        if slot["face"] <= 0:
            continue
        running += slot["face"]
        rows.append(
            {
                "ins_name": ins,
                "amount": _round2(slot["face"]),
                "share_pct": round(100.0 * slot["face"] / total, 1) if total else 0.0,
                "cumulative_pct": round(100.0 * running / total, 1) if total else 0.0,
            }
        )
    def _top(n: int) -> float:
        if not total:
            return 0.0
        return round(100.0 * sum(slot["face"] for _, slot in ranked[:n]) / total, 1)

    return {"rows": rows[:15], "top3_share": _top(3), "top5_share": _top(5)}


def _four_week_change(trend: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    points = [row for row in trend if _day(row.get("as_of"))]
    points.sort(key=lambda row: _day(row.get("as_of")) or date.min)
    if len(points) < 2:
        return None, None
    latest = points[-1]
    latest_day = _day(latest.get("as_of"))
    if latest_day is None:
        return None, None
    cutoff = latest_day - timedelta(days=28)
    prior = None
    for row in points:
        day = _day(row.get("as_of"))
        if day is not None and day <= cutoff:
            prior = row
    if prior is None:
        return None, None
    delta = _round2(_f(latest.get("overdue")) - _f(prior.get("overdue")))
    base = _f(prior.get("overdue"))
    pct = round(100.0 * delta / base, 1) if base else None
    return delta, pct


def build_overdue_analysis(
    grid: list[dict[str, Any]] | None,
    trend: list[dict[str, Any]] | None,
    claims: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Month x insurance, aging, recovery, Pareto, days to pay, clinics, chase list."""
    rows = list(grid or [])
    rollups = _ins_rollups(rows)
    pareto = _pareto(rollups)
    face = sum(slot["face"] for slot in rollups.values())
    recovery = sum(slot["recovery"] for slot in rollups.values())
    overdue_90 = sum(slot["b90_plus"] for slot in rollups.values())
    change, change_pct = _four_week_change(list(trend or []))

    aging = []
    for ins, slot in sorted(rollups.items(), key=lambda item: item[1]["face"], reverse=True):
        if slot["face"] <= 0:
            continue
        aging.append(
            {
                "ins_name": ins,
                "b0_30": _round2(slot["b0_30"]),
                "b31_60": _round2(slot["b31_60"]),
                "b61_90": _round2(slot["b61_90"]),
                "b90_plus": _round2(slot["b90_plus"]),
                "total": _round2(slot["face"]),
            }
        )
    if len(aging) > _TOP_N:
        rest = aging[_TOP_N:]
        aging = aging[:_TOP_N]
        other = {
            "ins_name": "Other",
            "b0_30": _round2(sum(r["b0_30"] for r in rest)),
            "b31_60": _round2(sum(r["b31_60"] for r in rest)),
            "b61_90": _round2(sum(r["b61_90"] for r in rest)),
            "b90_plus": _round2(sum(r["b90_plus"] for r in rest)),
            "total": _round2(sum(r["total"] for r in rest)),
        }
        if other["total"] > 0:
            aging.append(other)

    days_rows = []
    for ins, slot in sorted(rollups.items(), key=lambda item: item[1]["face"], reverse=True):
        if slot["face"] <= 0:
            continue
        claims_n = slot["claims"]
        days_rows.append(
            {
                "ins_name": ins,
                "amount": _round2(slot["face"]),
                "claims": int(claims_n),
                "avg_overdue_days": round(slot["days_sum"] / claims_n, 1) if claims_n else 0.0,
                "avg_sla_lag_days": round(slot["sla_sum"] / slot["sla_n"], 1) if slot["sla_n"] else None,
                "share_90_plus": round(100.0 * slot["b90_plus"] / slot["face"], 1) if slot["face"] else 0.0,
                "expected_recovery": _round2(slot["recovery"]),
            }
        )

    clinics: dict[str, dict[str, float]] = {}
    for row in rows:
        name = _name(row.get("facility_name"))
        slot = clinics.setdefault(name, {"amount": 0.0, "count": 0.0, "days": 0.0})
        slot["amount"] += _f(row.get("amount"))
        slot["count"] += _f(row.get("line_count"))
        slot["days"] += _f(row.get("overdue_days_sum"))
    by_clinic = [
        {
            "facility_name": name,
            "amount": _round2(slot["amount"]),
            "count": int(slot["count"]),
            "avg_overdue_days": round(slot["days"] / slot["count"], 1) if slot["count"] else 0.0,
        }
        for name, slot in sorted(clinics.items(), key=lambda item: item[1]["amount"], reverse=True)
        if slot["amount"] > 0
    ][:12]

    chase = []
    for claim in list(claims or []):
        amount = _f(claim.get("expected_amount"))
        days = int(_f(claim.get("overdue_days")))
        weighted = amount * age_factor(days)
        chase.append({**claim, "recovery_amount": _round2(weighted), "_rank": weighted})
    chase.sort(key=lambda row: row["_rank"], reverse=True)
    chase_list = []
    for row in chase[:15]:
        item = dict(row)
        item.pop("_rank", None)
        chase_list.append(item)

    trend_out = []
    for row in sorted(list(trend or []), key=lambda item: str(item.get("as_of") or "")):
        day = _day(row.get("as_of"))
        if day is None:
            continue
        trend_out.append(
            {
                "as_of": day.isoformat(),
                "overdue": _round2(_f(row.get("overdue"))),
                "on_track": _round2(_f(row.get("on_track"))),
                "overdue_90_plus": _round2(_f(row.get("overdue_90_plus"))),
            }
        )

    payload = empty_overdue_analysis()
    payload.update(
        {
            "by_month": _fold_months(rows, "dos_month", "dos") + _fold_months(rows, "land_month", "land"),
            "aging": aging,
            "recovery": [
                {
                    "ins_name": ins,
                    "face": _round2(slot["face"]),
                    "expected_recovery": _round2(slot["recovery"]),
                }
                for ins, slot in sorted(rollups.items(), key=lambda item: item[1]["face"], reverse=True)
                if slot["face"] > 0
            ][:12],
            "pareto": pareto,
            "days_to_pay": days_rows[:15],
            "by_clinic": by_clinic,
            "chase_list": chase_list,
            "trend": trend_out,
            "kpi": {
                "overdue_90_plus": _round2(overdue_90),
                "expected_recovery": _round2(recovery),
                "recovery_pct": round(100.0 * recovery / face, 1) if face else 0.0,
                "face": _round2(face),
                "overdue_change_4w": change,
                "overdue_change_4w_pct": change_pct,
                "top3_share": pareto["top3_share"],
            },
        }
    )
    return payload


def _index_amount(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        period = str(row.get("period") or "")[:10]
        if period:
            out[period] = out.get(period, 0.0) + _f(row.get("amount"))
    return out


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    if start.month == 12:
        nxt = date(start.year + 1, 1, 1)
    else:
        nxt = date(start.year, start.month + 1, 1)
    return start, nxt - timedelta(days=1)


def previous_month(day: date) -> tuple[date, date]:
    start, _end = month_bounds(day)
    return month_bounds(start - timedelta(days=1))


def build_burnup(
    daily: list[dict[str, Any]],
    actual: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    settled: date | None,
) -> list[dict[str, Any]]:
    """Cumulative tracker cash vs cumulative forecast for one month."""
    proj = _index_amount(daily)
    act = _index_amount(actual)
    rows: list[dict[str, Any]] = []
    cum_f = 0.0
    cum_a = 0.0
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        cum_f += proj.get(key, 0.0)
        point: dict[str, Any] = {"period": key, "forecast_cum": _round2(cum_f)}
        landed = settled is not None and cursor <= settled
        if landed:
            cum_a += act.get(key, 0.0)
            point["actual_cum"] = _round2(cum_a)
            point["forecast_to_date"] = _round2(cum_f)
            point["forecast_rest"] = _round2(cum_f) if settled is not None and cursor == settled else None
        else:
            point["actual_cum"] = None
            point["forecast_to_date"] = None
            point["forecast_rest"] = _round2(cum_f)
        rows.append(point)
        cursor += timedelta(days=1)
    return rows


def build_forward_weeks(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {}
    for row in rows or []:
        week = str(row.get("week") or "")[:10]
        if not week:
            continue
        slot = buckets.setdefault(week, {"on_track": 0.0, "overdue_recovery": 0.0})
        amount = _f(row.get("amount"))
        if str(row.get("outcome_stage") or "") == "on_track":
            slot["on_track"] += amount
        else:
            slot["overdue_recovery"] += amount * _factor(str(row.get("age_bucket") or ""))
    return [
        {
            "week": week,
            "on_track": _round2(slot["on_track"]),
            "overdue_recovery": _round2(slot["overdue_recovery"]),
        }
        for week, slot in sorted(buckets.items())
    ]


def _sum_between(series: dict[str, float], start: date, end: date) -> float:
    total = 0.0
    for key, amount in series.items():
        day = _day(key)
        if day is not None and start <= day <= end:
            total += amount
    return total


def build_cash_kpis(
    *,
    month_daily: list[dict[str, Any]],
    month_actual: list[dict[str, Any]],
    forward_daily: list[dict[str, Any]] | None = None,
    settled: date | None,
    today: date,
    accuracy_rows: list[dict[str, Any]] | None,
    forward_weeks: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    start, _end = month_bounds(today)
    through = settled if settled is not None and settled >= start else None
    if through is not None and through > today:
        through = today
    proj = _index_amount(month_daily)
    act = _index_amount(month_actual)
    if through is None or through < start:
        cash_mtd = 0.0
        forecast_mtd = 0.0
    else:
        cash_mtd = _sum_between(act, start, through)
        forecast_mtd = _sum_between(proj, start, through)
    pace = round(100.0 * cash_mtd / forecast_mtd, 1) if forecast_mtd else None
    next_start = (settled + timedelta(days=1)) if settled is not None else today
    ahead = _index_amount(list(forward_daily if forward_daily is not None else month_daily))
    next_10 = _sum_between(ahead, next_start, next_start + timedelta(days=9))
    next_30 = _sum_between(ahead, next_start, next_start + timedelta(days=29))
    weeks = list(forward_weeks or [])
    forward_total = sum(_f(row.get("on_track")) + _f(row.get("overdue_recovery")) for row in weeks)
    if next_30 <= 0 and forward_total > 0:
        next_30 = forward_total
    errs = [
        abs(_f(row.get("error_pct")))
        for row in (accuracy_rows or [])
        if row.get("error_pct") is not None
    ]
    sample = errs[:30]
    mae = round(sum(sample) / len(sample), 1) if sample else None
    return {
        "cash_mtd": _round2(cash_mtd),
        "forecast_mtd": _round2(forecast_mtd),
        "pace_pct": pace,
        "next_10d": _round2(next_10),
        "next_30d": _round2(next_30),
        "mae_30d": mae,
        "through": through.isoformat() if through else None,
    }


def accuracy_days(rows: list[dict[str, Any]] | None, limit: int = 30) -> list[dict[str, Any]]:
    parsed = []
    for row in rows or []:
        day = _day(row.get("bank_date"))
        if day is None or row.get("error_pct") is None:
            continue
        parsed.append({"bank_date": day.isoformat(), "error_pct": round(_f(row.get("error_pct")), 2)})
    parsed.sort(key=lambda row: row["bank_date"])
    return parsed[-limit:]


def payer_grade(collection_pct: float | None, avg_days: float | None) -> str:
    if collection_pct is None and avg_days is None:
        return ""
    days = 999.0 if avg_days is None else avg_days
    col = -1.0 if collection_pct is None else collection_pct
    if col >= 90 and days <= 30:
        return "A"
    if col >= 75 and days <= 45:
        return "B"
    if col >= 60 and days <= 75:
        return "C"
    if col < 0 and days <= 30:
        return "B"
    return "D"


def _month_last(period: str) -> date | None:
    day = _day(f"{period}-01")
    if day is None:
        return None
    return month_bounds(day)[1]


def build_collection_rate(
    rows: list[dict[str, Any]] | None,
    *,
    today: date,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {}
    for row in rows or []:
        period = str(row.get("period") or "")[:7]
        if len(period) < 7:
            continue
        slot = buckets.setdefault(period, {"paid": 0.0, "total": 0.0})
        amount = _f(row.get("amount"))
        slot["total"] += amount
        if str(row.get("outcome_stage") or "") == "paid":
            slot["paid"] += amount
    fresh_after = today - timedelta(days=45)
    out = []
    for period in sorted(buckets):
        slot = buckets[period]
        last = _month_last(period)
        total = slot["total"]
        rate = round(100.0 * slot["paid"] / total, 1) if total else None
        out.append(
            {
                "period": period,
                "paid": _round2(slot["paid"]),
                "expected": _round2(total),
                "rate_pct": rate,
                "maturing": bool(last is not None and last >= fresh_after),
            }
        )
    return out[-12:]


def _stage_leakage(rows: list[dict[str, Any]], period: str) -> float:
    total = 0.0
    for row in rows:
        if str(row.get("period") or "")[:7] != period:
            continue
        if str(row.get("outcome_stage") or "") in _LOSS_STAGES:
            total += _f(row.get("amount"))
    return total


def leakage_waterfall(stages: list[dict[str, Any]] | None, doc_risk: float) -> list[dict[str, Any]]:
    by_stage = {str(row.get("outcome_stage") or ""): _f(row.get("amount")) for row in (stages or [])}
    expected = sum(by_stage.values())
    denied = by_stage.get("denied", 0.0)
    rejected = by_stage.get("rejected", 0.0)
    zero = by_stage.get("zero_pay", 0.0)
    doc = max(_f(doc_risk), 0.0)
    collectible = expected - denied - rejected - zero - doc
    return [
        {"label": "Expected", "kind": "total", "amount": _round2(expected)},
        {"label": "Denied", "kind": "loss", "amount": _round2(denied)},
        {"label": "Rejected", "kind": "loss", "amount": _round2(rejected)},
        {"label": "Zero pay", "kind": "loss", "amount": _round2(zero)},
        {"label": "Doc risk", "kind": "loss", "amount": _round2(doc)},
        {"label": "Collectible", "kind": "result", "amount": _round2(collectible)},
    ]


def _mae(rows: list[dict[str, Any]], start: int, end: int) -> float | None:
    errs = [
        abs(_f(row.get("error_pct")))
        for row in rows
        if row.get("error_pct") is not None
    ]
    sample = errs[start:end]
    if not sample:
        return None
    return round(sum(sample) / len(sample), 1)


def _tile(
    key: str,
    label: str,
    value: float | None,
    unit: str,
    delta: float | None,
    good_when: str,
    hint: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": None if value is None else _round2(value) if unit != "pct" else round(value, 1),
        "unit": unit,
        "delta_pct": delta,
        "good_when": good_when,
        "hint": hint,
    }


def build_payer_scorecard(
    stage_rows: list[dict[str, Any]] | None,
    grid: list[dict[str, Any]] | None,
    tracker_rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, float]] = {}
    for row in stage_rows or []:
        ins = _name(row.get("ins_name"))
        slot = stats.setdefault(ins, {"expected": 0.0, "paid": 0.0, "denied": 0.0})
        amount = _f(row.get("amount"))
        slot["expected"] += amount
        stage = str(row.get("outcome_stage") or "")
        if stage == "paid":
            slot["paid"] += amount
        if stage == "denied":
            slot["denied"] += amount
    rollups = _ins_rollups(list(grid or []))
    cash: dict[str, float] = {}
    for row in tracker_rows or []:
        ins = _name(row.get("ins_name"))
        cash[ins] = cash.get(ins, 0.0) + _f(row.get("amount") if "amount" in row else row.get("paid_amount"))
    total_cash = sum(cash.values())
    names = set(stats) | set(rollups) | set(cash)
    scored = []
    for ins in names:
        slot = stats.get(ins, {"expected": 0.0, "paid": 0.0, "denied": 0.0})
        late = rollups.get(ins)
        expected = slot["expected"]
        collection = round(100.0 * slot["paid"] / expected, 1) if expected else None
        avg_days = None
        overdue = 0.0
        share_90 = 0.0
        if late and late["claims"]:
            avg_days = round(late["days_sum"] / late["claims"], 1)
            overdue = late["face"]
            share_90 = round(100.0 * late["b90_plus"] / late["face"], 1) if late["face"] else 0.0
        cash_amt = cash.get(ins, 0.0)
        scored.append(
            {
                "ins_name": ins,
                "cash_90d": _round2(cash_amt),
                "revenue_share": round(100.0 * cash_amt / total_cash, 1) if total_cash else 0.0,
                "collection_rate": collection,
                "avg_days": avg_days,
                "overdue": _round2(overdue),
                "share_90_plus": share_90,
                "denied": _round2(slot["denied"]),
                "grade": payer_grade(collection, avg_days),
            }
        )
    scored.sort(key=lambda row: (row["cash_90d"], row["overdue"]), reverse=True)
    return [row for row in scored if row["cash_90d"] or row["overdue"] or row["denied"]][:20]


def _trend_prior(trend: list[dict[str, Any]], today: date) -> dict[str, Any] | None:
    cutoff = today - timedelta(days=28)
    prior = None
    for row in sorted(trend, key=lambda item: str(item.get("as_of") or "")):
        day = _day(row.get("as_of"))
        if day is not None and day <= cutoff:
            prior = row
    return prior


def build_exec_scorecard(
    *,
    today: date,
    stages: list[dict[str, Any]] | None,
    doc_risk: float,
    collection_rows: list[dict[str, Any]] | None,
    grid: list[dict[str, Any]] | None,
    trend: list[dict[str, Any]] | None,
    stage_by_ins: list[dict[str, Any]] | None,
    tracker_by_ins: list[dict[str, Any]] | None,
    open_ar: dict[str, Any] | None,
    cash_last_month: float,
    cash_prior_month: float,
    cash_90: float,
    cash_prior_90: float,
    accuracy_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """CEO scorecard. Deltas compare a prior window; missing history stays blank."""
    ar = open_ar or {}
    on_track = _f(ar.get("on_track_amount"))
    overdue = _f(ar.get("overdue_amount"))
    open_total = on_track + overdue
    overdue_pct = round(100.0 * overdue / open_total, 1) if open_total else None
    dso = round(open_total / (cash_90 / 90.0), 1) if cash_90 else None

    points = list(trend or [])
    prior = _trend_prior(points, today)
    prior_open = None
    prior_overdue_pct = None
    if prior is not None:
        prior_open = _f(prior.get("on_track")) + _f(prior.get("overdue"))
        prior_overdue_pct = (
            round(100.0 * _f(prior.get("overdue")) / prior_open, 1) if prior_open else None
        )
    dso_prior = round(prior_open / (cash_prior_90 / 90.0), 1) if prior_open and cash_prior_90 else None

    rates = build_collection_rate(collection_rows, today=today)
    matured = [row for row in rates if not row["maturing"] and row["rate_pct"] is not None]
    latest_rate = matured[-1] if matured else None
    prior_rate = matured[-2] if len(matured) >= 2 else None
    collection_now = latest_rate["rate_pct"] if latest_rate else None
    collection_prev = prior_rate["rate_pct"] if prior_rate else None

    last_start, last_end = previous_month(today)
    prev_start, _prev_end = previous_month(last_start)
    last_key = last_end.strftime("%Y-%m")
    prev_key = prev_start.strftime("%Y-%m")
    raw_collection = list(collection_rows or [])
    leakage_last = _stage_leakage(raw_collection, last_key)
    leakage_prior = _stage_leakage(raw_collection, prev_key)

    acc = list(accuracy_rows or [])
    mae_now = _mae(acc, 0, 30)
    mae_prev = _mae(acc, 30, 60)

    rollups = _ins_rollups(list(grid or []))
    face = sum(slot["face"] for slot in rollups.values())
    recovered = sum(slot["recovery"] for slot in rollups.values())
    pareto = _pareto(rollups)
    change, change_pct = _four_week_change(points)
    worst_name = ""
    worst_90 = 0.0
    for ins, slot in rollups.items():
        if slot["b90_plus"] > worst_90:
            worst_name = ins
            worst_90 = slot["b90_plus"]

    tiles = [
        _tile(
            "cash",
            "Cash last month",
            cash_last_month,
            "money",
            _delta_pct(cash_last_month, cash_prior_month),
            "up",
            f"Tracker {last_key} vs {prev_key}. Not split by clinic.",
        ),
        _tile(
            "collection",
            "Collection rate",
            collection_now,
            "pct",
            _delta_pct(collection_now, collection_prev),
            "up",
            (
                f"Paid / expected for service month {latest_rate['period']}."
                if latest_rate
                else "No matured service month yet."
            ),
        ),
        _tile(
            "dso",
            "DSO",
            dso,
            "days",
            _delta_pct(dso, dso_prior),
            "down",
            "Open AR / average daily tracker cash over 90 days.",
        ),
        _tile(
            "overdue_pct",
            "Overdue % of AR",
            overdue_pct,
            "pct",
            _delta_pct(overdue_pct, prior_overdue_pct),
            "down",
            "Vs the forecast snapshot from about 4 weeks ago.",
        ),
        _tile(
            "leakage",
            "Leakage",
            leakage_last,
            "money",
            _delta_pct(leakage_last, leakage_prior),
            "down",
            f"Denied + rejected + zero pay for service month {last_key}.",
        ),
        _tile(
            "accuracy",
            "Forecast error",
            mae_now,
            "pct",
            _delta_pct(mae_now, mae_prev),
            "down",
            "Mean absolute day-ahead error, last 30 bank days.",
        ),
    ]
    return {
        "tiles": tiles,
        "collection_rate": rates,
        "leakage": leakage_waterfall(stages, doc_risk),
        "payer_scorecard": build_payer_scorecard(stage_by_ins, grid, tracker_by_ins),
        "trend": build_overdue_analysis([], points, [])["trend"],
        "recovery": build_overdue_analysis(grid, [], [])["recovery"],
        "narrative": {
            "ar_change_4w": change,
            "ar_change_4w_pct": change_pct,
            "collection_rate": collection_now,
            "collection_prior": collection_prev,
            "collection_period": latest_rate["period"] if latest_rate else "",
            "top3_share": pareto["top3_share"],
            "recovery_haircut": _round2(face - recovered),
            "recovery_face": _round2(face),
            "expected_recovery": _round2(recovered),
            "worst_payer": worst_name,
            "worst_payer_90": _round2(worst_90),
        },
    }
