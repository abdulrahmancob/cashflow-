"""Primary billing / cash-flow monthly analysis from Snowflake visit KPIs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg

from cashflow_db.repository import client

_MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

PAID_STATUSES = frozenset({"paid", "partial"})
DENIED_STATUSES = frozenset({"denied", "not paid"})

MONTHLY_SQL = """
SELECT date_trunc('month', kpi.date_of_service)::date AS month,
       count(*)::int AS visits,
       count(*) FILTER (
           WHERE lower(btrim(COALESCE(kpi.status, ''))) IN ('paid', 'partial')
       )::int AS paid_visits,
       count(*) FILTER (
           WHERE lower(btrim(COALESCE(kpi.status, ''))) IN ('denied', 'not paid')
       )::int AS denied_visits,
       COALESCE(SUM(kpi.insurance_payment), 0) AS insurance_payment,
       COALESCE(SUM(kpi.client_payment), 0) AS copay
FROM analytics.snowflake_visit_kpi kpi
WHERE kpi.date_of_service >= %s
  AND kpi.date_of_service < %s
  AND (%s::text IS NULL OR lower(btrim(kpi.clinic)) = lower(btrim(%s)))
GROUP BY 1
"""

_VISIT_WINDOW = """
aged.date_of_service >= %s
  AND aged.date_of_service < %s
  AND (%s::text IS NULL OR lower(btrim(aged.clinic)) = lower(btrim(%s)))
"""

AGING_SQL = f"""
SELECT date_trunc('month', aged.date_of_service)::date AS visit_month,
       date_trunc('month', aged.collect_date)::date AS collect_month,
       COALESCE(SUM(aged.amount), 0) AS amount
FROM analytics.billing_collect_visit aged
WHERE aged.collect_date IS NOT NULL
  AND {_VISIT_WINDOW}
GROUP BY 1, 2
"""

# Days from date of service to collect_date. Later-year collections stay on
# the visit month. Negative lags are left out of the day stats.
_NONNEG_LAG = """
aged.collect_date IS NOT NULL
  AND aged.collect_date >= aged.date_of_service
"""

CYCLE_SQL = f"""
SELECT date_trunc('month', aged.date_of_service)::date AS visit_month,
       (count(*) FILTER (WHERE {_NONNEG_LAG}))::int AS collected_visits,
       (count(*) FILTER (WHERE aged.collect_date IS NULL))::int AS open_visits,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY (aged.collect_date - aged.date_of_service)::double precision
       ) FILTER (WHERE {_NONNEG_LAG}) AS median_days,
       COALESCE(SUM(aged.collect_date - aged.date_of_service) FILTER (
           WHERE {_NONNEG_LAG}
       ), 0) AS sum_days,
       COALESCE(SUM(aged.amount) FILTER (WHERE {_NONNEG_LAG}), 0) AS sum_amount,
       COALESCE(SUM(
           aged.amount * (aged.collect_date - aged.date_of_service)
       ) FILTER (WHERE {_NONNEG_LAG}), 0) AS sum_amount_days
FROM analytics.billing_collect_visit aged
WHERE {_VISIT_WINDOW}
GROUP BY GROUPING SETS ((1), ())
"""

# Collected dollars by visit month and lag day. Later-year collections stay
# on the visit month. Only positive amounts move the cash-share curve.
CASH_SHARE_SQL = f"""
SELECT date_trunc('month', aged.date_of_service)::date AS visit_month,
       (aged.collect_date - aged.date_of_service)::int AS lag_days,
       COALESCE(SUM(aged.amount), 0) AS amount
FROM analytics.billing_collect_visit aged
WHERE {_VISIT_WINDOW}
  AND {_NONNEG_LAG}
  AND aged.amount > 0
GROUP BY 1, 2
"""

CLINICS_SQL = """
SELECT DISTINCT btrim(kpi.clinic) AS clinic
FROM analytics.snowflake_visit_kpi kpi
WHERE kpi.date_of_service >= %s
  AND kpi.date_of_service < %s
  AND NULLIF(btrim(kpi.clinic), '') IS NOT NULL
