"""CPT audit work-queue repository."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from cashflow_db.repository import client
from cashflow_db.repository.eligibility import SHEET_PAID_VISIT_EXISTS_SQL

IGNORE_REASONS = (
    "clinical_exception",
    "payer_exception",
    "false_positive",
    "duplicate",
    "already_corrected_external",
    "not_billable",
    "other",
)
WORKFLOW = ("open", "in_progress", "resolved", "ignored")
OPEN_RISK_WORKFLOW = ("open", "in_progress")
OPEN_RISK_DOMAINS = ("cpt", "icd", "demo")
CLINIC_CLUSTERS = (
    "Cluster 1: Queens Cluster",
    "Cluster 2: Manhattan Cluster",
    "Cluster 3: Lower Brooklyn Cluster",
    "Cluster 4: Bronx Cluster",
    "Cluster 5: Upper Brooklyn Cluster",
)
_FACILITY_CLUSTER_JOIN = """
        LEFT JOIN LATERAL (
            SELECT clinic_cluster
            FROM ref.facility
            WHERE name = f.facility_name
            LIMIT 1
        ) rf ON true
"""

SHEET_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dos", "DOS"),
    ("note_signed_at", "Signed at"),
    ("note_signed_by", "Signed by"),
    ("patient_name", "Patient"),
    ("webpt_patient_id", "Patient ID"),
    ("facility_name", "Facility"),
    ("clinic_cluster", "Cluster"),
    ("insurance_name", "Insurance"),
    ("audit_domain", "Type"),
    ("rule_code", "Rule"),
    ("bucket", "Bucket"),
    ("severity", "Severity"),
    ("estimated_opportunity", "Opportunity $"),
    ("exposure_amount", "Exposure $"),
    ("claim_status", "Claim"),
    ("workflow_status", "Status"),
    ("cpt_codes", "CPT codes"),
    ("icd_codes", "ICD codes"),
    ("detail", "Detail"),
    ("expected_value", "Expected"),
    ("found_value", "Found"),
    ("demo_field", "Demo field"),
    ("demo_edoc", "eDoc"),
    ("demo_chart", "Chart"),
    ("demo_note", "Daily note"),
    ("ignore_reason", "Ignore reason"),
    ("resolution_note", "Resolution note"),
    ("assigned_to_name", "Assigned to"),
)

_DOMAIN_LABELS = {"cpt": "CPT", "icd": "ICD-10", "demo": "Demographics"}


def _domain_label(domain: Any) -> str:
    key = str(domain or "cpt").strip().lower()
    return _DOMAIN_LABELS.get(key, "CPT")


def _demo_export_fields(row: dict[str, Any]) -> dict[str, Any]:
    if str(row.get("audit_domain") or "cpt").strip().lower() != "demo":
        return {
            "demo_field": None,
            "demo_edoc": None,
            "demo_chart": None,
            "demo_note": None,
        }
    snap = row.get("rule_snapshot") or {}
    if not isinstance(snap, dict):
        return {
            "demo_field": None,
            "demo_edoc": row.get("expected_value"),
            "demo_chart": None,
            "demo_note": None,
        }
    edoc = snap.get("edoc") if isinstance(snap.get("edoc"), dict) else {}
    chart = snap.get("chart") if isinstance(snap.get("chart"), dict) else {}
    note = snap.get("note") if isinstance(snap.get("note"), dict) else {}
    return {
        "demo_field": snap.get("field"),
        "demo_edoc": edoc.get("value") or row.get("expected_value"),
        "demo_chart": chart.get("value"),
        "demo_note": note.get("value"),
    }


def _export_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def sheet_export_headers() -> list[str]:
    return [label for _key, label in SHEET_EXPORT_COLUMNS]


def sheet_export_row(row: dict[str, Any]) -> list[Any]:
    demo = _demo_export_fields(row)
    out: list[Any] = []
    for key, _label in SHEET_EXPORT_COLUMNS:
        if key == "audit_domain":
            out.append(_domain_label(row.get("audit_domain")))
            continue
        if key in demo:
            out.append(_export_cell(demo[key]))
            continue
        out.append(_export_cell(row.get(key)))
    return out


def _filters(
    *,
    q: str | None,
    bucket: list[str] | None,
    rule_code: list[str] | None,
    insurance: list[str] | None,
    facility: list[str] | None,
    workflow_status: list[str] | None,
    claim_status: list[str] | None,
    days: int,
    include_stale: bool,
    audit_domain: list[str] | None = None,
    signed_on: date | None = None,
    cluster: list[str] | None = None,
) -> tuple[str, list[Any]]:
    clauses = ["f.dos >= CURRENT_DATE - (%s::int)"]
    params: list[Any] = [int(days)]
    if not include_stale:
        clauses.append("f.is_stale = false")
    if q:
        clauses.append(
            """(
                f.patient_name ILIKE %s
                OR f.webpt_patient_id ILIKE %s
                OR f.insurance_name ILIKE %s
                OR f.rule_code ILIKE %s
                OR f.detail ILIKE %s
            )"""
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if bucket:
        clauses.append("f.bucket = ANY(%s)")
        params.append(bucket)
    if rule_code:
        clauses.append("f.rule_code = ANY(%s)")
        params.append(rule_code)
    if insurance:
        clauses.append("f.insurance_name = ANY(%s)")
        params.append(insurance)
    if facility:
        clauses.append("f.facility_name = ANY(%s)")
        params.append(facility)
    if workflow_status:
        clauses.append("COALESCE(w.workflow_status, 'open') = ANY(%s)")
        params.append(workflow_status)
    if claim_status:
        clauses.append("f.claim_status = ANY(%s)")
        params.append(claim_status)
    if audit_domain:
        clauses.append("COALESCE(f.audit_domain, 'cpt') = ANY(%s)")
        params.append(audit_domain)
    if signed_on:
        clauses.append("(f.note_signed_at AT TIME ZONE 'US/Eastern')::date = %s")
        params.append(signed_on)
    if cluster:
        clauses.append(
            """EXISTS (
                SELECT 1 FROM ref.facility rf_cluster
                WHERE rf_cluster.name = f.facility_name
                  AND rf_cluster.clinic_cluster = ANY(%s)
            )"""
        )
        params.append(cluster)
    return " AND ".join(clauses), params


def _open_risk_where(
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Live audit-queue risk: open/in_progress, not stale, all three domains."""
    clauses = [
        "f.is_stale = false",
        "f.bucket = 'risk'",
        "COALESCE(f.audit_domain, 'cpt') = ANY(%s)",
        "COALESCE(w.workflow_status, 'open') = ANY(%s)",
        SHEET_PAID_VISIT_EXISTS_SQL,
    ]
    params: list[Any] = [list(OPEN_RISK_DOMAINS), list(OPEN_RISK_WORKFLOW)]
    if d0:
        clauses.append("f.dos >= %s")
        params.append(d0)
    if d1:
        clauses.append("f.dos <= %s")
        params.append(d1)
    if facilities:
        clauses.append("f.facility_name = ANY(%s)")
        params.append(facilities)
    if insurers:
        clauses.append("f.insurance_name = ANY(%s)")
        params.append(insurers)
    return " AND ".join(clauses), params


