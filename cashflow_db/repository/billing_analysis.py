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

def _compact_check_sql(expr: str) -> str:
    """SQL twin of waystar_shadow.full_compact_check_ref. Regex runs once."""
    return (
        "(SELECT CASE WHEN c ~ '^[0-9]+$' "
        "THEN COALESCE(NULLIF(ltrim(c, '0'), ''), '0') ELSE c END "
        "FROM (SELECT regexp_replace(regexp_replace(upper(btrim(COALESCE("
        + expr
        + ", ''))), '\\.0+$', ''), '[^A-Z0-9]', '', 'g') AS c) compacted)"
    )


def _override_date_sql(key: str) -> str:
    raw = f"wi.manual_overrides->>'{key}'"
    return (
        "CASE WHEN btrim(COALESCE("
        + raw
        + ", '')) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
        "THEN substring(btrim(" + raw + ") from 1 for 10)::date END"
    )


# Collection date: tracker txn_date, then Waystar trans_date, then the
# eligibility sheet date, then Snowflake primary_check_date.
# Shared by the cash-collected grid and the cash conversion cycle.
_COLLECT_SQL = f"""
WITH bounds AS (
    SELECT %s::date AS start_on, %s::date AS end_on, %s::text AS clinic_key
),
visits AS (
    SELECT btrim(kpi.emr_id) AS emr_id,
           kpi.date_of_service,
           kpi.primary_check_date,
           COALESCE(kpi.insurance_payment, 0) + COALESCE(kpi.client_payment, 0) AS amount,
           {_compact_check_sql("kpi.primary_check_number")} AS sf_ref
    FROM analytics.snowflake_visit_kpi kpi
    CROSS JOIN bounds b
    WHERE kpi.date_of_service >= b.start_on
      AND kpi.date_of_service < b.end_on
      AND (
          b.clinic_key IS NULL
          OR lower(btrim(kpi.clinic)) = lower(btrim(b.clinic_key))
      )
),
tracker_min AS (
    SELECT ref, MIN(txn_date) AS txn_date
    FROM (
        SELECT {_compact_check_sql("ref_raw")} AS ref,
               t.txn_date
        FROM billing.transaction_tracker_row t
        CROSS JOIN LATERAL (
            VALUES (t.eft_1), (t.eft_2), (t.check_reference)
        ) AS refs(ref_raw)
        WHERE t.deleted_at IS NULL
          AND t.txn_date IS NOT NULL
    ) compact_refs
    WHERE ref <> ''
    GROUP BY ref
),
waystar_base AS (
    SELECT m.webpt_patient_id AS emr_id,
           c.from_date AS date_of_service,
           c.trans_date,
           c.total_remit_amount,
           c.remit_numbers
    FROM billing.waystar_claim c
    JOIN billing.waystar_webpt_map m
      ON m.waystar_claim_key = c.claim_key
    CROSS JOIN bounds b
    WHERE c.from_date >= b.start_on
      AND c.from_date < b.end_on
      AND COALESCE(m.webpt_patient_id, '') <> ''
),
waystar AS (
    SELECT emr_id,
           date_of_service,
           MIN(trans_date) AS trans_date
    FROM waystar_base
    WHERE COALESCE(total_remit_amount, 0) > 0
      AND trans_date IS NOT NULL
    GROUP BY emr_id, date_of_service
),
ws_tracker AS (
    SELECT refs.emr_id, refs.date_of_service, MIN(t.txn_date) AS txn_date
    FROM (
        SELECT w.emr_id,
               w.date_of_service,
               {_compact_check_sql("num")} AS ref
        FROM waystar_base w
        CROSS JOIN LATERAL unnest(COALESCE(w.remit_numbers, ARRAY[]::text[])) AS num
    ) refs
    JOIN tracker_min t ON t.ref = refs.ref AND refs.ref <> ''
    GROUP BY refs.emr_id, refs.date_of_service
),
elig_base AS (
    SELECT wi.emr_patient_id AS emr_id,
           wi.dos AS date_of_service,
           COALESCE(
               {_override_date_sql("check_date")},
               {_override_date_sql("insurance_check_date")}
           ) AS check_date,
           wi.manual_overrides
    FROM ops.eligibility_work_item wi
    CROSS JOIN bounds b
    WHERE wi.dos >= b.start_on
      AND wi.dos < b.end_on
      AND COALESCE(wi.emr_patient_id, '') <> ''
),
elig AS (
    SELECT emr_id, date_of_service, MIN(check_date) AS check_date
    FROM elig_base
    WHERE check_date IS NOT NULL
    GROUP BY emr_id, date_of_service
),
el_tracker AS (
    SELECT refs.emr_id, refs.date_of_service, MIN(t.txn_date) AS txn_date
    FROM (
        SELECT e.emr_id,
               e.date_of_service,
               {_compact_check_sql("num")} AS ref
        FROM elig_base e
        CROSS JOIN LATERAL (
            VALUES
                (e.manual_overrides->>'check_number'),
                (e.manual_overrides->>'insurance_check_number')
        ) AS nums(num)
    ) refs
    JOIN tracker_min t ON t.ref = refs.ref AND refs.ref <> ''
    GROUP BY refs.emr_id, refs.date_of_service
),
sf_tracker AS (
    SELECT v.emr_id, v.date_of_service, MIN(t.txn_date) AS txn_date
    FROM visits v
    JOIN tracker_min t ON t.ref = v.sf_ref AND v.sf_ref <> ''
    GROUP BY v.emr_id, v.date_of_service
)
SELECT v.date_of_service,
       v.amount,
       COALESCE(
           sf_tracker.txn_date,
           ws_tracker.txn_date,
           el_tracker.txn_date,
           waystar.trans_date,
           elig.check_date,
           v.primary_check_date
       ) AS collect_date
FROM visits v
LEFT JOIN sf_tracker
  ON sf_tracker.emr_id = v.emr_id
 AND sf_tracker.date_of_service = v.date_of_service
LEFT JOIN ws_tracker
  ON ws_tracker.emr_id = v.emr_id
 AND ws_tracker.date_of_service = v.date_of_service
LEFT JOIN el_tracker
  ON el_tracker.emr_id = v.emr_id
 AND el_tracker.date_of_service = v.date_of_service
LEFT JOIN waystar
  ON waystar.emr_id = v.emr_id
 AND waystar.date_of_service = v.date_of_service
LEFT JOIN elig
  ON elig.emr_id = v.emr_id
 AND elig.date_of_service = v.date_of_service
"""

