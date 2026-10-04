"""Payor behavior / insurance analytics repository contracts."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Iterable

import psycopg

from cashflow_db.repository import client
from cashflow_forecast.payer_plan import is_blank_ins

_REJECT_FILL_NAMES = frozenset({"deposit"})
_BLANK_SQL_LIST = (
    "'', 'blank', '(blank)', 'nan', 'none', 'null', 'n/a', '#n/a', 'unknown', 'deposit'"
)


def usable_insurance_name(raw: Any) -> str:
    """Real payer label, or empty when the source is a placeholder."""
    text = str(raw or "").strip()
    if not text or is_blank_ins(text) or text.lower() in _REJECT_FILL_NAMES:
        return ""
    return text


def usable_sql(expr: str) -> str:
    """SQL that nulls blank / placeholder insurance labels."""
    return (
        "NULLIF(CASE "
        f"WHEN NULLIF(BTRIM({expr}::text), '') IS NULL THEN NULL "
        f"WHEN lower(BTRIM({expr}::text)) IN ({_BLANK_SQL_LIST}) THEN NULL "
        f"ELSE BTRIM({expr}::text) END, '')"
    )


def is_blank_sql(expr: str) -> str:
    return (
        f"(NULLIF(BTRIM({expr}::text), '') IS NULL "
        f"OR lower(BTRIM({expr}::text)) IN ({_BLANK_SQL_LIST}))"
    )


def replace_payor_behavior_summary(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None,
    rows: Iterable[dict[str, Any]],
) -> int:
    if reconciliation_run_id:
        client.execute(
            conn,
            "DELETE FROM analytics.payor_behavior_summary WHERE reconciliation_run_id = %s::uuid",
            (reconciliation_run_id,),
        )
    else:
        client.execute(conn, "DELETE FROM analytics.payor_behavior_summary")
    n = 0
    sql = """
        INSERT INTO analytics.payor_behavior_summary (
            reconciliation_run_id, payor_key, payor_raw, check_count,
            median_cash_velocity_days, p75_cash_velocity_days,
            median_eob_to_deposit_days, deposit_weekday_profile,
            payload
        ) VALUES (
            %s::uuid, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb
        )
    """
    for r in rows:
        payload = {k: v for k, v in r.items() if k not in {
            "payor_key", "payor_raw", "check_count",
            "median_cash_velocity_days", "p75_cash_velocity_days",
            "median_eob_to_deposit_days", "deposit_weekday_profile",
        }}
        client.execute(
            conn,
            sql,
            (
                reconciliation_run_id,
                r.get("payor_key") or r.get("insurance_key") or r.get("payor"),
                r.get("payor_raw") or r.get("payor"),
                r.get("check_count"),
                r.get("median_cash_velocity_days") or r.get("cash_velocity_days"),
                r.get("p75_cash_velocity_days"),
                r.get("median_eob_to_deposit_days") or r.get("eob_to_deposit_days"),
                json.dumps(r.get("deposit_weekday_profile") or {}),
                json.dumps(payload, default=str),
            ),
        )
        n += 1
    return n


def _date_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def replace_checks_timeline(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None,
    rows: Iterable[dict[str, Any]],
) -> int:
    if reconciliation_run_id:
        client.execute(
            conn,
            "DELETE FROM analytics.checks_timeline WHERE reconciliation_run_id = %s::uuid",
            (reconciliation_run_id,),
        )
    else:
        client.execute(conn, "DELETE FROM analytics.checks_timeline")
    n = 0
    sql = """
        INSERT INTO analytics.checks_timeline (
            reconciliation_run_id, check_eft_num, payor_raw, eob_date,
            deposit_date, paid_amount, payload
        ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
    """
    for r in rows:
        client.execute(
            conn,
            sql,
            (
                reconciliation_run_id,
                r.get("check_eft_num"),
                r.get("payor_raw") or r.get("payor"),
                _date_or_none(r.get("eob_date")),
                _date_or_none(r.get("deposit_date")),
                r.get("paid_amount"),
                json.dumps(r, default=str),
            ),
        )
        n += 1
    return n


def get_payor_behavior_summary(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if reconciliation_run_id:
        rows = client.fetchall(
            conn,
            """
            SELECT * FROM analytics.payor_behavior_summary
            WHERE reconciliation_run_id = %s::uuid
            ORDER BY payor_key
            """,
            (reconciliation_run_id,),
        )
    else:
        rows = client.fetchall(
            conn,
            """
            SELECT DISTINCT ON (payor_key) *
            FROM analytics.payor_behavior_summary
            ORDER BY payor_key, created_at DESC
            """,
        )
    # Flatten payload for forecast consumers expecting CSV columns
    out: list[dict[str, Any]] = []
    for r in rows:
        flat = dict(r)
        payload = flat.pop("payload", None) or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            for k, v in payload.items():
                flat.setdefault(k, v)
        out.append(flat)
    return out


def get_checks_timeline(
    conn: psycopg.Connection,
    *,
    reconciliation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    if reconciliation_run_id:
        return client.fetchall(
            conn,
            """
            SELECT * FROM analytics.checks_timeline
            WHERE reconciliation_run_id = %s::uuid
            ORDER BY deposit_date NULLS LAST, eob_date NULLS LAST
            """,
            (reconciliation_run_id,),
        )
    return client.fetchall(
        conn,
        """
        SELECT * FROM analytics.checks_timeline
        ORDER BY created_at DESC
        LIMIT 50000
        """,
    )


def summarize_behavior_trend(
    conn: psycopg.Connection,
    *,
    grain: str = "month",
    insurers: list[str] | None = None,
    limit_insurers: int = 8,
) -> list[dict[str, Any]]:
    """Median EOB→deposit lag and paid $ by payor and month/year.

    Uses the latest reconciliation_run_id that has checks_timeline rows so
    overlapping historical runs are not double-counted.
    """
    period_fmt = "YYYY" if str(grain).lower().startswith("year") else "YYYY-MM"
    params: list[Any] = []
    ins_sql = ""
    if insurers:
        ins_sql = " AND NULLIF(BTRIM(payor_raw), '') = ANY(%s)"
        params.append(list(insurers))
    sql = f"""
        WITH latest AS (
            SELECT reconciliation_run_id
            FROM analytics.checks_timeline
            WHERE reconciliation_run_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ),
        ranked AS (
            SELECT
                to_char(deposit_date, '{period_fmt}') AS period,
                COALESCE(NULLIF(BTRIM(payor_raw), ''), 'Unknown') AS ins_name,
                COUNT(*)::int AS check_count,
                ROUND(SUM(COALESCE(paid_amount, 0)), 2) AS paid_amount,
                PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY (deposit_date - eob_date)
                ) AS median_lag_days
            FROM analytics.checks_timeline
            WHERE deposit_date IS NOT NULL
              AND eob_date IS NOT NULL
              AND (
                    reconciliation_run_id IS NULL
                    OR reconciliation_run_id = (SELECT reconciliation_run_id FROM latest)
                    OR NOT EXISTS (SELECT 1 FROM latest)
              )
              {ins_sql}
            GROUP BY 1, 2
        ),
        top_ins AS (
            SELECT ins_name
            FROM ranked
            GROUP BY ins_name
            ORDER BY SUM(paid_amount) DESC
            LIMIT %s
        )
        SELECT r.period, r.ins_name, r.check_count, r.paid_amount,
               ROUND(r.median_lag_days::numeric, 1) AS median_lag_days
        FROM ranked r
        JOIN top_ins t ON t.ins_name = r.ins_name
        ORDER BY r.period, r.paid_amount DESC
    """
    params.append(int(limit_insurers) if limit_insurers > 0 else 8)
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        lag = row.get("median_lag_days")
        out.append(
            {
                "period": str(row.get("period") or ""),
                "ins_name": str(row.get("ins_name") or ""),
                "check_count": int(row.get("check_count") or 0),
                "paid_amount": round(float(row.get("paid_amount") or 0), 2),
                "median_lag_days": None if lag is None else round(float(lag), 1),
            }
        )
    return out


_TRACKER_LABELED_SQL = """
        WITH latest AS (
            SELECT reconciliation_run_id
            FROM analytics.checks_timeline
            WHERE reconciliation_run_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ),
        payor_map AS (
            SELECT DISTINCT ON (NULLIF(BTRIM(check_eft_num), ''))
                NULLIF(BTRIM(check_eft_num), '') AS eft,
                NULLIF(BTRIM(payor_raw), '') AS payor
            FROM analytics.checks_timeline
            WHERE NULLIF(BTRIM(check_eft_num), '') IS NOT NULL
              AND (
                    reconciliation_run_id IS NULL
                    OR reconciliation_run_id = (SELECT reconciliation_run_id FROM latest)
                    OR NOT EXISTS (SELECT 1 FROM latest)
              )
            ORDER BY NULLIF(BTRIM(check_eft_num), ''), created_at DESC
        ),
        labeled AS (
            SELECT
                src.txn_date,
                src.amount,
                src.ins_name,
                regexp_replace(lower(src.ins_name), '[^a-z0-9]+', '', 'g') AS ins_key,
                src.check_num
            FROM (
                SELECT
                    t.txn_date,
                    COALESCE(t.amount, 0)::numeric AS amount,
                    COALESCE(
                        NULLIF(BTRIM(p1.payor), ''),
                        NULLIF(BTRIM(p2.payor), ''),
                        NULLIF(BTRIM(p3.payor), ''),
                        CASE
                            WHEN t.description ~* 'DES:|PMT ID|PMT INFO|CO ID'
                                THEN NULL
                            WHEN length(BTRIM(t.description)) > 48
                                THEN NULL
                            WHEN lower(BTRIM(t.description)) IN ('deposit', 'unknown', E'\\\\')
                                THEN NULL
                            ELSE NULLIF(BTRIM(t.description), '')
                        END,
                        'Unknown'
                    ) AS ins_name,
                    COALESCE(
                        NULLIF(BTRIM(t.eft_1), ''),
                        NULLIF(BTRIM(t.eft_2), ''),
                        NULLIF(BTRIM(t.check_reference), '')
                    ) AS check_num
                FROM billing.transaction_tracker_row t
                LEFT JOIN payor_map p1 ON p1.eft = NULLIF(BTRIM(t.eft_1), '')
                LEFT JOIN payor_map p2 ON p2.eft = NULLIF(BTRIM(t.eft_2), '')
                LEFT JOIN payor_map p3 ON p3.eft = NULLIF(BTRIM(t.check_reference), '')
                WHERE t.deleted_at IS NULL
                  AND t.txn_date IS NOT NULL
            ) src
        )