def _share_pct(rows: list[dict[str, Any]], col: str) -> list[dict[str, Any]]:
    total = sum(float(r.get(col) or 0) for r in rows)
    for row in rows:
        amt = float(row.get(col) or 0)
        row[col] = round(amt, 2)
        row["share_pct"] = round(100.0 * amt / total, 1) if total else 0.0
    return rows


def _merge_risk_insurance(
    audit: list[dict[str, Any]],
    denied: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from cashflow_db.repository.insurance import insurance_names_match, usable_insurance_name

    rows: list[dict[str, Any]] = []

    def _find(name: str) -> dict[str, Any] | None:
        for row in rows:
            if insurance_names_match(str(row["ins_name"]), name):
                return row
        return None

    def _add(name: str, exposure: float, visits: int) -> None:
        label = usable_insurance_name(name) or str(name or "").strip() or "(blank)"
        row = _find(label)
        if row is None:
            row = {"ins_name": label, "exposure_amount": 0.0, "visit_count": 0}
            rows.append(row)
        row["exposure_amount"] = round(float(row["exposure_amount"]) + float(exposure or 0), 2)
        row["visit_count"] = int(row["visit_count"] or 0) + int(visits or 0)

    for item in audit:
        _add(
            str(item.get("ins_name") or ""),
            float(item.get("exposure_amount") or 0),
            int(item.get("visit_count") or 0),
        )
    for item in denied:
        _add(
            str(item.get("ins_name") or ""),
            float(item.get("exposure_amount") or 0),
            int(item.get("visit_count") or 0),
        )
    rows.sort(key=lambda r: float(r["exposure_amount"]), reverse=True)
    return _share_pct(rows, "exposure_amount")


def open_risk_exposure(
    conn: psycopg.Connection,
    *,
    d0: date | None = None,
    d1: date | None = None,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
) -> dict[str, Any]:
    """Open audit-queue risk $ plus Collection Denied charged (not warnings)."""
    from cashflow_db.repository.eligibility import (
        collection_denied_exposure,
        collection_denied_visit_sql,
    )

    where, params = _open_risk_where(
        d0=d0, d1=d1, facilities=facilities, insurers=insurers
    )
    from_sql = """
        FROM billing.cpt_audit_finding f
        LEFT JOIN ops.cpt_audit_work_item w ON w.finding_id = f.finding_id
        WHERE {where}
    """.format(where=where)
    totals = client.fetchone(
        conn,
        f"""
        SELECT
            COALESCE(SUM(f.exposure_amount), 0) AS exposure_amount,
            COUNT(DISTINCT (COALESCE(f.webpt_patient_id, ''), f.dos))
                FILTER (WHERE COALESCE(f.exposure_amount, 0) > 0)::int AS visit_count
        {from_sql}
        """,
        params,
    )
    by_insurance = client.fetchall(
        conn,
        f"""
        SELECT
            COALESCE(NULLIF(BTRIM(f.insurance_name), ''), '(blank)') AS ins_name,
            COALESCE(SUM(f.exposure_amount), 0) AS exposure_amount,
            COUNT(DISTINCT (COALESCE(f.webpt_patient_id, ''), f.dos))
                FILTER (WHERE COALESCE(f.exposure_amount, 0) > 0)::int AS visit_count
        {from_sql}
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    )
    by_flag = client.fetchall(
        conn,
        f"""
        SELECT
            COALESCE(f.audit_domain, 'cpt') AS risk_flag,
            COALESCE(SUM(f.exposure_amount), 0) AS exposure_amount,
            COUNT(DISTINCT (COALESCE(f.webpt_patient_id, ''), f.dos))
                FILTER (WHERE COALESCE(f.exposure_amount, 0) > 0)::int AS visit_count
        {from_sql}
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    )
    denied = collection_denied_exposure(
        conn, d0=d0, d1=d1, facilities=facilities, insurers=insurers
    )
    audit_amt = round(float((totals or {}).get("exposure_amount") or 0), 2)
    denied_amt = round(float(denied.get("exposure_amount") or 0), 2)
    audit_n = int((totals or {}).get("visit_count") or 0)
    denied_n = int(denied.get("visit_count") or 0)
    overlap = client.fetchone(
        conn,
        f"""
        SELECT COUNT(*)::int AS visit_count
        FROM (
            SELECT DISTINCT COALESCE(f.webpt_patient_id, ''), f.dos
            {from_sql}
              AND COALESCE(f.exposure_amount, 0) > 0
            INTERSECT
            {collection_denied_visit_sql(str(denied.get("where_sql") or "FALSE"))}
        ) overlap
        """,
        [*params, *list(denied.get("params") or [])],
    )
    overlap_n = int((overlap or {}).get("visit_count") or 0)
    flags = list(by_flag or [])
    if denied_amt > 0 or denied_n > 0:
        flags.append(
            {
                "risk_flag": "denied",
                "exposure_amount": denied_amt,
                "visit_count": denied_n,
            }
        )
    flags.sort(key=lambda r: float(r.get("exposure_amount") or 0), reverse=True)
    return {
        "exposure_amount": round(audit_amt + denied_amt, 2),
        "visit_count": max(audit_n + denied_n - overlap_n, 0),
        "by_insurance": _merge_risk_insurance(list(by_insurance or []), list(denied.get("by_insurance") or [])),
        "by_flag": _share_pct(flags, "exposure_amount"),
    }


def list_queue(
    conn: psycopg.Connection,
    *,
    q: str | None = None,
    bucket: list[str] | None = None,
    rule_code: list[str] | None = None,
    insurance: list[str] | None = None,
    facility: list[str] | None = None,
    workflow_status: list[str] | None = None,
    claim_status: list[str] | None = None,
    audit_domain: list[str] | None = None,
    days: int = 30,
    page: int = 1,
    page_size: int = 50,
    include_stale: bool = False,
    signed_on: date | None = None,
    cluster: list[str] | None = None,
) -> dict[str, Any]:
    where, params = _filters(
        q=q,
        bucket=bucket,
        rule_code=rule_code,
        insurance=insurance,
        facility=facility,
        workflow_status=workflow_status,
        claim_status=claim_status,
        audit_domain=audit_domain,
        days=days,
        include_stale=include_stale,
        signed_on=signed_on,
        cluster=cluster,
    )
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 200)
    offset = (page - 1) * page_size
    total_row = client.fetchone(
        conn,
        f"""
        SELECT COUNT(*)::int AS n
        FROM billing.cpt_audit_finding f
        LEFT JOIN ops.cpt_audit_work_item w ON w.finding_id = f.finding_id
        WHERE {where}
        """,
        params,
    )
    items = client.fetchall(
        conn,
        f"""
        SELECT
            f.*,
            rf.clinic_cluster,
            w.work_item_id,
            COALESCE(w.workflow_status, 'open') AS workflow_status,
            w.resolution_code,
            w.resolution_note,
            w.ignore_reason,
            w.assigned_to,
            au.display_name AS assigned_to_name,
            w.resolved_at,
            w.updated_at AS workflow_updated_at
        FROM billing.cpt_audit_finding f
        {_FACILITY_CLUSTER_JOIN}
        LEFT JOIN ops.cpt_audit_work_item w ON w.finding_id = f.finding_id
        LEFT JOIN auth.app_user au ON au.user_id = w.assigned_to
        WHERE {where}
        ORDER BY COALESCE(w.updated_at, f.updated_at) DESC, f.dos DESC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    )
    return {
        "items": items,
        "total": int(total_row["n"]) if total_row else 0,
        "page": page,
        "page_size": page_size,
        "days": days,
    }


def get_item(conn: psycopg.Connection, work_item_id: str) -> dict[str, Any] | None:
    return client.fetchone(
        conn,
        f"""
        SELECT
            f.*,
            rf.clinic_cluster,
            w.work_item_id,
            COALESCE(w.workflow_status, 'open') AS workflow_status,
            w.resolution_code,
            w.resolution_note,
            w.ignore_reason,
            w.assigned_to,
            au.display_name AS assigned_to_name,
            w.resolved_at,
            w.resolved_by,
            w.updated_at AS workflow_updated_at
        FROM ops.cpt_audit_work_item w
        JOIN billing.cpt_audit_finding f ON f.finding_id = w.finding_id
        {_FACILITY_CLUSTER_JOIN}
        LEFT JOIN auth.app_user au ON au.user_id = w.assigned_to
        WHERE w.work_item_id = %s::uuid
        """,
        (work_item_id,),
    )


def list_item_history(conn: psycopg.Connection, work_item_id: str) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT h.*, u.display_name AS changed_by_name
        FROM ops.cpt_audit_work_history h
        LEFT JOIN auth.app_user u ON u.user_id = h.changed_by
        WHERE h.work_item_id = %s::uuid
        ORDER BY h.changed_at DESC
        LIMIT 100
        """,
        (work_item_id,),
    )


