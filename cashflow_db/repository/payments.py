"""Patient payments, EOB, and bank deposit repository contracts."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import psycopg

from cashflow_db.repository import client


def get_patient_payments(
    conn: psycopg.Connection,
    *,
    service_from: date | None = None,
    service_to: date | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if service_from:
        clauses.append("pp.service_date >= %s")
        params.append(service_from)
    if service_to:
        clauses.append("pp.service_date <= %s")
        params.append(service_to)
    lim = f"LIMIT {int(limit)}" if limit else ""
    return client.fetchall(
        conn,
        f"""
        SELECT
            pp.*,
            p.webpt_patient_id,
            pc.webpt_case_id AS case_id
        FROM billing.patient_payment pp
        JOIN core.patient p ON p.patient_id = pp.patient_id
        LEFT JOIN core.patient_case pc ON pc.case_pk = pp.case_pk
        WHERE {' AND '.join(clauses)}
        ORDER BY pp.service_date NULLS LAST
        {lim}
        """,
        params,
    )


def get_eob_payments_unified(
    conn: psycopg.Connection,
    *,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Flatten eob_line ⋈ eob_check into payments_unified-shaped rows."""
    clause = ""
    params: list[Any] = []
    if as_of:
        clause = "WHERE ec.eob_date IS NULL OR ec.eob_date <= %s"
        params.append(as_of)
    return client.fetchall(
        conn,
        f"""
        SELECT
            el.revflow_patient_id,
            COALESCE(p.webpt_patient_id, p_webpt.webpt_patient_id) AS webpt_patient_id,
            ph.patient_name AS first_name,
            NULL::text AS last_name,
            COALESCE(p_webpt.name_key, p.name_key) AS name_key,
            f.name AS facility_name,
            el.date_of_service,
            el.cpt_code,
            el.modifiers AS modifier,
            el.units,
            el.billed_amount,
            el.allowed_amount,
            el.paid_amount,
            el.adjustment_amount,
            el.deductible_amount,
            el.carcs,
            ec.payor_raw AS payor,
            ec.check_eft_num,
            ec.eob_date,
            ec.source_file,
            ec.eob_key,
            ec.company_id,
            el.eob_line_id,
            ec.eob_check_id
        FROM billing.eob_line el
        JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
        LEFT JOIN core.patient p ON p.patient_id = el.patient_id
        LEFT JOIN LATERAL (

            SELECT name_key, webpt_patient_id
            FROM core.patient
            WHERE revflow_patient_id = el.revflow_patient_id
              AND webpt_patient_id IS NOT NULL
              AND btrim(webpt_patient_id) <> ''
            ORDER BY created_at
            LIMIT 1
        ) p_webpt ON true
        LEFT JOIN core.patient_history ph ON ph.patient_id = COALESCE(p.patient_id, el.patient_id) AND ph.is_current
        -- LATERAL LIMIT 1: multi-case same-day visits must not duplicate payment rows
        LEFT JOIN LATERAL (
            SELECT v.facility_id
            FROM core.visit v
            WHERE v.patient_id = el.patient_id AND v.service_date = el.date_of_service
            ORDER BY v.created_at DESC
            LIMIT 1
        ) v ON true
        LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
        {clause}
        ORDER BY ec.eob_date, el.date_of_service
        """,
        params,
    )


def get_eob_payments_slim(
    conn: psycopg.Connection,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payor_tags: tuple[str, ...] | None = None,
    eft_nums: list[str] | None = None,
) -> list[dict[str, Any]]:
    """EOB lines without facility laterals, optional date/payor/EFT filter for audits."""
    if eft_nums is not None and len(eft_nums) == 0:
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if date_from:
        clauses.append("COALESCE(ec.eob_date, ec.check_date) >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(ec.eob_date, ec.check_date) <= %s")
        params.append(date_to)
    if payor_tags:
        ors = " OR ".join(["LOWER(ec.payor_raw) LIKE %s" for _ in payor_tags])
        clauses.append(f"({ors})")
        params.extend([f"%{tag.lower()}%" for tag in payor_tags])
    if eft_nums:
        clauses.append("ec.check_eft_num = ANY(%s)")
        params.append(eft_nums)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return client.fetchall(
        conn,
        f"""
        SELECT
            el.revflow_patient_id,
            p.webpt_patient_id,
            p.name_key,
            el.date_of_service,
            el.cpt_code,
            el.billed_amount,
            el.paid_amount,
            ec.payor_raw AS payor,
            ec.check_eft_num,
            COALESCE(ec.eob_date, ec.check_date) AS eob_date
        FROM billing.eob_line el
        JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
        LEFT JOIN core.patient p ON p.patient_id = el.patient_id
        {where}
        """,
        params,
    )