# One materialization per request. Aging and cycle both read this table.
LOAD_AGED_SQL = (
    "CREATE TEMP TABLE billing_collect_aged ON COMMIT DROP AS\n" + _COLLECT_SQL
)

AGING_SQL = """
SELECT date_trunc('month', aged.date_of_service)::date AS visit_month,
       date_trunc('month', aged.collect_date)::date AS collect_month,
       COALESCE(SUM(aged.amount), 0) AS amount
FROM billing_collect_aged aged
WHERE aged.collect_date IS NOT NULL
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
FROM billing_collect_aged aged
GROUP BY GROUPING SETS ((1), ())
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
    month_rows = client.fetchall(
        conn, MONTHLY_SQL, (start, end, clinic_key, clinic_key)
    )
    client.execute(conn, "DROP TABLE IF EXISTS billing_collect_aged")
    client.execute(conn, LOAD_AGED_SQL, (start, end, clinic_key))
    aging_rows = client.fetchall(conn, AGING_SQL)
    cycle_rows = client.fetchall(conn, CYCLE_SQL)
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
    return {
        "year": y,
        "clinic": clinic_key,
        "clinics": list_clinics(conn, y),
        "rows": rows,
        "totals": totals,
        "aging_columns": [f"{_MONTH_LABELS[m - 1]} {y}" for m in range(1, 13)],
    }