def filter_options(conn: psycopg.Connection, *, days: int = 30) -> dict[str, Any]:
    rows_ins = client.fetchall(
        conn,
        """
        SELECT DISTINCT insurance_name AS v
        FROM billing.cpt_audit_finding
        WHERE dos >= CURRENT_DATE - (%s::int) AND insurance_name IS NOT NULL
        ORDER BY 1
        """,
        (days,),
    )
    rows_fac = client.fetchall(
        conn,
        """
        SELECT DISTINCT facility_name AS v
        FROM billing.cpt_audit_finding
        WHERE dos >= CURRENT_DATE - (%s::int) AND facility_name IS NOT NULL
        ORDER BY 1
        """,
        (days,),
    )
    rows_rule = client.fetchall(
        conn,
        """
        SELECT DISTINCT rule_code AS v
        FROM billing.cpt_audit_finding
        WHERE dos >= CURRENT_DATE - (%s::int)
        ORDER BY 1
        """,
        (days,),
    )
    return {
        "insurance": [r["v"] for r in rows_ins if r.get("v")],
        "facility": [r["v"] for r in rows_fac if r.get("v")],
        "rule_code": [r["v"] for r in rows_rule if r.get("v")],
        "bucket": ["warning", "risk"],
        "audit_domain": ["cpt", "icd", "demo"],
        "workflow_status": list(WORKFLOW),
        "claim_status": ["open", "paid", "denied", "rejected", "zero_pay"],
        "ignore_reasons": list(IGNORE_REASONS),
        "cluster": list(CLINIC_CLUSTERS),
    }