ORDER BY 1
"""


def classify_billing_status(status: str | None) -> str:
    key = (status or "").strip().casefold()
    if key in PAID_STATUSES:
        return "paid"
    if key in DENIED_STATUSES:
        return "denied"
    return "pending"


def year_bounds(year: int) -> tuple[date, date]:
    return date(int(year), 1, 1), date(int(year) + 1, 1, 1)


def empty_month_metrics() -> dict[str, Any]:
    return {
        "insurance_payment": 0.0,
        "copay": 0.0,
        "total_payment": 0.0,
        "visits": 0,
        "ave_visit": 0.0,
        "paid_visits": 0,
        "pending_visits": 0,
        "denied_visits": 0,
        "payment_pct": 0.0,
        "act_ave_visit": 0.0,
        "collection": {f"{m:02d}": 0.0 for m in range(1, 13)},
    }


def apply_visit_rates(row: dict[str, Any]) -> dict[str, Any]:
    visits = int(row.get("visits") or 0)
    paid = int(row.get("paid_visits") or 0)
    denied = int(row.get("denied_visits") or 0)
    pending = max(0, visits - paid - denied)
    insurance = float(row.get("insurance_payment") or 0)
    copay = float(row.get("copay") or 0)
    total = insurance + copay
    row["pending_visits"] = pending
    row["insurance_payment"] = round(insurance, 2)
    row["copay"] = round(copay, 2)
    row["total_payment"] = round(total, 2)
    row["ave_visit"] = round(total / visits, 2) if visits else 0.0
    row["payment_pct"] = round(100.0 * paid / visits, 3) if visits else 0.0
    row["act_ave_visit"] = round(total / paid, 2) if paid else 0.0
    return row


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def empty_cycle() -> dict[str, Any]:
    return {
        "collected_visits": 0,
        "open_visits": 0,
        "median_days": None,
        "avg_days": None,
        "weighted_avg_days": None,
    }


def cycle_metrics(
    *,
    collected_visits: int,
    open_visits: int,
    median_days: float | None,
    sum_days: float,
    sum_amount: float,
    sum_amount_days: float,
) -> dict[str, Any]:
    """Average and dollar-weighted days from summed visits. Median is supplied."""
    n = int(collected_visits or 0)
    amount = float(sum_amount or 0)
    median = None if median_days is None or n == 0 else round(float(median_days), 1)
    return {
        "collected_visits": n,
        "open_visits": int(open_visits or 0),
        "median_days": median,
        "avg_days": round(float(sum_days) / n, 1) if n else None,
        "weighted_avg_days": round(float(sum_amount_days) / amount, 1) if amount else None,
    }


def _period_month(value: Any) -> date | None:
    month = _as_month(value)
    if month is not None:
        return month
    if isinstance(value, str) and len(value) >= 10:
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            return None
        return date(parsed.year, parsed.month, 1)
    return None


def attach_cycle(
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
    cycle_rows: list[dict[str, Any]],
) -> None:
    """Stamp visit-month cycle stats. The null visit_month row is the year total."""
    by_month: dict[date, dict[str, Any]] = {}
    year_cycle = empty_cycle()
    for raw in cycle_rows:
        metrics = cycle_metrics(
            collected_visits=_int(raw.get("collected_visits")),
            open_visits=_int(raw.get("open_visits")),
            median_days=_optional_num(raw.get("median_days")),
            sum_days=_num(raw.get("sum_days")),
            sum_amount=_num(raw.get("sum_amount")),
            sum_amount_days=_num(raw.get("sum_amount_days")),
        )
        month = _period_month(raw.get("visit_month"))
        if month is None:
            year_cycle = metrics
        else:
            by_month[month] = metrics
    for row in rows:
        row.update(by_month.get(_period_month(row.get("period_start"))) or empty_cycle())
    totals.update(year_cycle)


_CASH_SHARE_KEYS = (
    ("cash_50_days", 0.50),
    ("cash_80_days", 0.80),
    ("cash_90_days", 0.90),
    ("cash_95_days", 0.95),
)


def empty_cash_share() -> dict[str, int | None]:
    return {key: None for key, _share in _CASH_SHARE_KEYS}


def cash_share_days(buckets: list[tuple[int, float]]) -> dict[str, int | None]:
    """First lag day where cumulative collected dollars reach each share."""
    ordered = sorted(
        (int(days), float(amount))
        for days, amount in buckets
        if float(amount) > 0
    )
    total = sum(amount for _days, amount in ordered)
    out = empty_cash_share()
    if total <= 0:
        return out
    running = 0.0
    next_i = 0
    for days, amount in ordered:
        running += amount
        while next_i < len(_CASH_SHARE_KEYS) and running + 1e-6 >= _CASH_SHARE_KEYS[next_i][1] * total:
            out[_CASH_SHARE_KEYS[next_i][0]] = days
            next_i += 1
        if next_i >= len(_CASH_SHARE_KEYS):
            break
    return out


def attach_cash_share(
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
    share_rows: list[dict[str, Any]],
) -> None:
    """Stamp cash-share days. The year row pools every month's dollars by lag."""
    by_month: dict[date, list[tuple[int, float]]] = {}
    year_buckets: dict[int, float] = {}
    for raw in share_rows:
        amount = _num(raw.get("amount"))
        if amount <= 0:
            continue
        lag = _int(raw.get("lag_days"))
        month = _period_month(raw.get("visit_month"))
        if month is not None:
            by_month.setdefault(month, []).append((lag, amount))
        year_buckets[lag] = year_buckets.get(lag, 0.0) + amount
    for row in rows:
        row.update(cash_share_days(by_month.get(_period_month(row.get("period_start"))) or []))
    totals.update(cash_share_days(list(year_buckets.items())))