def get_bank_deposits(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT *
        FROM billing.bank_deposit
        ORDER BY bank_posting_date NULLS LAST, check_date_recognized NULLS LAST
        """,
    )


def get_scoring_actuals(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Daily cash actuals: prefer tracker txn_date when that day exists, else bank_deposit."""
    return client.fetchall(
        conn,
        """
        WITH tracker AS (
            SELECT txn_date AS period, SUM(amount)::float AS amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL AND txn_date IS NOT NULL
            GROUP BY 1
        ),
        bank AS (
            SELECT COALESCE(bank_posting_date, check_date_recognized) AS period,
                   SUM(amount)::float AS amount
            FROM billing.bank_deposit
            WHERE COALESCE(bank_posting_date, check_date_recognized) IS NOT NULL
            GROUP BY 1
        )
        SELECT COALESCE(t.period, b.period) AS period,
               COALESCE(t.amount, b.amount) AS amount,
               CASE WHEN t.period IS NOT NULL THEN 'tracker' ELSE 'bank_deposit' END AS source
        FROM tracker t
        FULL OUTER JOIN bank b ON t.period = b.period
        ORDER BY 1
        """,
    )


def get_unallocated_eob_checks(
    conn: psycopg.Connection,
    *,
    as_of: date,
    max_age_days: int = 14,
    pit: bool = False,
) -> list[dict[str, Any]]:
    """Posted EOBs whose check/EFT is not yet in tracker or a deposit allocation.

    ``max_age_days`` drops stale unmatched EOBs (dead refs / unlinked matches)
    that would otherwise pile onto the first bank day after as_of.
    When ``pit`` is set, a tracker row counts only if it was typed on or
    before ``as_of`` (created_at date). Later typing must not hide the check.
    """
    cutoff = as_of - timedelta(days=max(0, int(max_age_days)))
    seen_by = "AND t.created_at::date <= %s" if pit else ""
    params: list[Any] = [as_of, cutoff]
    if pit:
        params.append(as_of)
    return client.fetchall(
        conn,
        f"""
        SELECT ec.eob_check_id, ec.payor_raw, ec.check_eft_num,
               ec.check_date, ec.eob_date, ec.paid_amount_sum
        FROM billing.eob_check ec
        WHERE COALESCE(ec.eob_date, ec.check_date) <= %s
          AND COALESCE(ec.eob_date, ec.check_date) >= %s
          AND COALESCE(ec.paid_amount_sum, 0) > 0
          AND NOT EXISTS (
              SELECT 1 FROM billing.deposit_check_allocation a
              WHERE a.eob_check_id = ec.eob_check_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM billing.transaction_tracker_row t
              WHERE t.deleted_at IS NULL
                AND t.txn_date IS NOT NULL
                {seen_by}
                AND (
                    (ec.check_eft_num IS NOT NULL AND t.eft_1 = ec.check_eft_num)
                    OR (ec.check_eft_num IS NOT NULL AND t.eft_2 = ec.check_eft_num)
                    OR (ec.check_eft_num IS NOT NULL AND t.check_reference = ec.check_eft_num)
                )
          )
        """,
        params,
    )


def get_mail_checks_in_hand(
    conn: psycopg.Connection,
    *,
    as_of: date,
) -> list[dict[str, Any]]:
    """Mail checks recognized but not yet posted to the tracker date."""
    return client.fetchall(
        conn,
        """
        SELECT deposit_id, amount, check_date_recognized, bank_posting_date,
               description, eft_1, channel
        FROM billing.bank_deposit
        WHERE channel IN ('mail_check', 'mail_sheet')
          AND COALESCE(check_date_recognized, bank_posting_date) <= %s
          AND (
              bank_posting_date IS NULL
              OR bank_posting_date > %s
          )
        """,
        (as_of, as_of),
    )


def add_tracked_ref(
    out: dict[str, date | None],
    key: object,
    deposit_date: date | None,
) -> None:
    """Record a check/EFT ref → earliest known deposit date."""
    if key is None:
        return
    text = str(key).strip()
    if not text:
        return
    current = out.get(text)
    if current is None or (deposit_date is not None and deposit_date < current):
        out[text] = deposit_date


def get_tracked_eft_refs(conn: psycopg.Connection) -> dict[str, date | None]:
    """Map EFT/check refs → deposit posting date (tracker + checks-deposits)."""
    out: dict[str, date | None] = {}
    deposits = client.fetchall(
        conn,
        """
        SELECT eft_1, eft_2, eft_last4, bank_posting_date, check_date_recognized
        FROM billing.bank_deposit
        """,
    )
    for r in deposits:
        d = r.get("bank_posting_date") or r.get("check_date_recognized")
        for key in (r.get("eft_1"), r.get("eft_2"), r.get("eft_last4")):
            add_tracked_ref(out, key, d)

    tracker_rows = client.fetchall(
        conn,
        """
        SELECT eft_1, eft_2, check_reference, txn_date
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL
        """,
    )
    for r in tracker_rows:
        d = r.get("txn_date")
        for key in (r.get("eft_1"), r.get("eft_2"), r.get("check_reference")):
            add_tracked_ref(out, key, d)
    return out


def get_eob_deposit_leadtime(
    conn: psycopg.Connection,
    *,
    before: date | None = None,
) -> list[dict[str, Any]]:
    """Matched EOB→tracker lead days per payor (txn_date − eob/check date).

    Pass ``before`` (as_of) in backtests so future tracker matches do not leak.
    Production omits ``before`` and uses full history.
    """
    extra = ""
    params: tuple[Any, ...] = ()
    if before is not None:
        extra = """
              AND t.txn_date <= %s
              AND COALESCE(ec.eob_date, ec.check_date) <= %s
        """
        params = (before, before)
    return client.fetchall(
        conn,
        f"""
        WITH matched AS (
            SELECT
                lower(btrim(COALESCE(ec.payor_raw, ''))) AS payor,
                COALESCE(ec.paid_amount_sum, 0)::float AS dollars,
                (t.txn_date - COALESCE(ec.eob_date, ec.check_date)) AS lead_days
            FROM billing.eob_check ec
            JOIN billing.transaction_tracker_row t
              ON t.deleted_at IS NULL
             AND t.txn_date IS NOT NULL
             AND ec.check_eft_num IS NOT NULL
             AND (
                    t.eft_1 = ec.check_eft_num
                 OR t.eft_2 = ec.check_eft_num
                 OR t.check_reference = ec.check_eft_num
             )
            WHERE COALESCE(ec.eob_date, ec.check_date) IS NOT NULL
              AND COALESCE(ec.paid_amount_sum, 0) > 0
              {extra}
        )
        SELECT
            payor,
            COUNT(*)::int AS n_matched,
            SUM(dollars) AS matched_dollars,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY lead_days) AS median_lead_days,
            SUM(dollars) FILTER (WHERE lead_days >= 1)
                / NULLIF(SUM(dollars), 0) AS share_lead_ge_1
        FROM matched
        GROUP BY payor
        ORDER BY matched_dollars DESC
        """,
        params,
    )


def audit_tracker_entry_lag(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per txn_date: first created_at vs txn_date (availability lag, not bank lag)."""
    return client.fetchall(
        conn,
        """
        SELECT
            txn_date AS period,
            MIN(created_at) AS first_entered_at,
            (MIN(created_at)::date - txn_date) AS lag_days,
            SUM(amount)::float AS dollars,
            COUNT(*)::int AS n_rows
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL AND txn_date IS NOT NULL
        GROUP BY txn_date
        ORDER BY txn_date
        """,
    )


def audit_tracker_created_at_degeneracy(conn: psycopg.Connection) -> dict[str, Any]:
    """Detect bulk-reload created_at (one calendar day owns most dollars)."""
    row = client.fetchone(
        conn,
        """
        WITH by_day AS (
            SELECT created_at::date AS entered_date,
                   COUNT(*)::int AS n_rows,
                   SUM(amount)::float AS dollars
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL
            GROUP BY 1
        ),
        tot AS (
            SELECT COALESCE(SUM(n_rows), 0)::int AS n_rows,
                   COALESCE(SUM(dollars), 0)::float AS dollars,
                   COUNT(*)::int AS n_entered_dates
            FROM by_day
        ),
        top AS (
            SELECT entered_date, n_rows, dollars
            FROM by_day
            ORDER BY dollars DESC NULLS LAST
            LIMIT 1
        ),
        lags AS (
            SELECT (created_at::date - txn_date) AS lag_days, amount
            FROM billing.transaction_tracker_row
            WHERE deleted_at IS NULL AND txn_date IS NOT NULL AND created_at IS NOT NULL
        )
        SELECT
            tot.n_rows,
            tot.dollars,
            tot.n_entered_dates,
            top.entered_date AS top_entered_date,
            top.n_rows AS top_day_rows,
            top.dollars AS top_day_dollars,
            top.dollars / NULLIF(tot.dollars, 0) AS top_day_dollar_share,
            (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_days) FROM lags) AS p50_lag_days,
            (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY lag_days) FROM lags) AS p90_lag_days,
            (SELECT SUM(amount) FILTER (WHERE lag_days <= 0) / NULLIF(SUM(amount), 0) FROM lags) AS share_lag_le_0,
            (SELECT SUM(amount) FILTER (WHERE lag_days <= 1) / NULLIF(SUM(amount), 0) FROM lags) AS share_lag_le_1
        FROM tot
        LEFT JOIN top ON TRUE
        """,
    )
    return dict(row) if row else {}


def audit_eob_etl_availability(conn: psycopg.Connection) -> dict[str, Any]:
    """EOB availability vs eob/check date via etl.etl_run timestamps."""
    row = client.fetchone(
        conn,
        """
        WITH joined AS (
            SELECT
                COALESCE(ec.eob_date, ec.check_date) AS origin,
                COALESCE(ec.paid_amount_sum, 0)::float AS dollars,
                ec.etl_run_id IS NOT NULL AS has_etl,
                COALESCE(r.finished_at, r.started_at) AS available_at
            FROM billing.eob_check ec
            LEFT JOIN etl.etl_run r ON r.etl_run_id = ec.etl_run_id
            WHERE COALESCE(ec.eob_date, ec.check_date) IS NOT NULL
              AND COALESCE(ec.paid_amount_sum, 0) > 0
        )
        SELECT
            COUNT(*)::int AS n_checks,
            SUM(dollars) AS dollars,
            AVG(has_etl::int)::float AS share_with_etl_run,
            SUM(dollars) FILTER (WHERE has_etl) / NULLIF(SUM(dollars), 0) AS dollar_share_with_etl,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY (available_at::date - origin)
            ) FILTER (WHERE available_at IS NOT NULL) AS p50_avail_lag_days,
            percentile_cont(0.9) WITHIN GROUP (
                ORDER BY (available_at::date - origin)
            ) FILTER (WHERE available_at IS NOT NULL) AS p90_avail_lag_days
        FROM joined
        """,
    )
    return dict(row) if row else {}


def get_tracker_availability_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Tracker dollars by txn_date × created_at date (PIT join key)."""
    return client.fetchall(
        conn,
        """
        SELECT
            txn_date AS period,
            created_at::date AS available_date,
            SUM(amount)::float AS amount
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL AND txn_date IS NOT NULL AND created_at IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
    )


def _norm_eft(ref: object) -> str:
    compact = re.sub(r"[\s\-]", "", str(ref or "").strip().upper())
    if compact.isdigit():
        compact = compact.lstrip("0") or "0"
    return compact


def _revflow_label_for_hit(hit: Any, score_text: str) -> str:
    """Map a registry hit to the closest RevFlow payor, not always orgs[0]."""
    from cashflow_reconcile.payer_registry import org_by_code

    org = org_by_code(getattr(hit, "code", "") or "")
    if org is None or not org.revflow_payors:
        name = str(getattr(hit, "name", "") or "").strip()
        return name.lower() if name else "__unmatched__"
    tokens = [t for t in re.findall(r"[a-z0-9]+", (score_text or "").lower()) if len(t) >= 3]
    best = org.revflow_payors[0]
    best_score = -1
    for payor in org.revflow_payors:
        pl = payor.lower()
        score = sum(1 for t in tokens if t in pl)
        if score > best_score:
            best_score = score
            best = payor
    return str(best).strip().lower()


def assign_tracker_payor(
    row: dict[str, Any],
    eob_index: dict[str, str],
    *,
    amount_date_index: dict[float, list[dict[str, Any]]] | None = None,
) -> str:
    """Resolve a tracker row to a payor: exact EOB, normalized EFT, description, ACH head."""
    from cashflow_reconcile.payer_registry import (
        ach_payer_resolve_texts,
        extract_eft_refs_from_description,
        is_ach_processor,
        resolve_tracker_description,
    )

    existing = str(row.get("payor") or "").strip()
    if existing and existing != "__unmatched__":
        return existing.lower()
    refs = [row.get("eft_1"), row.get("eft_2"), row.get("check_reference")]
    desc = str(row.get("description") or "")
    refs.extend(extract_eft_refs_from_description(desc))
    for ref in refs:
        raw = str(ref or "").strip()
        if not raw:
            continue
        for key in (raw, raw.upper(), _norm_eft(raw)):
            payor = eob_index.get(key)
            if payor:
                return payor.lower()
        if len(raw) == 4:
            payor = eob_index.get(f"last4:{raw}")
            if payor:
                return payor.lower()
        compact = _norm_eft(raw)
        if len(compact) >= 4:
            payor = eob_index.get(f"last4:{compact[-4:]}")
            if payor:
                return payor.lower()
    hit = resolve_tracker_description(desc)
    if hit is not None and not is_ach_processor(hit):
        score = " ".join(ach_payer_resolve_texts(desc)) or desc
        return _revflow_label_for_hit(hit, score)
    if amount_date_index:
        amt = round(float(row.get("amount") or 0), 2)
        txn = row.get("txn_date") or row.get("period")
        hits = []
        for cand in amount_date_index.get(amt, []):
            origin = cand.get("origin_date")
            if txn is not None and origin is not None:
                try:
                    delta = abs((txn - origin).days)
                except TypeError:
                    delta = 0
                if delta > 7:
                    continue
            hits.append(cand)
        if len(hits) > 1:
            tracker_efts = {_norm_eft(r) for r in refs if r}
            tails = {e[-4:] for e in tracker_efts if len(e) >= 4}
            narrowed = []
            for cand in hits:
                eft = _norm_eft(cand.get("check_eft_num"))
                if eft and (eft in tracker_efts or (len(eft) >= 4 and eft[-4:] in tails)):
                    narrowed.append(cand)
            if len(narrowed) == 1:
                hits = narrowed
        if len(hits) == 1:
            payor = str(hits[0].get("payor_raw") or "").strip()
            if payor:
                return payor.lower()
    return "__unmatched__"


def get_tracker_payer_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Tracker dollars by txn_date × payor.

    Exact EFT match to ``eob_check`` first; unmatched rows retry normalized
    refs, description TRN segments, ACH payer head (registry), then a unique
    amount+date fallback. Remaining dollars stay in ``__unmatched__``.
    """
    lines = client.fetchall(
        conn,
        """
        SELECT
            t.txn_date AS period,
            t.amount,
            t.eft_1,
            t.eft_2,
            t.check_reference,
            t.description,
            LOWER(BTRIM(m.payor_raw)) AS payor
        FROM billing.transaction_tracker_row t
        LEFT JOIN LATERAL (
            SELECT ec.payor_raw
            FROM billing.eob_check ec
            WHERE ec.check_eft_num IS NOT NULL
              AND (
                    t.eft_1 = ec.check_eft_num
                 OR t.eft_2 = ec.check_eft_num
                 OR t.check_reference = ec.check_eft_num
              )
            ORDER BY COALESCE(ec.eob_date, ec.check_date) DESC NULLS LAST
            LIMIT 1
        ) m ON TRUE
        WHERE t.deleted_at IS NULL AND t.txn_date IS NOT NULL
        """,
    )
    eob_rows = client.fetchall(
        conn,
        """
        SELECT check_eft_num, payor_raw, paid_amount_sum,
               COALESCE(eob_date, check_date) AS origin_date
        FROM billing.eob_check
        WHERE COALESCE(paid_amount_sum, 0) > 0
        """,
    )
    index: dict[str, str] = {}
    by_amt: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in eob_rows:
        payor = str(row.get("payor_raw") or "").strip()
        if payor:
            raw = str(row.get("check_eft_num") or "").strip()
            for key in (raw, raw.upper(), _norm_eft(raw)):
                if key and key not in index:
                    index[key] = payor
            if len(raw) >= 4 and f"last4:{raw[-4:]}" not in index:
                index[f"last4:{raw[-4:]}"] = payor
        by_amt[round(float(row.get("paid_amount_sum") or 0), 2)].append(row)
    agg: dict[tuple[Any, str], dict[str, float]] = defaultdict(lambda: {"amount": 0.0, "n_rows": 0})
    for line in lines:
        payor = assign_tracker_payor(line, index, amount_date_index=by_amt)
        period = line.get("period")
        key = (period, payor)
        agg[key]["amount"] += float(line.get("amount") or 0)
        agg[key]["n_rows"] += 1
    out = [
        {"period": period, "payor": payor, "amount": rec["amount"], "n_rows": int(rec["n_rows"])}
        for (period, payor), rec in sorted(agg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
    ]
    return out


def get_eob_availability_daily(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """EOB dollars by origin date × payor × etl availability date."""
    return client.fetchall(
        conn,
        """
        SELECT
            COALESCE(ec.eob_date, ec.check_date) AS period,
            LOWER(BTRIM(COALESCE(ec.payor_raw, ''))) AS ins_name,
            COALESCE(r.finished_at, r.started_at)::date AS available_date,
            SUM(COALESCE(ec.paid_amount_sum, 0))::float AS amount
        FROM billing.eob_check ec
        LEFT JOIN etl.etl_run r ON r.etl_run_id = ec.etl_run_id
        WHERE COALESCE(ec.eob_date, ec.check_date) IS NOT NULL
          AND COALESCE(ec.paid_amount_sum, 0) > 0
        GROUP BY 1, 2, 3
        ORDER BY 1, 2
        """,
    )


def count_patient_payments(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.patient_payment")
    return int(row["n"]) if row else 0


def list_checks_deposits_sheet(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Cashed-check sheet rows in bank_deposit (source_system=checks_deposits)."""
    return client.fetchall(
        conn,
        """
        SELECT
            COALESCE(bank_posting_date, check_date_recognized) AS deposit_date,
            amount,
            eft_1 AS check_number,
            description AS payer
        FROM billing.bank_deposit
        WHERE source_system = 'checks_deposits'
          AND COALESCE(bank_posting_date, check_date_recognized) IS NOT NULL
          AND COALESCE(amount, 0) > 0
        """,
    )


def count_eob_checks(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.eob_check")
    return int(row["n"]) if row else 0


def count_bank_deposits(conn: psycopg.Connection) -> int:
    row = client.fetchone(conn, "SELECT COUNT(*)::int AS n FROM billing.bank_deposit")
    return int(row["n"]) if row else 0


def list_tracker_for_match(
    conn: psycopg.Connection,
    *,
    as_of: date,
    lookback_days: int = 40,
) -> list[dict[str, Any]]:
    """Tracker rows observable on ``as_of`` (created_at date, not a later edit)."""
    start = as_of - timedelta(days=lookback_days)
    return client.fetchall(
        conn,
        """
        SELECT
            row_id::text AS row_id,
            txn_date,
            amount::float AS amount,
            eft_1,
            eft_2,
            check_reference,
            description,
            created_at::date AS observed_at
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL
          AND txn_date >= %s
          AND created_at::date <= %s
          AND COALESCE(amount, 0) > 0
        """,
        (start, as_of),
    )


def patient_cash_daily(
    conn: psycopg.Connection,
    *,
    as_of: date,
    weeks: int = 8,
) -> list[tuple[date, float]]:
    """Clinic cash/card by bank day. Insurance ACH and sheet deposits stay out."""
    start = as_of - timedelta(days=7 * weeks)
    rows = client.fetchall(
        conn,
        """
        SELECT txn_date, SUM(amount)::float AS amount
        FROM billing.transaction_tracker_row
        WHERE deleted_at IS NULL
          AND txn_date >= %s
          AND txn_date <= %s
          AND COALESCE(amount, 0) > 0
          AND (
            lower(coalesce(description, '')) ~ 'visa|cash|copay|patient pay|credit card|patient payment'
            OR lower(coalesce(transaction_type, '')) ~ 'visa|cash|copay|patient'
          )
        GROUP BY txn_date
        """,
        (start, as_of),
    )
    out: list[tuple[date, float]] = []
    for row in rows:
        day = row.get("txn_date")
        if isinstance(day, date):
            out.append((day, float(row.get("amount") or 0)))
    return out