"""


_SKIP_TOKENS = frozenset({"inc", "llc", "ltd", "phsp", "the", "and", "of", "for"})
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def insurance_match_tokens(names: list[str] | None) -> list[str]:
    """WebPT filter labels → compact tokens that can hit tracker payor/description."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        compact = _TOKEN_SPLIT.sub("", text)
        parts = [p for p in _TOKEN_SPLIT.split(text) if p]
        for tok in [compact, *parts]:
            if len(tok) < 3 or tok in _SKIP_TOKENS or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out


def _ins_compact(name: str) -> str:
    return _TOKEN_SPLIT.sub("", str(name or "").strip().lower())


def insurance_names_match(left: str, right: str) -> bool:
    a = _ins_compact(left)
    b = _ins_compact(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    for tok in insurance_match_tokens([left]):
        if tok in b:
            return True
    for tok in insurance_match_tokens([right]):
        if tok in a:
            return True
    return False


def merge_insurance_mix(
    *,
    landed: list[dict[str, Any]],
    overdue: list[dict[str, Any]],
    risk: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Attach overdue/risk onto Eligibility Sheet insurer names via token match."""
    rows: list[dict[str, Any]] = []

    def _find(name: str) -> dict[str, Any] | None:
        for row in rows:
            if insurance_names_match(row["ins_name"], name):
                return row
        return None

    def _add(name: str, **amounts: float) -> None:
        label = usable_insurance_name(name)
        if not label:
            return
        row = _find(label)
        if row is None:
            row = {"ins_name": label, "landed": 0.0, "overdue": 0.0, "risk": 0.0}
            rows.append(row)
        for key, val in amounts.items():
            row[key] = round(float(row.get(key) or 0) + float(val or 0), 2)

    for item in landed:
        _add(str(item.get("ins_name") or ""), landed=float(item.get("landed") or 0))
    for item in overdue:
        _add(
            str(item.get("ins_name") or ""),
            overdue=float(item.get("expected_payment") or item.get("overdue") or 0),
        )
    for item in risk:
        _add(
            str(item.get("ins_name") or ""),
            risk=float(item.get("exposure_amount") or item.get("risk") or 0),
        )
    rows.sort(
        key=lambda r: float(r["landed"]) + float(r["overdue"]) + float(r["risk"]),
        reverse=True,
    )
    if limit is None or int(limit) <= 0:
        return rows
    return rows[: int(limit)]


def _tracker_ins_date_sql(
    *,
    insurers: list[str] | None,
    d0: date | None,
    d1: date | None,
    params: list[Any],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    tokens = insurance_match_tokens(insurers)
    if tokens:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM unnest(%s::text[]) AS tok(t)
                WHERE length(tok.t) >= 3
                  AND ins_key LIKE '%%' || tok.t || '%%'
            )
            """
        )
        params.append(tokens)
    if d0 is not None:
        clauses.append("txn_date >= %s")
        params.append(d0)
    if d1 is not None:
        clauses.append("txn_date <= %s")
        params.append(d1)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def summarize_tracker_paid_trend(
    conn: psycopg.Connection,
    *,
    grain: str = "month",
    insurers: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
    limit_insurers: int = 2,
) -> list[dict[str, Any]]:
    """Paid $ by insurer from Transaction Tracker txn_date (not EOB/deposit)."""
    g = str(grain).lower()
    if g.startswith("year"):
        period_fmt = "YYYY"
    elif g.startswith("day"):
        period_fmt = "YYYY-MM-DD"
    else:
        period_fmt = "YYYY-MM"
    params: list[Any] = []
    where_sql, params = _tracker_ins_date_sql(
        insurers=insurers, d0=d0, d1=d1, params=params
    )
    cap = int(limit_insurers) if limit_insurers > 0 else 2
    filtered_ins = 1 if insurers else 0
    sql = f"""
        {_TRACKER_LABELED_SQL}
        , filtered AS (
            SELECT * FROM labeled
            {where_sql}
        ),
        bounds AS (
            SELECT
                COALESCE(%s::date, (SELECT MIN(txn_date) FROM filtered)) AS win_start,
                COALESCE(%s::date, (SELECT MAX(txn_date) FROM filtered)) AS win_end
        ),
        mid AS (
            SELECT
                win_start,
                win_end,
                (win_start + ((win_end - win_start) / 2))::date AS mid_date,
                (win_end > win_start) AS has_drop
            FROM bounds
        ),
        half AS (
            SELECT
                f.ins_name,
                COALESCE(SUM(f.amount) FILTER (
                    WHERE f.txn_date < m.mid_date
                ), 0) AS first_amt,
                COALESCE(SUM(f.amount) FILTER (
                    WHERE f.txn_date >= m.mid_date
                ), 0) AS last_amt,
                COALESCE(SUM(f.amount), 0) AS total_amt,
                BOOL_AND(m.has_drop) AS has_drop
            FROM filtered f
            CROSS JOIN mid m
            GROUP BY f.ins_name
        ),
        top_ins AS (
            SELECT ins_name
            FROM half
            WHERE ins_name NOT IN ('Unknown', 'Deposit')
              AND total_amt > 0
              AND (
                    NOT COALESCE(has_drop, false)
                    OR {filtered_ins} = 1
                    OR first_amt >= 1000
              )
            ORDER BY
                CASE
                    WHEN NOT COALESCE(has_drop, false) THEN total_amt
                    WHEN {filtered_ins} = 1 THEN total_amt
                    ELSE first_amt - last_amt
                END DESC,
                total_amt DESC
            LIMIT %s
        ),
        ranked AS (
            SELECT
                to_char(txn_date, '{period_fmt}') AS period,
                ins_name,
                COUNT(*)::int AS check_count,
                ROUND(SUM(amount), 2) AS paid_amount
            FROM filtered
            GROUP BY 1, 2
        )
        SELECT r.period, r.ins_name, r.check_count, r.paid_amount
        FROM ranked r
        JOIN top_ins t ON t.ins_name = r.ins_name
        ORDER BY r.period, r.paid_amount DESC
    """
    params.extend([d0, d1, cap])
    rows = client.fetchall(conn, sql, params)
    return [
        {
            "period": str(row.get("period") or ""),
            "ins_name": str(row.get("ins_name") or ""),
            "check_count": int(row.get("check_count") or 0),
            "paid_amount": round(float(row.get("paid_amount") or 0), 2),
        }
        for row in rows
    ]


def sum_tracker_cash(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    insurers: list[str] | None = None,
) -> float:
    """Tracker cash in a date window. Clinic is not on the tracker."""
    params: list[Any] = []
    where_sql, params = _tracker_ins_date_sql(
        insurers=insurers, d0=d0, d1=d1, params=params
    )
    sql = f"""
        {_TRACKER_LABELED_SQL}
        SELECT COALESCE(ROUND(SUM(amount), 2), 0) AS amount
        FROM labeled
        {where_sql}
    """
    row = client.fetchone(conn, sql, params)
    return round(float((row or {}).get("amount") or 0), 2)


def tracker_cash_by_insurer(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    insurers: list[str] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Tracker cash by insurer, largest first. No drop-heuristic ranking."""
    params: list[Any] = []
    where_sql, params = _tracker_ins_date_sql(
        insurers=insurers, d0=d0, d1=d1, params=params
    )
    sql = f"""
        {_TRACKER_LABELED_SQL}
        SELECT ins_name, ROUND(SUM(amount), 2) AS amount
        FROM labeled
        {where_sql}
        GROUP BY 1
        ORDER BY amount DESC
        LIMIT %s
    """
    params.append(max(1, int(limit or 30)))
    rows = client.fetchall(conn, sql, params)
    return [
        {
            "ins_name": str(row.get("ins_name") or ""),
            "amount": round(float(row.get("amount") or 0), 2),
        }
        for row in rows
        if str(row.get("ins_name") or "").strip()
    ]


def list_tracker_checks(
    conn: psycopg.Connection,
    *,
    insurers: list[str] | None = None,
    exact_names: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Individual tracker deposits: txn_date, insurer, check #, amount."""
    params: list[Any] = []
    names = [str(n).strip() for n in (exact_names or []) if str(n).strip()]
    where_sql, params = _tracker_ins_date_sql(
        insurers=None if names else insurers,
        d0=d0,
        d1=d1,
        params=params,
    )
    if names:
        extra = "ins_name = ANY(%s)"
        params.append(names)
        where_sql = f"{where_sql} AND {extra}" if where_sql else f" WHERE {extra}"
    sql = f"""
        {_TRACKER_LABELED_SQL}
        SELECT txn_date, ins_name, check_num, ROUND(amount, 2) AS paid_amount
        FROM labeled
        {where_sql}
        ORDER BY txn_date DESC, paid_amount DESC
        LIMIT %s
    """
    params.append(int(limit) if limit > 0 else 300)
    rows = client.fetchall(conn, sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        day = row.get("txn_date")
        out.append(
            {
                "txn_date": day.isoformat() if hasattr(day, "isoformat") else str(day or ""),
                "ins_name": str(row.get("ins_name") or ""),
                "check_num": str(row.get("check_num") or ""),
                "paid_amount": round(float(row.get("paid_amount") or 0), 2),
            }
        )
    return out


_RESOLVE_FROM_SQL = f"""
WITH latest_recon AS (
    SELECT reconciliation_run_id
    FROM billing.reconciliation_run
    WHERE status = 'success'
    ORDER BY created_at DESC
    LIMIT 1
),
latest_timeline AS (
    SELECT reconciliation_run_id
    FROM analytics.checks_timeline
    WHERE reconciliation_run_id IS NOT NULL
    ORDER BY created_at DESC
    LIMIT 1
),
payor_map AS (
    SELECT DISTINCT ON (NULLIF(BTRIM(check_eft_num), ''))
        NULLIF(BTRIM(check_eft_num), '') AS eft,
        {usable_sql("payor_raw")} AS payor
    FROM analytics.checks_timeline
    WHERE NULLIF(BTRIM(check_eft_num), '') IS NOT NULL
      AND {usable_sql("payor_raw")} IS NOT NULL
      AND (
            reconciliation_run_id IS NULL
            OR reconciliation_run_id = (SELECT reconciliation_run_id FROM latest_timeline)
            OR NOT EXISTS (SELECT 1 FROM latest_timeline)
      )
    ORDER BY NULLIF(BTRIM(check_eft_num), ''), created_at DESC
),
blank_wi AS (
    SELECT
        wi.work_item_id,
        wi.emr_patient_id,
        wi.dos,
        wi.facility_name,
        wi.patient_name,
        wi.source_visit_status,
        wi.insurance_name,
        wi.context
    FROM ops.eligibility_work_item wi
    WHERE {{blank_pred}}
),
sf AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        {usable_sql("sf.insurance")} AS ins,
        NULLIF(BTRIM(sf.primary_check_number), '') AS chk1,
        NULLIF(BTRIM(sf.secondary_check_number), '') AS chk2
    FROM blank_wi b
    JOIN analytics.snowflake_visit_kpi sf
      ON sf.emr_id = b.emr_patient_id
     AND sf.date_of_service = b.dos
    ORDER BY b.work_item_id, sf.insurance NULLS LAST
),
recon_line AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        COALESCE(
            {usable_sql("rl.ins_name")},
            {usable_sql("rl.insurance_revflow")}
        ) AS ins
    FROM blank_wi b
    JOIN billing.reconciliation_line rl
      ON rl.webpt_patient_id = b.emr_patient_id
     AND rl.date_of_service = b.dos
     AND rl.reconciliation_run_id = (SELECT reconciliation_run_id FROM latest_recon)
    WHERE {usable_sql("rl.ins_name")} IS NOT NULL
       OR {usable_sql("rl.insurance_revflow")} IS NOT NULL
    ORDER BY b.work_item_id, rl.ins_name NULLS LAST
),
recon_visit AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        NULLIF(BTRIM(rv.primary_check_number), '') AS chk1,
        NULLIF(BTRIM(rv.secondary_check_number), '') AS chk2
    FROM blank_wi b
    JOIN billing.reconciliation_visit_agg rv
      ON rv.webpt_patient_id = b.emr_patient_id
     AND rv.date_of_service = b.dos
     AND rv.reconciliation_run_id = (SELECT reconciliation_run_id FROM latest_recon)
    ORDER BY b.work_item_id
),
note_ins AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        COALESCE(
            {usable_sql("cn.insurance_name_raw")},
            {usable_sql("v.insurance_name_raw")}
        ) AS ins
    FROM blank_wi b
    JOIN core.patient p ON p.webpt_patient_id = b.emr_patient_id
    JOIN core.visit v
      ON v.patient_id = p.patient_id
     AND v.service_date = b.dos
    LEFT JOIN core.clinical_note cn ON cn.visit_id = v.visit_id
    WHERE {usable_sql("cn.insurance_name_raw")} IS NOT NULL
       OR {usable_sql("v.insurance_name_raw")} IS NOT NULL
    ORDER BY b.work_item_id, cn.insurance_name_raw NULLS LAST
),
charge_name_counts AS (
    SELECT
        c.insurance_id,
        {usable_sql("wi.insurance_name")} AS ins,
        count(*) AS n
    FROM analytics.pt_city_charge_ins c
    JOIN ops.eligibility_work_item wi
      ON wi.emr_patient_id = c.emr_id
     AND wi.dos = c.date_of_service
    WHERE NULLIF(BTRIM(c.insurance_id), '') IS NOT NULL
      AND {usable_sql("wi.insurance_name")} IS NOT NULL
    GROUP BY 1, 2
),
charge_id_map AS (
    SELECT DISTINCT ON (insurance_id)
        insurance_id,
        ins
    FROM charge_name_counts
    ORDER BY insurance_id, n DESC, ins
),
charge_ins AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        m.ins
    FROM blank_wi b
    JOIN analytics.pt_city_charge_ins c
      ON c.emr_id = b.emr_patient_id
     AND c.date_of_service = b.dos
    JOIN charge_id_map m ON m.insurance_id = c.insurance_id
    WHERE m.ins IS NOT NULL
    ORDER BY b.work_item_id
),
prior_paid AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        {usable_sql("src.insurance_name")} AS ins
    FROM blank_wi b
    JOIN ops.eligibility_work_item src
      ON src.emr_patient_id = b.emr_patient_id
     AND src.work_item_id <> b.work_item_id
     AND src.dos <= b.dos
     AND lower(btrim(COALESCE(src.source_visit_status, ''))) IN ('paid', 'partial')
     AND {usable_sql("src.insurance_name")} IS NOT NULL
    ORDER BY b.work_item_id, src.dos DESC
),
check_src AS (
    SELECT DISTINCT b.work_item_id, chk
    FROM blank_wi b
    LEFT JOIN sf ON sf.work_item_id = b.work_item_id
    LEFT JOIN recon_visit rv ON rv.work_item_id = b.work_item_id
    CROSS JOIN LATERAL unnest(ARRAY[
        NULLIF(BTRIM(b.context->>'primary_check_number'), ''),
        NULLIF(BTRIM(b.context->>'secondary_check_number'), ''),
        NULLIF(BTRIM(b.context->>'insurance_check_number'), ''),
        sf.chk1, sf.chk2, rv.chk1, rv.chk2
    ]) AS chk
    WHERE chk IS NOT NULL
),
eob_by_visit AS (
    SELECT DISTINCT ON (b.work_item_id)
        b.work_item_id,
        {usable_sql("ec.payor_raw")} AS payor
    FROM blank_wi b
    JOIN core.patient p ON p.webpt_patient_id = b.emr_patient_id
    JOIN billing.eob_line el
      ON el.date_of_service = b.dos
     AND (
            el.patient_id = p.patient_id
            OR (
                NULLIF(BTRIM(el.revflow_patient_id), '') IS NOT NULL
                AND NULLIF(BTRIM(p.revflow_patient_id), '') IS NOT NULL
                AND el.revflow_patient_id = p.revflow_patient_id
            )
     )
    JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
    WHERE {usable_sql("ec.payor_raw")} IS NOT NULL
    ORDER BY b.work_item_id
),
eob_by_check AS (
    SELECT DISTINCT ON (c.work_item_id)
        c.work_item_id,
        {usable_sql("ec.payor_raw")} AS payor
    FROM check_src c
    JOIN billing.eob_check ec ON NULLIF(BTRIM(ec.check_eft_num), '') = c.chk
    WHERE {usable_sql("ec.payor_raw")} IS NOT NULL
    ORDER BY c.work_item_id
),
timeline_by_check AS (
    SELECT DISTINCT ON (c.work_item_id)
        c.work_item_id,
        pm.payor
    FROM check_src c
    JOIN payor_map pm ON pm.eft = c.chk
    WHERE pm.payor IS NOT NULL
    ORDER BY c.work_item_id
),
tracker_by_check AS (
    SELECT DISTINCT ON (c.work_item_id)
        c.work_item_id,
        COALESCE(
            pm.payor,
            CASE
                WHEN t.description ~* 'DES:|PMT ID|PMT INFO|CO ID' THEN NULL
                WHEN length(BTRIM(t.description)) > 48 THEN NULL
                ELSE {usable_sql("t.description")}
            END
        ) AS payor
    FROM check_src c
    JOIN billing.transaction_tracker_row t
      ON t.deleted_at IS NULL
     AND c.chk IN (
            NULLIF(BTRIM(t.eft_1), ''),
            NULLIF(BTRIM(t.eft_2), ''),
            NULLIF(BTRIM(t.check_reference), '')
     )
    LEFT JOIN payor_map pm ON pm.eft = c.chk
    ORDER BY c.work_item_id
),
resolved AS (
    SELECT
        b.work_item_id,
        b.emr_patient_id,
        b.dos,
        b.facility_name,
        b.patient_name,
        b.source_visit_status,
        b.insurance_name,
        COALESCE(
            {usable_sql("b.insurance_name")},
            sf.ins,
            rl.ins,
            dn.ins,
            ch.ins,
            ev.payor,
            ec.payor,
            tl.payor,
            tr.payor,
            pp.ins
        ) AS resolved_name,
        CASE
            WHEN {usable_sql("b.insurance_name")} IS NOT NULL THEN 'eligibility'
            WHEN sf.ins IS NOT NULL THEN 'eligibility_sf'
            WHEN rl.ins IS NOT NULL THEN 'eligibility_recon'
            WHEN dn.ins IS NOT NULL THEN 'daily_note'
            WHEN ch.ins IS NOT NULL THEN 'charges'
            WHEN ev.payor IS NOT NULL THEN 'check_visit'
            WHEN ec.payor IS NOT NULL THEN 'check_number'
            WHEN tl.payor IS NOT NULL THEN 'check_timeline'
            WHEN tr.payor IS NOT NULL THEN 'tracker'
            WHEN pp.ins IS NOT NULL THEN 'prior_paid'
            ELSE 'unresolved'
        END AS fill_source,
        (
            SELECT string_agg(DISTINCT chk, ', ')
            FROM check_src cs
            WHERE cs.work_item_id = b.work_item_id
        ) AS check_numbers
    FROM blank_wi b
    LEFT JOIN sf ON sf.work_item_id = b.work_item_id
    LEFT JOIN recon_line rl ON rl.work_item_id = b.work_item_id
    LEFT JOIN note_ins dn ON dn.work_item_id = b.work_item_id
    LEFT JOIN charge_ins ch ON ch.work_item_id = b.work_item_id
    LEFT JOIN eob_by_visit ev ON ev.work_item_id = b.work_item_id
    LEFT JOIN eob_by_check ec ON ec.work_item_id = b.work_item_id
    LEFT JOIN timeline_by_check tl ON tl.work_item_id = b.work_item_id
    LEFT JOIN tracker_by_check tr ON tr.work_item_id = b.work_item_id
    LEFT JOIN prior_paid pp ON pp.work_item_id = b.work_item_id
)
"""


def lookup_insurance_name(
    conn: psycopg.Connection,
    *,
    emr: str,
    dos: Any,
    check_numbers: Iterable[str] | None = None,
    current_name: str = "",
) -> str:
    """Resolve one visit: Eligibility overlay → Check → Tracker."""
    found = usable_insurance_name(current_name)
    if found:
        return found
    extra = [str(n).strip() for n in (check_numbers or []) if str(n or "").strip()]
    row = client.fetchone(
        conn,
        f"""
        WITH latest_recon AS (
            SELECT reconciliation_run_id
            FROM billing.reconciliation_run
            WHERE status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
        ),
        k AS (SELECT %s::text AS emr, %s::date AS dos),
        sf AS (
            SELECT {usable_sql("sf.insurance")} AS ins
            FROM k
            JOIN analytics.snowflake_visit_kpi sf
              ON sf.emr_id = k.emr AND sf.date_of_service = k.dos
            WHERE {usable_sql("sf.insurance")} IS NOT NULL
            LIMIT 1
        ),
        recon AS (
            SELECT COALESCE(
                {usable_sql("rl.ins_name")},
                {usable_sql("rl.insurance_revflow")}
            ) AS ins
            FROM k
            JOIN billing.reconciliation_line rl
              ON rl.webpt_patient_id = k.emr
             AND rl.date_of_service = k.dos
             AND rl.reconciliation_run_id = (SELECT reconciliation_run_id FROM latest_recon)
            WHERE {usable_sql("rl.ins_name")} IS NOT NULL
               OR {usable_sql("rl.insurance_revflow")} IS NOT NULL
            LIMIT 1
        ),
        eob_visit AS (
            SELECT {usable_sql("ec.payor_raw")} AS payor
            FROM k
            JOIN core.patient p ON p.webpt_patient_id = k.emr
            JOIN billing.eob_line el
              ON el.date_of_service = k.dos
             AND (
                    el.patient_id = p.patient_id
                    OR el.revflow_patient_id = p.revflow_patient_id
             )
            JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
            WHERE {usable_sql("ec.payor_raw")} IS NOT NULL
            LIMIT 1
        ),
        note_ins AS (
            SELECT COALESCE(
                {usable_sql("cn.insurance_name_raw")},
                {usable_sql("v.insurance_name_raw")}
            ) AS ins
            FROM k
            JOIN core.patient p ON p.webpt_patient_id = k.emr
            JOIN core.visit v
              ON v.patient_id = p.patient_id
             AND v.service_date = k.dos
            LEFT JOIN core.clinical_note cn ON cn.visit_id = v.visit_id
            WHERE {usable_sql("cn.insurance_name_raw")} IS NOT NULL
               OR {usable_sql("v.insurance_name_raw")} IS NOT NULL
            LIMIT 1
        ),
        charge_ins AS (
            SELECT mapped.ins
            FROM k
            JOIN analytics.pt_city_charge_ins c
              ON c.emr_id = k.emr AND c.date_of_service = k.dos
            JOIN (
                SELECT DISTINCT ON (insurance_id)
                    insurance_id,
                    ins
                FROM (
                    SELECT
                        c2.insurance_id,
                        {usable_sql("wi.insurance_name")} AS ins,
                        count(*) AS n
                    FROM analytics.pt_city_charge_ins c2
                    JOIN ops.eligibility_work_item wi
                      ON wi.emr_patient_id = c2.emr_id
                     AND wi.dos = c2.date_of_service
                    WHERE NULLIF(BTRIM(c2.insurance_id), '') IS NOT NULL
                      AND {usable_sql("wi.insurance_name")} IS NOT NULL
                    GROUP BY 1, 2
                ) counted
                ORDER BY insurance_id, n DESC, ins
            ) mapped ON mapped.insurance_id = c.insurance_id
            WHERE mapped.ins IS NOT NULL
            LIMIT 1
        )
        SELECT COALESCE(
            (SELECT ins FROM sf),
            (SELECT ins FROM recon),
            (SELECT ins FROM note_ins),
            (SELECT ins FROM charge_ins),
            (SELECT payor FROM eob_visit)
        ) AS name
        """,
        (emr, dos),
    )
    if row and usable_insurance_name(row.get("name")):
        return usable_insurance_name(row.get("name"))
    if extra:
        mapped = client.fetchone(
            conn,
            f"""
            SELECT {usable_sql("c.payor_raw")} AS payor
            FROM (
                SELECT payor_raw, created_at, 1 AS ord
                FROM analytics.checks_timeline
                WHERE NULLIF(BTRIM(check_eft_num), '') = ANY(%s)
                UNION ALL
                SELECT payor_raw, NULL::timestamptz, 2
                FROM billing.eob_check
                WHERE NULLIF(BTRIM(check_eft_num), '') = ANY(%s)
            ) c
            WHERE {usable_sql("c.payor_raw")} IS NOT NULL
            ORDER BY c.ord, c.created_at DESC NULLS LAST
            LIMIT 1
            """,
            (extra, extra),
        )
        if mapped and usable_insurance_name(mapped.get("payor")):
            return usable_insurance_name(mapped.get("payor"))
    prior = client.fetchone(
        conn,
        f"""
        SELECT {usable_sql("insurance_name")} AS ins
        FROM ops.eligibility_work_item
        WHERE emr_patient_id = %s
          AND dos <= %s::date
          AND lower(btrim(COALESCE(source_visit_status, ''))) IN ('paid', 'partial')
          AND {usable_sql("insurance_name")} IS NOT NULL
        ORDER BY dos DESC
        LIMIT 1
        """,
        (emr, dos),
    )
    if prior and usable_insurance_name(prior.get("ins")):
        return usable_insurance_name(prior.get("ins"))
    return ""


def backfill_blank_work_item_insurance(
    conn: psycopg.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fill blank eligibility insurance_name. Returns counts + unresolved rows."""
    sql = _RESOLVE_FROM_SQL.format(blank_pred=is_blank_sql("wi.insurance_name"))
    preview = client.fetchall(
        conn,
        sql
        + """
        SELECT
            work_item_id, emr_patient_id, dos, facility_name, patient_name,
            source_visit_status, insurance_name, resolved_name, fill_source,
            check_numbers
        FROM resolved
        """,
    )
    filled = 0
    by_source: dict[str, int] = {}
    unresolved: list[dict[str, Any]] = []
    updates: list[tuple[str, str]] = []
    for row in preview:
        source = str(row.get("fill_source") or "unresolved")
        by_source[source] = by_source.get(source, 0) + 1
        name = usable_insurance_name(row.get("resolved_name"))
        dos = row.get("dos")
        item = {
            "facility": str(row.get("facility_name") or ""),
            "emr": str(row.get("emr_patient_id") or ""),
            "patient_name": str(row.get("patient_name") or ""),
            "dos": dos.isoformat() if hasattr(dos, "isoformat") else str(dos or ""),
            "visit_status": str(row.get("source_visit_status") or ""),
            "check_numbers": str(row.get("check_numbers") or ""),
            "prior_insurance": str(row.get("insurance_name") or ""),
            "fill_source": source,
            "resolved_name": name,
        }
        if name and source != "unresolved":
            filled += 1
            updates.append((name, str(row["work_item_id"])))
        else:
            item["why"] = "no elig / no check payor / no tracker"
            unresolved.append(item)
    if not dry_run and updates:
        client.execute(
            conn,
            """
            UPDATE ops.eligibility_work_item wi
            SET insurance_name = u.name,
                updated_at = now()
            FROM unnest(%s::text[], %s::uuid[]) AS u(name, work_item_id)
            WHERE wi.work_item_id = u.work_item_id
            """,
            ([n for n, _ in updates], [i for _, i in updates]),
        )
    return {
        "blank_before": len(preview),
        "filled": filled if not dry_run else 0,
        "would_fill": filled,
        "unresolved": len(unresolved),
        "by_source": by_source,
        "dry_run": dry_run,
        "unresolved_rows": unresolved,
    }


def write_unresolved_insurance_xlsx(
    rows: list[dict[str, Any]],
    path: Any,
) -> Any:
    """Write leftover blank-insurance visits to an xlsx workbook."""
    from pathlib import Path

    from openpyxl import Workbook

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "unresolved"
    headers = [
        "facility",
        "emr",
        "patient_name",
        "dos",
        "visit_status",
        "check_numbers",
        "prior_insurance",
        "why",
    ]
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    wb.save(dest)
    return dest


def get_plans_of_care(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT
            poc.poc_id,
            poc.date_of_plan_of_care,
            poc.frequency,
            poc.duration,
            poc.plan_text,
            p.webpt_patient_id AS patient_id,
            ph.patient_name,
            pc.webpt_case_id AS case_id
        FROM docs.plan_of_care_detail poc
        JOIN docs.document d ON d.document_id = poc.document_id
        LEFT JOIN core.patient p ON p.patient_id = d.patient_id
        LEFT JOIN core.patient_history ph ON ph.patient_id = p.patient_id AND ph.is_current
        LEFT JOIN core.patient_case pc ON pc.case_pk = d.case_pk
        ORDER BY poc.date_of_plan_of_care NULLS LAST
        """,
    )