def _sum_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out = empty_month_metrics()
    for key in ("insurance_payment", "copay", "total_payment"):
        out[key] = _num(left.get(key)) + _num(right.get(key))
    for key in ("visits", "paid_visits", "pending_visits", "denied_visits"):
        out[key] = _int(left.get(key)) + _int(right.get(key))
    collection = dict(out["collection"])
    for month_key, amount in (left.get("collection") or {}).items():
        collection[str(month_key)] = collection.get(str(month_key), 0.0) + _num(amount)
    for month_key, amount in (right.get("collection") or {}).items():
        collection[str(month_key)] = collection.get(str(month_key), 0.0) + _num(amount)
    out["collection"] = {k: round(_num(v), 2) for k, v in collection.items()}
    return apply_visit_rates(out)


def fill_year_rows(
    year: int,
    monthly: dict[date, dict[str, Any]],
    aging: dict[tuple[date, int], float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    totals = empty_month_metrics()
    for month_num in range(1, 13):
        start = date(year, month_num, 1)
        src = dict(empty_month_metrics())
        src.update(monthly.get(start) or {})
        collection = dict(src.get("collection") or empty_month_metrics()["collection"])
        for collect_month in range(1, 13):
            key = (start, collect_month)
            if key in aging:
                collection[f"{collect_month:02d}"] = round(
                    _num(collection.get(f"{collect_month:02d}")) + _num(aging[key]),
                    2,
                )
        src["collection"] = collection
        src = apply_visit_rates(src)
        row = {
            "period": f"{_MONTH_LABELS[month_num - 1]} {year}",
            "period_start": start.isoformat(),
            **src,
        }
        totals = _sum_metrics(totals, src)
        rows.append(row)
    totals_row = {
        "period": "Total",
        "period_start": None,
        **apply_visit_rates(totals),
    }
    return rows, totals_row


def _as_month(value: Any) -> date | None:
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    return None


def refresh_billing_collect_visit() -> None:
    """Rebuild the stored collection dates. Cannot run inside a transaction."""
    from cashflow_db.config import DATABASE_URL

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.billing_collect_visit"
        )


def list_clinics(conn: psycopg.Connection, year: int) -> list[str]:
    start, end = year_bounds(year)
    rows = client.fetchall(conn, CLINICS_SQL, (start, end))
    return [str(r["clinic"]) for r in rows if r.get("clinic")]


def monthly_analysis(
    conn: psycopg.Connection,
    *,
    year: int,
    clinic: str | None = None,
) -> dict[str, Any]:
    y = int(year)
    if y < 2000 or y > 2100:
        raise ValueError("year out of range")
    clinic_key = (clinic or "").strip() or None
    start, end = year_bounds(y)
    window = (start, end, clinic_key, clinic_key)
    month_rows = client.fetchall(conn, MONTHLY_SQL, window)
    aging_rows = client.fetchall(conn, AGING_SQL, window)
    cycle_rows = client.fetchall(conn, CYCLE_SQL, window)
    share_rows = client.fetchall(conn, CASH_SHARE_SQL, window)
    monthly: dict[date, dict[str, Any]] = {}
    for raw in month_rows:
        month = _as_month(raw.get("month"))
        if month is None:
            continue
        monthly[month] = {
            "insurance_payment": _num(raw.get("insurance_payment")),
            "copay": _num(raw.get("copay")),
            "visits": _int(raw.get("visits")),
            "paid_visits": _int(raw.get("paid_visits")),
            "denied_visits": _int(raw.get("denied_visits")),
        }
    aging: dict[tuple[date, int], float] = {}
    for raw in aging_rows:
        visit_month = _as_month(raw.get("visit_month"))
        collect_month = _as_month(raw.get("collect_month"))
        if visit_month is None or collect_month is None:
            continue
        if collect_month.year != y:
            continue
        aging[(visit_month, collect_month.month)] = _num(raw.get("amount"))
    rows, totals = fill_year_rows(y, monthly, aging)
    attach_cycle(rows, totals, cycle_rows)
    attach_cash_share(rows, totals, share_rows)
    return {
        "year": y,
        "clinic": clinic_key,
        "clinics": list_clinics(conn, y),
        "rows": rows,
        "totals": totals,
        "aging_columns": [f"{_MONTH_LABELS[m - 1]} {y}" for m in range(1, 13)],
    }