def patch_work_item(
    conn: psycopg.Connection,
    work_item_id: str,
    changes: dict[str, Any],
    *,
    actor_user_id: str | None,
) -> dict[str, Any]:
    current = get_item(conn, work_item_id)
    if not current:
        raise KeyError("not found")
    allowed = {
        "workflow_status",
        "resolution_code",
        "resolution_note",
        "ignore_reason",
        "assigned_to",
    }
    diffs: list[tuple[str, Any, Any]] = []
    for field, new_val in changes.items():
        if field not in allowed:
            continue
        old = current.get(field)
        if str(old or "") == str(new_val or ""):
            continue
        diffs.append((field, old, new_val))
    status = changes.get("workflow_status", current.get("workflow_status"))
    ignore_reason = changes.get("ignore_reason", current.get("ignore_reason"))
    if status == "ignored" and not ignore_reason:
        raise ValueError("ignore_reason required when ignoring")
    if status and status not in WORKFLOW:
        raise ValueError("invalid workflow_status")
    if ignore_reason and ignore_reason not in IGNORE_REASONS:
        raise ValueError("invalid ignore_reason")
    if not diffs:
        return current
    sets = ["updated_at = now()", "updated_by = %s::uuid"]
    params: list[Any] = [actor_user_id]
    for field, _old, new_val in diffs:
        if field == "assigned_to":
            sets.append("assigned_to = %s::uuid")
            sets.append("assigned_at = CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() END")
            params.extend([new_val, new_val])
        else:
            sets.append(f"{field} = %s")
            params.append(new_val)
    if status in ("resolved", "ignored") and current.get("workflow_status") not in (
        "resolved",
        "ignored",
    ):
        sets.append("resolved_at = now()")
        sets.append("resolved_by = %s::uuid")
        params.append(actor_user_id)
    params.append(work_item_id)
    client.execute(
        conn,
        f"""
        UPDATE ops.cpt_audit_work_item
        SET {', '.join(sets)}
        WHERE work_item_id = %s::uuid
        """,
        params,
    )
    for field, old, new_val in diffs:
        client.execute(
            conn,
            """
            INSERT INTO ops.cpt_audit_work_history (
                work_item_id, column_name, old_value, new_value, changed_by
            ) VALUES (%s::uuid, %s, %s, %s, %s::uuid)
            """,
            (work_item_id, field, str(old) if old is not None else None, str(new_val) if new_val is not None else None, actor_user_id),
        )
    row = get_item(conn, work_item_id)
    assert row
    return row


def insight_totals(
    conn: psycopg.Connection,
    *,
    published_only_headline: bool = True,
) -> dict[str, Any]:
    settings = client.fetchone(
        conn,
        "SELECT insights_published, published_at FROM analytics.cpt_insight_settings WHERE settings_id = 1",
    )
    published = bool(settings and settings.get("insights_published"))
    agg = client.fetchone(
        conn,
        """
        SELECT
            COUNT(*) FILTER (WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt')::int AS findings,
            COUNT(*) FILTER (WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND bucket = 'warning')::int AS warnings,
            COUNT(*) FILTER (WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND bucket = 'risk')::int AS risks,
            COALESCE(SUM(estimated_opportunity) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND impact_type = 'opportunity'
                  AND estimation_confidence IN ('high', 'medium')
            ), 0) AS opportunity_high_medium,
            COALESCE(SUM(estimated_opportunity) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND impact_type = 'opportunity'
                  AND estimation_method = 'heuristic'
            ), 0) AS opportunity_heuristic,
            COALESCE(SUM(estimated_opportunity) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND impact_type = 'opportunity'
            ), 0) AS opportunity_all,
            COALESCE(SUM(exposure_amount) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND bucket = 'risk' AND claim_status = 'paid'
            ), 0) AS at_risk_paid,
            COALESCE(SUM(exposure_amount) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND bucket = 'risk' AND claim_status IN ('open', 'rejected')
            ), 0) AS open_at_risk,
            COUNT(*) FILTER (
                WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND rule_code = 'MISSING_90901'
            )::int AS missing_90901
        FROM billing.cpt_audit_finding
        """,
    )
    leakage = client.fetchall(
        conn,
        """
        SELECT
            date_trunc('month', dos)::date AS month,
            COUNT(*)::int AS visits,
            COALESCE(SUM(estimated_opportunity) FILTER (
                WHERE estimation_confidence IN ('high', 'medium')
            ), 0) AS opportunity_high_medium,
            COALESCE(SUM(estimated_opportunity) FILTER (
                WHERE estimation_method = 'heuristic'
            ), 0) AS opportunity_heuristic
        FROM billing.cpt_audit_finding
        WHERE NOT is_stale AND COALESCE(audit_domain, 'cpt') = 'cpt' AND rule_code = 'MISSING_90901'
        GROUP BY 1
        ORDER BY 1 DESC
        LIMIT 12
        """,
    )
    models = client.fetchall(
        conn,
        """
        SELECT grain_key, grain, model_type, base_visit_amount, addons, n_visits, as_of_date
        FROM analytics.payer_payment_model
        WHERE as_of_date = (SELECT MAX(as_of_date) FROM analytics.payer_payment_model)
        ORDER BY n_visits DESC
        LIMIT 25
        """,
    )
    behavior = client.fetchall(
        conn,
        """
        SELECT payor_key, payor_raw, check_count, median_cash_velocity_days,
               median_eob_to_deposit_days, payload
        FROM analytics.payor_behavior_summary
        ORDER BY check_count DESC NULLS LAST
        LIMIT 20
        """,
    )
    headline = {
        "opportunity_high_medium": float(agg["opportunity_high_medium"] or 0) if agg else 0,
        "at_risk_paid": float(agg["at_risk_paid"] or 0) if agg else 0,
        "open_at_risk": float(agg["open_at_risk"] or 0) if agg else 0,
    }
    if published_only_headline and not published:
        headline = {k: None for k in headline}
    return {
        "insights_published": published,
        "published_at": settings.get("published_at") if settings else None,
        "headline": headline,
        "secondary": {
            "opportunity_all": float(agg["opportunity_all"] or 0) if agg else 0,
            "opportunity_heuristic": float(agg["opportunity_heuristic"] or 0) if agg else 0,
            "warnings": int(agg["warnings"] or 0) if agg else 0,
            "risks": int(agg["risks"] or 0) if agg else 0,
            "findings": int(agg["findings"] or 0) if agg else 0,
            "missing_90901": int(agg["missing_90901"] or 0) if agg else 0,
        },
        "missing_90901_by_month": leakage,
        "payer_models": models,
        "payor_behavior": behavior,
    }


def set_insights_published(
    conn: psycopg.Connection,
    published: bool,
    *,
    actor_user_id: str | None,
) -> dict[str, Any]:
    return client.fetchone(
        conn,
        """
        UPDATE analytics.cpt_insight_settings
        SET insights_published = %s,
            published_at = CASE WHEN %s THEN now() ELSE published_at END,
            published_by = CASE WHEN %s THEN %s::uuid ELSE published_by END,
            updated_at = now()
        WHERE settings_id = 1
        RETURNING *
        """,
        (published, published, published, actor_user_id),
    ) or {}
