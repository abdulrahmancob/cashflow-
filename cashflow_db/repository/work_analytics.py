"""Team work analytics: activity slices, queue volume, claim outcomes, money."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg

from cashflow_db.repository import client
from cashflow_db.repository.collection import (
    COLLECTION_STATUS_SEED,
    ROOT_CAUSE_SEED,
    fold_label,
)
from cashflow_db.repository.eligibility import (
    COLLECTION_VISIT_SQL,
    DENIED_VISIT_SQL,
    PAID_OR_DEDUCT_SQL,
)
from cashflow_db.util import parse_money

SLICE_GAP_SECONDS = 120
IDLE_CAP_SECONDS = 3 * 60 * 60
DESK_PERMISSIONS = frozenset({"watching", "prompt", "denied", "unsupported"})
SCOPE_ALL = "all"
SCOPE_OPS = "ops"
SCOPE_SS = "second_submission"

OPS_ROLE_KEYS = (
    "posting_team",
    "collector",
    "submission",
    "second_submission",
    "second_submission_lead",
    "ops_admin",
    "sub_admin",
)
SS_ROLE_KEYS = ("second_submission", "second_submission_lead")
ELIG_ROLE_KEYS = ("posting_team",)
COLLECTION_ROLE_KEYS = ("collector",)
SUBMISSION_ROLE_KEYS = ("submission",)
TEAM_SS = "second_submission"
TEAM_ELIGIBILITY = "eligibility"
TEAM_COLLECTION = "collection"
TEAM_SUBMISSION = "submission"
KNOWN_TEAMS = (TEAM_SS, TEAM_ELIGIBILITY, TEAM_COLLECTION, TEAM_SUBMISSION)
TEAM_LABELS = {
    TEAM_SS: "Second Submission",
    TEAM_ELIGIBILITY: "Eligibility",
    TEAM_COLLECTION: "Collection",
    TEAM_SUBMISSION: "Submission",
}
TEAM_ROLE_KEYS = {
    TEAM_SS: SS_ROLE_KEYS,
    TEAM_ELIGIBILITY: ELIG_ROLE_KEYS,
    TEAM_COLLECTION: COLLECTION_ROLE_KEYS,
    TEAM_SUBMISSION: SUBMISSION_ROLE_KEYS,
}
ELIG_SHEET_WHERE = f"NOT {DENIED_VISIT_SQL} AND NOT {COLLECTION_VISIT_SQL}"
COLLECTION_QUEUE_WHERE = f"({DENIED_VISIT_SQL} OR {COLLECTION_VISIT_SQL})"
COLLECTION_WORKED_SQL = """
NULLIF(btrim(COALESCE(wi.manual_overrides->>'collection_status', '')), '') IS NOT NULL
"""
COLLECTION_EDIT_COLUMNS = (
    "collection_status",
    "root_cause",
    "denial_reason",
    "actions_taken",
)
COLLECTION_STATUS_OTHER = "Other"
ROOT_CAUSE_OTHER = "Other"
COLLECTION_STATUS_LABELS = (*COLLECTION_STATUS_SEED, COLLECTION_STATUS_OTHER)
COLLECTION_SET_PAID_SQL = """
(
    h.column_name = 'collection_status'
    AND regexp_replace(lower(btrim(COALESCE(h.new_value, ''))), '[^a-z0-9]', '', 'g') = 'paid'
)
"""
COLLECTION_PAYMENT_SQL = """
COALESCE((
    SELECT COALESCE(sf.insurance_payment, 0) + COALESCE(sf.client_payment, 0)
    FROM analytics.snowflake_visit_kpi sf
    WHERE sf.emr_id = wi.emr_patient_id
      AND sf.date_of_service = wi.dos
), 0)
"""
COLLECTION_RECOVERED_TODAY_SQL = f"""
w.worked_today AND (w.set_paid_today OR ({PAID_OR_DEDUCT_SQL}))
"""
COLLECTION_RECOVERED_MONTH_SQL = f"""
w.set_paid_month OR ({PAID_OR_DEDUCT_SQL})
"""
COLLECTION_WORK_DAY_SQL = """
GREATEST(
  wi.updated_at::date,
  COALESCE(wi.completed_at::date, wi.updated_at::date)
)
"""
CPT_LIVE_DOMAIN_SQL = "COALESCE(f.audit_domain, 'cpt') <> 'demo'"
CPT_DOMAIN_SQL = "COALESCE(f.audit_domain, 'cpt')"
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

_PAID = "paid"
_DENIED = "denied"
_PENDING = "pending"
_SUBMITTED = "submitted"
_CORRECTED = "corrected"
_TFL = "timely_filing"
_NOT_ENTERED = "not_entered"
_OTHER = "other"

SS_WORKLOAD_WHERE = """
COALESCE(flag.second_submission, false)
AND NULLIF(btrim(flag.second_insurance), '') IS NOT NULL
"""
SS_NAME_FOLD = "lower(regexp_replace(btrim({col}), '\\s+', ' ', 'g'))"
SS_SUBMITTER_MATCH = f"""
(
    {SS_NAME_FOLD.format(col="flag.submitter")}
      = {SS_NAME_FOLD.format(col="su.display_name")}
 OR {SS_NAME_FOLD.format(col="flag.submitter")}
      = {SS_NAME_FOLD.format(col="su.username")}
)
"""
SS_SUBMITTER_JOIN = f"""
JOIN auth.app_user su
  ON {SS_SUBMITTER_MATCH}
 AND su.is_active
"""
SS_TEAM_JOIN = f"""
LEFT JOIN auth.app_user su
  ON {SS_SUBMITTER_MATCH}
 AND su.is_active
"""
SS_TEAM_INCLUDE_SQL = """
(
    su.user_id = ANY(%s::uuid[])
 OR flag.submission_date IS NOT NULL
)
"""
# Credit the day status was first set, not later sibling-insurance / queue
# refreshes that only bump updated_at on already-submitted rows.
SS_WORK_DAY_SQL = """
COALESCE(flag.submission_date, flag.updated_at::date)
"""
SS_DOS_SQL = "flag.dos"
SS_PAYMENT_SQL = """
COALESCE(
    (regexp_match(
        replace(COALESCE(flag.payment, ''), ',', ''),
        '-?[0-9]+(?:\\.[0-9]+)?'
    ))[1]::numeric,
    0
)
"""


def include_ss_history_row(
    *,
    submitter_user_id: str | None,
    submission_date: date | None,
    scoped_ids: list[str] | tuple[str, ...],
    member_id: str | None = None,
) -> bool:
    """All team: any SS workload row. One member: submitter only."""
    if member_id:
        return bool(submitter_user_id) and submitter_user_id == member_id
    return True


def parse_payment_amount(value: Any) -> Decimal | None:
    return parse_money(value)


def classify_workload_outcome(status: str | None) -> str:
    raw = (status or "").strip()
    if not raw:
        return _NOT_ENTERED
    key = raw.casefold()
    if key == "paid":
        return _PAID
    if key in {"denied", "not paid"}:
        return _DENIED
    if key == "pending":
        return _PENDING
    if key == "submitted":
        return _SUBMITTED
    if key == "corrected":
        return _CORRECTED
    if key == "timely filing":
        return _TFL
    return _OTHER


def empty_breakdown_row() -> dict[str, Any]:
    return {
        "payment": 0.0,
        "claims": 0,
        "total_submitted_claims": 0,
        "submitted": 0,
        "paid": 0,
        "pending": 0,
        "denied": 0,
        "corrected": 0,
        "timely_filing": 0,
    }


def total_submitted_claims(src: dict[str, Any] | None) -> int:
    row = src or {}
    return (
        _int(row.get("submitted"))
        + _int(row.get("paid"))
        + _int(row.get("pending"))
        + _int(row.get("denied"))
        + _int(row.get("corrected"))
    )


def add_ss_fact(
    metrics: dict[str, Any],
    *,
    status: str | None,
    payment: Any = 0,
    submission_date: date | None = None,
) -> dict[str, Any]:
    bucket = classify_workload_outcome(status)
    metrics["claims"] = _int(metrics.get("claims")) + 1
    metrics["payment"] = _num(metrics.get("payment")) + _num(payment)
    if bucket == _SUBMITTED or submission_date is not None:
        metrics["submitted"] = _int(metrics.get("submitted")) + 1
    if bucket == _PAID:
        metrics["paid"] = _int(metrics.get("paid")) + 1
    elif bucket == _DENIED:
        metrics["denied"] = _int(metrics.get("denied")) + 1
    elif bucket == _PENDING:
        metrics["pending"] = _int(metrics.get("pending")) + 1
    elif bucket == _CORRECTED:
        metrics["corrected"] = _int(metrics.get("corrected")) + 1
    elif bucket == _TFL:
        metrics["timely_filing"] = _int(metrics.get("timely_filing")) + 1
    return metrics


def iter_year_weeks(year: int) -> list[date]:
    start = date(year, 1, 1) - timedelta(days=date(year, 1, 1).weekday())
    last = date(year, 12, 31)
    last_monday = last - timedelta(days=last.weekday())
    out: list[date] = []
    day = start
    while day <= last_monday:
        out.append(day)
        day += timedelta(days=7)
    return out


def iter_month_days(year: int, month: int) -> list[date]:
    n = monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


def _sum_breakdown(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out = empty_breakdown_row()
    for key in out:
        if key == "payment":
            out[key] = _num(left.get(key)) + _num(right.get(key))
        else:
            out[key] = _int(left.get(key)) + _int(right.get(key))
    return out


def period_start_for(work_day: date, grain: str) -> date:
    kind = (grain or "month").strip().lower()
    if kind == "day":
        return work_day
    if kind == "week":
        return work_day - timedelta(days=work_day.weekday())
    return work_day.replace(day=1)


def parse_breakdown_month(month: str | int | None, year: int) -> tuple[int, int]:
    if month is None or month == "":
        return year, date.today().month
    if isinstance(month, int):
        m = month
        if m < 1 or m > 12:
            raise ValueError("month must be 1-12")
        return year, m
    raw = str(month).strip()
    if "-" in raw:
        parts = raw.split("-")
        if len(parts) < 2:
            raise ValueError("month must be YYYY-MM")
        y, m = int(parts[0]), int(parts[1])
        if m < 1 or m > 12:
            raise ValueError("month must be 1-12")
        return y, m
    m = int(raw)
    if m < 1 or m > 12:
        raise ValueError("month must be 1-12")
    return year, m


def add_ss_status_batch(
    metrics: dict[str, Any],
    *,
    status: str | None,
    claims: int,
    payment: Any = 0,
    submitted_with_date: int = 0,
) -> dict[str, Any]:
    n = _int(claims)
    if n <= 0:
        return metrics
    bucket = classify_workload_outcome(status)
    metrics["claims"] = _int(metrics.get("claims")) + n
    metrics["payment"] = _num(metrics.get("payment")) + _num(payment)
    if bucket == _SUBMITTED:
        metrics["submitted"] = _int(metrics.get("submitted")) + n
    else:
        metrics["submitted"] = _int(metrics.get("submitted")) + _int(submitted_with_date)
    if bucket == _PAID:
        metrics["paid"] = _int(metrics.get("paid")) + n
    elif bucket == _DENIED:
        metrics["denied"] = _int(metrics.get("denied")) + n
    elif bucket == _PENDING:
        metrics["pending"] = _int(metrics.get("pending")) + n
    elif bucket == _CORRECTED:
        metrics["corrected"] = _int(metrics.get("corrected")) + n
    elif bucket == _TFL:
        metrics["timely_filing"] = _int(metrics.get("timely_filing")) + n
    return metrics


def fill_breakdown_rows(
    grain: str,
    year: int,
    grouped: dict[date, dict[str, Any]],
    *,
    month: int | None = None,
) -> dict[str, Any]:
    kind = (grain or "month").strip().lower()
    if kind == "day":
        m = int(month or 1)
        keys = [(d, d.isoformat()) for d in iter_month_days(year, m)]
    elif kind == "week":
        keys = [(d, f"Week of {d.isoformat()}") for d in iter_year_weeks(year)]
    else:
        kind = "month"
        keys = [(date(year, m, 1), f"{_MONTH_LABELS[m - 1]} {year}") for m in range(1, 13)]
    rows: list[dict[str, Any]] = []
    totals = empty_breakdown_row()
    blank = empty_breakdown_row()
    for start, label in keys:
        src = grouped.get(start) or blank
        row = {
            "period": label,
            "period_start": start.isoformat(),
            "payment": _num(src.get("payment")),
            "claims": _int(src.get("claims")),
            "submitted": _int(src.get("submitted")),
            "paid": _int(src.get("paid")),
            "pending": _int(src.get("pending")),
            "denied": _int(src.get("denied")),
            "corrected": _int(src.get("corrected")),
            "timely_filing": _int(src.get("timely_filing")),
        }
        row["total_submitted_claims"] = total_submitted_claims(row)
        totals = _sum_breakdown(totals, row)
        rows.append(row)
    totals["total_submitted_claims"] = total_submitted_claims(totals)
    return {
        "grain": kind,
        "year": year,
        "month": month if kind == "day" else None,
        "rows": rows,
        "totals": {"period": "Total", "period_start": None, **totals},
    }


def viewer_scope(roles: list[str] | None) -> str:
    keys = list(roles or [])
    if "super_admin" in keys or "sub_admin" in keys:
        return SCOPE_ALL
    if "ops_admin" in keys or "analytics_viewer" in keys:
        return SCOPE_OPS
    if "second_submission_lead" in keys:
        return SCOPE_SS
    raise PermissionError("insufficient analytics scope")


def scope_role_keys(scope: str) -> tuple[str, ...] | None:
    if scope == SCOPE_ALL:
        return None
    if scope == SCOPE_OPS:
        return OPS_ROLE_KEYS
    if scope == SCOPE_SS:
        return SS_ROLE_KEYS
    raise PermissionError("insufficient analytics scope")


def resolve_team(roles: list[str] | None, team: str | None) -> str:
    requested = (team or TEAM_SS).strip().lower() or TEAM_SS
    if requested not in KNOWN_TEAMS:
        raise ValueError(f"unknown team: {requested}")
    scope = viewer_scope(roles)
    if scope == SCOPE_SS and requested != TEAM_SS:
        raise PermissionError("lead is limited to second_submission")
    return requested


def team_role_keys(team_key: str) -> tuple[str, ...]:
    keys = TEAM_ROLE_KEYS.get(team_key)
    if not keys:
        raise ValueError(f"unknown team: {team_key}")
    return keys


def visible_teams(roles: list[str] | None) -> list[dict[str, str]]:
    resolve_team(roles, TEAM_SS)
    scope = viewer_scope(roles)
    keys = (TEAM_SS,) if scope == SCOPE_SS else KNOWN_TEAMS
    return [{"key": key, "label": TEAM_LABELS[key]} for key in keys]


def resolve_period(
    preset: str | None,
    date_from: date | None = None,
    date_to: date | None = None,
    today: date | None = None,
) -> tuple[datetime, datetime]:
    today = today or date.today()
    kind = (preset or "month").strip().lower()
    if kind == "today":
        start_d = today
        end_d = today
    elif kind == "week":
        start_d = today - timedelta(days=today.weekday())
        end_d = start_d + timedelta(days=6)
    elif kind == "custom":
        start_d = date_from or today.replace(day=1)
        end_d = date_to or today
        if end_d < start_d:
            start_d, end_d = end_d, start_d
    else:
        start_d = today.replace(day=1)
        end_d = today
    start = datetime.combine(start_d, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def plan_heartbeat(
    last: dict[str, Any] | None,
    now: datetime,
    *,
    idle: bool = False,
    gap_seconds: int = SLICE_GAP_SECONDS,
) -> dict[str, Any]:
    if idle:
        return {"action": "skip", "add_seconds": 0}
    now = _as_aware(now)
    if not last:
        return {"action": "open", "add_seconds": 0}
    last_ping = last.get("last_ping_at")
    if last_ping is None:
        return {"action": "open", "add_seconds": 0}
    last_ping = _as_aware(last_ping)
    delta = (now - last_ping).total_seconds()
    if delta < 0:
        return {"action": "skip", "add_seconds": 0}
    if delta <= gap_seconds:
        return {"action": "extend", "add_seconds": max(0, int(delta))}
    return {"action": "open", "add_seconds": 0}


def idle_seconds_for_gap(delta_seconds: float, away_overlap_seconds: int = 0) -> int:
    """A return after a short empty stretch counts as idle. A long gap does not."""
    delta = int(delta_seconds)
    if delta <= SLICE_GAP_SECONDS or delta >= IDLE_CAP_SECONDS:
        return 0
    return max(0, delta - max(0, int(away_overlap_seconds)))


def away_overlap_seconds(
    start: datetime,
    end: datetime,
    sessions: list[dict[str, Any]],
) -> int:
    window_start = _as_aware(start)
    window_end = _as_aware(end)
    if window_end <= window_start:
        return 0
    total = 0
    for row in sessions:
        began = row.get("started_at")
        if not isinstance(began, datetime):
            continue
        began = _as_aware(began)
        finished = row.get("ended_at")
        stopped = _as_aware(finished) if isinstance(finished, datetime) else window_end
        lo = max(window_start, began)
        hi = min(window_end, stopped)
        if hi > lo:
            total += int((hi - lo).total_seconds())
    span = int((window_end - window_start).total_seconds())
    return min(total, span)


def record_heartbeat(
    conn: psycopg.Connection,
    user_id: str,
    *,
    page_path: str | None = None,
    idle: bool = False,
    presence: bool = False,
    closed: bool = False,
    desk_permission: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _as_aware(now or datetime.now(timezone.utc))
    permission = (desk_permission or "").strip().lower()
    if permission in DESK_PERMISSIONS:
        client.execute(
            conn,
            """
            UPDATE auth.app_user
            SET desk_permission = %s
            WHERE user_id = %s::uuid
            """,
            (permission, user_id),
        )
    path = None if presence or closed else ((page_path or "").strip()[:200] or None)
    last = client.fetchone(
        conn,
        """
        SELECT slice_id, last_ping_at, seconds_active
        FROM ops.user_activity_slice
        WHERE user_id = %s::uuid
        ORDER BY last_ping_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if closed:
        if not last:
            return {"ok": True, "counted": False, "action": "skip", "add_seconds": 0, "add_desk_seconds": 0}
        client.execute(
            conn,
            """
            UPDATE ops.user_activity_slice
            SET last_ping_at = %s
            WHERE slice_id = %s
            """,
            (moment, last["slice_id"]),
        )
        return {
            "ok": True,
            "counted": True,
            "action": "close",
            "slice_id": str(last["slice_id"]),
            "add_seconds": 0,
            "add_desk_seconds": 0,
            "add_idle_seconds": 0,
        }
    plan = plan_heartbeat(last, moment, idle=idle)
    if plan["action"] == "skip":
        return {"ok": True, "counted": False, "action": "skip"}
    active_add = 0 if presence else plan["add_seconds"]
    desk_add = plan["add_seconds"]
    if plan["action"] == "extend" and last:
        client.execute(
            conn,
            """
            UPDATE ops.user_activity_slice
            SET last_ping_at = %s,
                seconds_active = seconds_active + %s,
                seconds_desk = seconds_desk + %s,
                page_path = COALESCE(%s, page_path)
            WHERE slice_id = %s
            """,
            (moment, active_add, desk_add, path, last["slice_id"]),
        )
        return {
            "ok": True,
            "counted": True,
            "action": "extend",
            "slice_id": str(last["slice_id"]),
            "add_seconds": active_add,
            "add_desk_seconds": desk_add,
            "add_idle_seconds": 0,
        }
    idle_add = 0
    last_ping = last.get("last_ping_at") if last else None
    if isinstance(last_ping, datetime):
        last_ping = _as_aware(last_ping)
        delta = (moment - last_ping).total_seconds()
        if SLICE_GAP_SECONDS < delta < IDLE_CAP_SECONDS:
            aways = client.fetchall(
                conn,
                """
                SELECT started_at, ended_at
                FROM ops.user_away
                WHERE user_id = %s::uuid
                  AND started_at < %s
                  AND (ended_at IS NULL OR ended_at > %s)
                """,
                (user_id, moment, last_ping),
            )
            idle_add = idle_seconds_for_gap(delta, away_overlap_seconds(last_ping, moment, aways))
    slice_id = uuid4()
    client.execute(
        conn,
        """
        INSERT INTO ops.user_activity_slice (
            slice_id, user_id, started_at, last_ping_at,
            seconds_active, seconds_desk, seconds_idle, page_path
        )
        VALUES (%s, %s::uuid, %s, %s, 0, 0, %s, %s)
        """,
        (slice_id, user_id, moment, moment, idle_add, path),
    )
    return {
        "ok": True,
        "counted": True,
        "action": "open",
        "slice_id": str(slice_id),
        "add_seconds": 0,
        "add_desk_seconds": 0,
        "add_idle_seconds": idle_add,
    }


def canonical_collection_status(label: str | None) -> str:
    """Map a history value onto the collection-status catalog. Unknown labels are Other."""
    folded = fold_label(label)
    if not folded:
        return ""
    for seed in COLLECTION_STATUS_SEED:
        if fold_label(seed) == folded:
            return seed
    return COLLECTION_STATUS_OTHER


def canonical_root_cause(label: str | None) -> str:
    folded = fold_label(label)
    if not folded:
        return ""
    for seed in ROOT_CAUSE_SEED:
        if fold_label(seed) == folded:
            return seed
    return ROOT_CAUSE_OTHER


def empty_status_counts() -> dict[str, int]:
    return {label: 0 for label in COLLECTION_STATUS_LABELS}


def apply_status_count(bucket: dict[str, int], label: str | None, n: int) -> None:
    key = canonical_collection_status(label)
    if not key:
        return
    bucket[key] = _int(bucket.get(key)) + _int(n)


def claim_recovered(*, set_paid: bool, visit_paid: bool) -> bool:
    """Paid by the collector, or the visit later became paid/deduct after they worked it."""
    return bool(set_paid or visit_paid)


def latest_assignment_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest assigned_to write. A blank new value means the claim is no longer assigned."""
    if not events:
        return None
    last = max(events, key=lambda event: event["changed_at"])
    assignee = str(last.get("new_value") or "").strip()
    if not assignee:
        return None
    return {
        "assigner_id": str(last.get("changed_by") or ""),
        "assignee_id": assignee,
        "changed_at": last["changed_at"],
    }


def status_finishes_assignment(
    *,
    assignee_id: str,
    assigned_at: datetime,
    status_by: str | None,
    status_at: datetime | None,
    status_value: str | None,
) -> bool:
    """Finished only when the assignee writes a collection status after the assign."""
    if not status_by or status_at is None:
        return False
    if str(status_by) != str(assignee_id):
        return False
    if not str(status_value or "").strip():
        return False
    return status_at > assigned_at


def _top_cause(counts: dict[str, int], labels: list[str]) -> tuple[str, int]:
    top_label = ""
    top_count = 0
    for label in labels:
        n = _int(counts.get(label))
        if n > top_count:
            top_label = label
            top_count = n
    return top_label, top_count


def last_root_cause_write(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One fact per claim: the latest root-cause write, placed in the DOS month."""
    by_item: dict[str, dict[str, Any]] = {}
    for event in events:
        item = str(event.get("work_item_id") or "")
        if not item:
            continue
        changed_at = event.get("changed_at")
        prev = by_item.get(item)
        if prev is not None and changed_at < prev["changed_at"]:
            continue
        dos = event.get("dos")
        if isinstance(dos, datetime):
            dos = dos.date()
        if not isinstance(dos, date):
            continue
        by_item[item] = {
            "work_item_id": item,
            "changed_at": changed_at,
            "user_id": str(event.get("changed_by") or ""),
            "root_cause": event.get("root_cause"),
            "month_start": date(dos.year, dos.month, 1),
            "n": 1,
        }
    return list(by_item.values())


def rollup_root_causes(year: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One row per month. Each claim contributes its last root cause in its DOS month."""
    counts: dict[date, dict[str, int]] = {}
    seen: set[str] = set()
    for row in rows:
        month_start = row.get("month_start")
        if isinstance(month_start, datetime):
            month_start = month_start.date()
        if not isinstance(month_start, date):
            continue
        label = canonical_root_cause(str(row.get("root_cause") or ""))
        if not label:
            continue
        seen.add(label)
        bucket = counts.setdefault(month_start.replace(day=1), {})
        bucket[label] = _int(bucket.get(label)) + _int(row.get("n"))
    labels = [seed for seed in ROOT_CAUSE_SEED if seed in seen]
    if ROOT_CAUSE_OTHER in seen:
        labels.append(ROOT_CAUSE_OTHER)
    out_rows: list[dict[str, Any]] = []
    for month in range(1, 13):
        start = date(year, month, 1)
        bucket = counts.get(start, {})
        top_label, top_count = _top_cause(bucket, labels)
        out_rows.append(
            {
                "period": f"{_MONTH_LABELS[month - 1]} {year}",
                "period_start": start.isoformat(),
                "counts": {label: _int(bucket.get(label)) for label in labels},
                "top": top_label,
                "top_count": top_count,
            }
        )
    return {"year": year, "labels": labels, "rows": out_rows}


def rollup_root_cause_people(
    rows: list[dict[str, Any]],
    members: list[dict[str, Any]],
    labels: list[str],
) -> list[dict[str, Any]]:
    """Per collector: claims whose last root cause they wrote. Missing writers stay at zero."""
    by_user: dict[str, dict[str, int]] = {}
    for row in rows:
        uid = row.get("user_id")
        if uid is None or str(uid) == "":
            continue
        label = canonical_root_cause(str(row.get("root_cause") or ""))
        if not label:
            continue
        bucket = by_user.setdefault(str(uid), {})
        bucket[label] = _int(bucket.get(label)) + _int(row.get("n"))
    people: list[dict[str, Any]] = []
    for member in members:
        uid = str(member["user_id"])
        bucket = by_user.get(uid, {})
        counts = {label: _int(bucket.get(label)) for label in labels}
        top_label, top_count = _top_cause(counts, labels)
        people.append(
            {
                "user_id": uid,
                "display_name": member.get("display_name") or "",
                "counts": counts,
                "top": top_label,
                "top_count": top_count,
            }
        )
    return people


def empty_user_metrics() -> dict[str, Any]:
    return {
        "seconds_today": 0,
        "seconds_week": 0,
        "seconds_month": 0,
        "seconds_period": 0,
        "last_activity_at": None,
        "logins_period": 0,
        "elig_touched": 0,
        "elig_touched_today": 0,
        "elig_touched_week": 0,
        "elig_touched_month": 0,
        "elig_completed": 0,
        "elig_completed_today": 0,
        "elig_completed_week": 0,
        "elig_completed_month": 0,
        "elig_money": 0.0,
        "coll_touched": 0,
        "coll_touched_today": 0,
        "coll_touched_week": 0,
        "coll_touched_month": 0,
        "coll_worked": 0,
        "coll_worked_today": 0,
        "coll_worked_week": 0,
        "coll_worked_month": 0,
        "coll_recovered_today": 0,
        "coll_recovered_month": 0,
        "coll_money_today": 0.0,
        "coll_money_month": 0.0,
        "coll_assigned": 0,
        "coll_finished": 0,
        "coll_status_today": empty_status_counts(),
        "coll_status_month": empty_status_counts(),
        "ss_claims": 0,
        "ss_claims_today": 0,
        "ss_claims_week": 0,
        "ss_claims_month": 0,
        "ss_money": 0.0,
        "ss_paid": 0,
        "ss_denied": 0,
        "ss_pending": 0,
        "ss_submitted": 0,
        "ss_corrected": 0,
        "ss_timely_filing": 0,
        "ss_not_entered": 0,
        "ss_other": 0,
        "ss_paid_today": 0,
        "ss_denied_today": 0,
        "ss_timely_filing_today": 0,
        "cpt_touched": 0,
        "cpt_touched_today": 0,
        "cpt_touched_week": 0,
        "cpt_touched_month": 0,
        "cpt_resolved": 0,
        "cpt_resolved_today": 0,
        "cpt_resolved_week": 0,
        "cpt_resolved_month": 0,
        "icd_touched": 0,
        "icd_touched_today": 0,
        "icd_touched_week": 0,
        "icd_touched_month": 0,
        "icd_resolved": 0,
        "icd_resolved_today": 0,
        "icd_resolved_week": 0,
        "icd_resolved_month": 0,
        "tracker_rows": 0,
        "tracker_money": 0.0,
        "money_total": 0.0,
    }


def _is_money_metric(key: str) -> bool:
    return key.endswith("_money") or key.startswith("coll_money") or key == "money_total"


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_aware(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _index_by_user(rows: list[dict[str, Any]], key: str = "user_id") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row.get(key)
        if uid is None:
            continue
        out[str(uid)] = row
    return out


def merge_user_row(
    base: dict[str, Any],
    *parts: dict[str, Any] | None,
) -> dict[str, Any]:
    row = dict(base)
    metrics = empty_user_metrics()
    for part in parts:
        if not part:
            continue
        for key, value in part.items():
            if key in {"user_id", "display_name", "username", "roles", "is_active", "last_login_at"}:
                continue
            if key not in metrics:
                continue
            if key in {"last_activity_at"}:
                metrics[key] = value or metrics[key]
            elif _is_money_metric(key):
                metrics[key] = _num(metrics[key]) + _num(value)
            elif isinstance(metrics[key], dict):
                metrics[key] = value if isinstance(value, dict) else metrics[key]
            elif isinstance(metrics[key], float):
                metrics[key] = _num(metrics[key]) + _num(value)
            elif isinstance(metrics[key], int):
                metrics[key] = _int(metrics[key]) + _int(value)
            else:
                metrics[key] = value if value is not None else metrics[key]
    metrics["money_total"] = (
        _num(metrics["elig_money"]) + _num(metrics["ss_money"]) + _num(metrics["tracker_money"])
    )
    row.update(metrics)
    row["last_login_at"] = _iso(row.get("last_login_at"))
    row["last_activity_at"] = _iso(metrics.get("last_activity_at"))
    return row


def summarize_kpis(people: list[dict[str, Any]]) -> dict[str, Any]:
    keys = (
        "seconds_period",
        "elig_touched",
        "elig_touched_today",
        "elig_touched_week",
        "elig_touched_month",
        "elig_completed",
        "elig_completed_today",
        "elig_completed_week",
        "elig_completed_month",
        "elig_money",
        "coll_touched",
        "coll_touched_today",
        "coll_touched_week",
        "coll_touched_month",
        "coll_worked",
        "coll_worked_today",
        "coll_worked_week",
        "coll_worked_month",
        "coll_recovered_today",
        "coll_recovered_month",
        "coll_money_today",
        "coll_money_month",
        "coll_assigned",
        "coll_finished",
        "ss_claims",
        "ss_claims_today",
        "ss_claims_week",
        "ss_claims_month",
        "ss_money",
        "ss_paid",
        "ss_denied",
        "ss_pending",
        "ss_submitted",
        "ss_corrected",
        "ss_timely_filing",
        "ss_not_entered",
        "ss_paid_today",
        "ss_denied_today",
        "ss_timely_filing_today",
        "cpt_touched",
        "cpt_touched_today",
        "cpt_touched_week",
        "cpt_touched_month",
        "cpt_resolved",
        "cpt_resolved_today",
        "cpt_resolved_week",
        "cpt_resolved_month",
        "icd_touched",
        "icd_touched_today",
        "icd_touched_week",
        "icd_touched_month",
        "icd_resolved",
        "icd_resolved_today",
        "icd_resolved_week",
        "icd_resolved_month",
        "tracker_rows",
        "tracker_money",
        "money_total",
        "logins_period",
    )
    totals = {k: 0 for k in keys}
    for person in people:
        for key in keys:
            if _is_money_metric(key):
                totals[key] = _num(totals[key]) + _num(person.get(key))
            else:
                totals[key] = _int(totals[key]) + _int(person.get(key))
    totals["people"] = len(people)
    return totals


def list_scoped_users(
    conn: psycopg.Connection,
    roles: list[str] | None,
    team: str | None = None,
) -> list[dict[str, Any]]:
    team_key = resolve_team(roles, team)
    role_keys = team_role_keys(team_key)
    rows = client.fetchall(
        conn,
        """
        SELECT u.user_id, u.username, u.display_name, u.is_active, u.last_login_at,
               COALESCE(
                   array_agg(r.role_key ORDER BY r.role_key)
                       FILTER (WHERE r.role_key IS NOT NULL),
                   ARRAY[]::text[]
               ) AS roles
        FROM auth.app_user u
        JOIN auth.user_role ur ON ur.user_id = u.user_id
        JOIN auth.role r ON r.role_id = ur.role_id
        WHERE u.is_active
          AND u.user_id IN (
              SELECT ur2.user_id
              FROM auth.user_role ur2
              JOIN auth.role r2 ON r2.role_id = ur2.role_id
              WHERE r2.role_key = ANY(%s)
          )
        GROUP BY u.user_id
        ORDER BY u.display_name
        """,
        (list(role_keys),),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "user_id": str(row["user_id"]),
                "username": row["username"],
                "display_name": row["display_name"],
                "is_active": bool(row.get("is_active")),
                "last_login_at": row.get("last_login_at"),
                "roles": list(row.get("roles") or []),
            }
        )
    return out


def _hours_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    return client.fetchall(
        conn,
        """
        SELECT user_id,
               COALESCE(SUM(seconds_active) FILTER (
                   WHERE started_at >= %s AND started_at < %s
               ), 0)::int AS seconds_period,
               COALESCE(SUM(seconds_active) FILTER (
                   WHERE started_at >= %s::date
               ), 0)::int AS seconds_today,
               COALESCE(SUM(seconds_active) FILTER (
                   WHERE started_at >= %s::date
               ), 0)::int AS seconds_week,
               COALESCE(SUM(seconds_active) FILTER (
                   WHERE started_at >= %s::date
               ), 0)::int AS seconds_month,
               MAX(last_ping_at) AS last_activity_at
        FROM ops.user_activity_slice
        WHERE user_id = ANY(%s::uuid[])
        GROUP BY user_id
        """,
        (start, end, today, week_start, month_start, user_ids),
    )


def _login_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT user_id, count(*)::int AS logins_period
        FROM auth.login_event
        WHERE user_id = ANY(%s::uuid[])
          AND logged_in_at >= %s AND logged_in_at < %s
        GROUP BY user_id
        """,
        (user_ids, start, end),
    )


def _period_count_sql(ts_col: str, alias: str, *, distinct: str | None = None) -> str:
    inner = f"DISTINCT {distinct}" if distinct else "*"
    return f"""
        count({inner}) FILTER (WHERE {ts_col} >= %s AND {ts_col} < %s)::int AS {alias},
        count({inner}) FILTER (WHERE {ts_col}::date = %s)::int AS {alias}_today,
        count({inner}) FILTER (
            WHERE {ts_col}::date >= %s AND {ts_col}::date < %s
        )::int AS {alias}_week,
        count({inner}) FILTER (
            WHERE {ts_col}::date >= %s AND {ts_col}::date < %s
        )::int AS {alias}_month
    """


def _period_sum_sql(ts_col: str, amount_col: str, alias: str) -> str:
    return f"""
        COALESCE(SUM({amount_col}) FILTER (
            WHERE {ts_col} >= %s AND {ts_col} < %s
        ), 0) AS {alias}
    """


def _period_count_params(start: datetime, end: datetime, today: date) -> tuple[Any, ...]:
    _start_d, _end_d, today_d, week_start, week_end, month_start, month_end = _ss_period_bounds(
        start, end, today
    )
    return (start, end, today_d, week_start, week_end, month_start, month_end)


def _date_count_sql(date_col: str, alias: str) -> str:
    return f"""
        count(*) FILTER (WHERE {date_col} >= %s AND {date_col} < %s)::int AS {alias},
        count(*) FILTER (WHERE {date_col} = %s)::int AS {alias}_today,
        count(*) FILTER (
            WHERE {date_col} >= %s AND {date_col} < %s
        )::int AS {alias}_week,
        count(*) FILTER (
            WHERE {date_col} >= %s AND {date_col} < %s
        )::int AS {alias}_month
    """


def _date_count_params(start: datetime, end: datetime, today: date) -> tuple[Any, ...]:
    start_d, end_d, today_d, week_start, week_end, month_start, month_end = _ss_period_bounds(
        start, end, today
    )
    return (start_d, end_d, today_d, week_start, week_end, month_start, month_end)


def _fold_metric_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_user: dict[str, dict[str, Any]] = {}
    for rows in groups:
        for row in rows:
            uid = row.get("user_id")
            if uid is None:
                continue
            key = str(uid)
            dest = by_user.setdefault(key, {"user_id": key})
            dest.update({k: v for k, v in row.items() if k != "user_id"})
    return list(by_user.values())


def _expand_domain_counts(
    rows: list[dict[str, Any]],
    kind: str,
) -> list[dict[str, Any]]:
    by_user: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row.get("user_id")
        if uid is None:
            continue
        key = str(uid)
        dest = by_user.setdefault(key, {"user_id": key})
        domain = str(row.get("audit_domain") or "cpt").strip().lower()
        prefix = "icd" if domain == "icd" else "cpt"
        dest[f"{prefix}_{kind}"] = _int(row.get("n"))
        dest[f"{prefix}_{kind}_today"] = _int(row.get("n_today"))
        dest[f"{prefix}_{kind}_week"] = _int(row.get("n_week"))
        dest[f"{prefix}_{kind}_month"] = _int(row.get("n_month"))
    return list(by_user.values())


def _elig_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    params = _period_count_params(start, end, today)
    money_params = (start, end)
    touched = client.fetchall(
        conn,
        f"""
        SELECT h.changed_by AS user_id,
{_period_count_sql("h.changed_at", "elig_touched", distinct="h.work_item_id")}
        FROM ops.eligibility_history h
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = h.work_item_id
        WHERE h.changed_by = ANY(%s::uuid[])
          AND {ELIG_SHEET_WHERE}
        GROUP BY h.changed_by
        """,
        (*params, user_ids),
    )
    completed = client.fetchall(
        conn,
        f"""
        SELECT COALESCE(wi.assigned_to, wi.updated_by) AS user_id,
{_period_count_sql("wi.completed_at", "elig_completed")}
        FROM ops.eligibility_work_item wi
        WHERE COALESCE(wi.assigned_to, wi.updated_by) = ANY(%s::uuid[])
          AND wi.eligibility_status IN ('completed', 'rejected')
          AND {ELIG_SHEET_WHERE}
        GROUP BY 1
        """,
        (*params, user_ids),
    )
    money = client.fetchall(
        conn,
        f"""
        SELECT led.created_by AS user_id,
{_period_sum_sql("led.created_at", "led.amount", "elig_money")}
        FROM ops.eligibility_amount_ledger led
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = led.work_item_id
        WHERE led.created_by = ANY(%s::uuid[])
          AND {ELIG_SHEET_WHERE}
        GROUP BY led.created_by
        """,
        (*money_params, user_ids),
    )
    return _fold_metric_rows(touched, completed, money)


def _month_bounds(today: date) -> tuple[date, date]:
    month_start = today.replace(day=1)
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)
    return month_start, month_end


def _collection_worked_sql(*, by_user: bool) -> str:
    user_col = "h.changed_by AS user_id," if by_user else ""
    group_by = "h.changed_by, h.work_item_id" if by_user else "h.work_item_id"
    return f"""
            SELECT
                {user_col}
                h.work_item_id,
                bool_or(h.changed_at::date = %s) AS worked_today,
                bool_or(
                    h.changed_at::date = %s
                    AND {COLLECTION_SET_PAID_SQL}
                ) AS set_paid_today,
                bool_or({COLLECTION_SET_PAID_SQL}) AS set_paid_month
            FROM ops.eligibility_history h
            WHERE h.changed_by = ANY(%s::uuid[])
              AND h.column_name = ANY(%s::text[])
              AND h.changed_at::date >= %s
              AND h.changed_at::date < %s
            GROUP BY {group_by}
    """


def _collection_recovered_select(user_expr: str, group_expr: str) -> str:
    return f"""
        SELECT
            {user_expr}
            count(*) FILTER (WHERE {COLLECTION_RECOVERED_TODAY_SQL})::int AS coll_recovered_today,
            count(*) FILTER (WHERE {COLLECTION_RECOVERED_MONTH_SQL})::int AS coll_recovered_month,
            COALESCE(SUM({COLLECTION_PAYMENT_SQL}) FILTER (
                WHERE {COLLECTION_RECOVERED_TODAY_SQL}
            ), 0) AS coll_money_today,
            COALESCE(SUM({COLLECTION_PAYMENT_SQL}) FILTER (
                WHERE {COLLECTION_RECOVERED_MONTH_SQL}
            ), 0) AS coll_money_month
        FROM worked w
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = w.work_item_id
        {group_expr}
    """


def _collection_recovered_params(
    user_ids: list[str],
    today: date,
) -> tuple[Any, ...]:
    month_start, month_end = _month_bounds(today)
    return (today, today, user_ids, list(COLLECTION_EDIT_COLUMNS), month_start, month_end)


def _shape_recovered_row(row: dict[str, Any]) -> dict[str, Any]:
    shaped = {
        "coll_recovered_today": _int(row.get("coll_recovered_today")),
        "coll_recovered_month": _int(row.get("coll_recovered_month")),
        "coll_money_today": _num(row.get("coll_money_today")),
        "coll_money_month": _num(row.get("coll_money_month")),
    }
    if row.get("user_id") is not None:
        shaped["user_id"] = str(row["user_id"])
    return shaped


def _empty_team_recovered() -> dict[str, Any]:
    return {
        "coll_recovered_today": 0,
        "coll_recovered_month": 0,
        "coll_money_today": 0.0,
        "coll_money_month": 0.0,
    }


def _collection_status_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    today: date,
) -> list[dict[str, Any]]:
    """Last collection_status each person wrote today and this month, per claim."""
    if not user_ids:
        return []
    month_start, month_end = _month_bounds(today)
    rows = client.fetchall(
        conn,
        """
        SELECT user_id, period, status_label, count(*)::int AS n
        FROM (
            SELECT DISTINCT ON (h.changed_by, h.work_item_id)
                h.changed_by AS user_id,
                NULLIF(btrim(h.new_value), '') AS status_label,
                'today'::text AS period
            FROM ops.eligibility_history h
            WHERE h.changed_by = ANY(%s::uuid[])
              AND h.column_name = 'collection_status'
              AND h.changed_at::date = %s
              AND NULLIF(btrim(h.new_value), '') IS NOT NULL
            ORDER BY h.changed_by, h.work_item_id, h.changed_at DESC
        ) today_last
        GROUP BY user_id, period, status_label
        UNION ALL
        SELECT user_id, period, status_label, count(*)::int AS n
        FROM (
            SELECT DISTINCT ON (h.changed_by, h.work_item_id)
                h.changed_by AS user_id,
                NULLIF(btrim(h.new_value), '') AS status_label,
                'month'::text AS period
            FROM ops.eligibility_history h
            WHERE h.changed_by = ANY(%s::uuid[])
              AND h.column_name = 'collection_status'
              AND h.changed_at::date >= %s
              AND h.changed_at::date < %s
              AND NULLIF(btrim(h.new_value), '') IS NOT NULL
            ORDER BY h.changed_by, h.work_item_id, h.changed_at DESC
        ) month_last
        GROUP BY user_id, period, status_label
        """,
        (user_ids, today, user_ids, month_start, month_end),
    )
    by_user: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row["user_id"])
        dest = by_user.setdefault(
            uid,
            {
                "user_id": uid,
                "coll_status_today": empty_status_counts(),
                "coll_status_month": empty_status_counts(),
            },
        )
        period_key = "coll_status_today" if row.get("period") == "today" else "coll_status_month"
        apply_status_count(dest[period_key], row.get("status_label"), _int(row.get("n")))
    return list(by_user.values())


def _collection_recovered_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    today: date,
) -> list[dict[str, Any]]:
    """Claims this person worked that they marked Paid, or that eligibility later paid."""
    if not user_ids:
        return []
    rows = client.fetchall(
        conn,
        f"""
        WITH worked AS (
{_collection_worked_sql(by_user=True)}
        )
{_collection_recovered_select("w.user_id,", "GROUP BY w.user_id")}
        """,
        _collection_recovered_params(user_ids, today),
    )
    return [_shape_recovered_row(row) for row in rows]


def _collection_team_recovered(
    conn: psycopg.Connection,
    user_ids: list[str],
    today: date,
) -> dict[str, Any]:
    """Team recovered totals count each claim once, even if two people worked it."""
    if not user_ids:
        return _empty_team_recovered()
    row = client.fetchone(
        conn,
        f"""
        WITH worked AS (
{_collection_worked_sql(by_user=False)}
        )
{_collection_recovered_select("", "")}
        """,
        _collection_recovered_params(user_ids, today),
    )
    if not row:
        return _empty_team_recovered()
    return _shape_recovered_row(row)


def _collection_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    touched = client.fetchall(
        conn,
        f"""
        SELECT h.changed_by AS user_id,
{_period_count_sql("h.changed_at", "coll_touched", distinct="h.work_item_id")}
        FROM ops.eligibility_history h
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = h.work_item_id
        WHERE h.changed_by = ANY(%s::uuid[])
          AND {COLLECTION_QUEUE_WHERE}
        GROUP BY h.changed_by
        """,
        (*_period_count_params(start, end, today), user_ids),
    )
    worked = client.fetchall(
        conn,
        f"""
        SELECT COALESCE(wi.assigned_to, wi.updated_by) AS user_id,
{_date_count_sql("work_day", "coll_worked")}
        FROM (
            SELECT
                wi.assigned_to,
                wi.updated_by,
                {COLLECTION_WORK_DAY_SQL} AS work_day
            FROM ops.eligibility_work_item wi
            WHERE COALESCE(wi.assigned_to, wi.updated_by) = ANY(%s::uuid[])
              AND {COLLECTION_QUEUE_WHERE}
              AND {COLLECTION_WORKED_SQL}
        ) wi
        GROUP BY 1
        """,
        (*_date_count_params(start, end, today), user_ids),
    )
    status = _collection_status_rows(conn, user_ids, today)
    recovered = _collection_recovered_rows(conn, user_ids, today)
    return _fold_metric_rows(touched, worked, status, recovered)


def _ss_period_bounds(
    start: datetime,
    end: datetime,
    today: date,
) -> tuple[date, date, date, date, date, date, date]:
    start_d = start.date()
    end_d = end.date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    month_start = today.replace(day=1)
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)
    return start_d, end_d, today, week_start, week_end, month_start, month_end


SS_METRIC_SELECT = f"""
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
               )::int AS ss_claims,
               count(*) FILTER (WHERE work_day = %s)::int AS ss_claims_today,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
               )::int AS ss_claims_week,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
               )::int AS ss_claims_month,
               COALESCE(SUM(payment_amt) FILTER (
                   WHERE work_day >= %s AND work_day < %s
               ), 0) AS ss_money,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND lower(btrim(workload_status)) = 'paid'
               )::int AS ss_paid,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND btrim(workload_status) IN ('Denied', 'Not Paid')
               )::int AS ss_denied,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND lower(btrim(workload_status)) = 'pending'
               )::int AS ss_pending,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND (
                         lower(btrim(workload_status)) = 'submitted'
                      OR submission_date IS NOT NULL
                     )
               )::int AS ss_submitted,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND lower(btrim(workload_status)) = 'corrected'
               )::int AS ss_corrected,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND btrim(workload_status) = 'Timely Filing'
               )::int AS ss_timely_filing,
               count(*) FILTER (
                   WHERE work_day >= %s AND work_day < %s
                     AND NULLIF(btrim(workload_status), '') IS NULL
               )::int AS ss_not_entered,
               count(*) FILTER (
                   WHERE work_day = %s AND lower(btrim(workload_status)) = 'paid'
               )::int AS ss_paid_today,
               count(*) FILTER (
                   WHERE work_day = %s
                     AND btrim(workload_status) IN ('Denied', 'Not Paid')
               )::int AS ss_denied_today,
               count(*) FILTER (
                   WHERE work_day = %s AND btrim(workload_status) = 'Timely Filing'
               )::int AS ss_timely_filing_today
"""


def _ss_metric_params(
    start: datetime,
    end: datetime,
    today: date,
) -> tuple[Any, ...]:
    start_d, end_d, today_d, week_start, week_end, month_start, month_end = _ss_period_bounds(
        start, end, today
    )
    return (
        start_d,
        end_d,
        today_d,
        week_start,
        week_end,
        month_start,
        month_end,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        start_d,
        end_d,
        today_d,
        today_d,
        today_d,
    )


def _ss_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    return client.fetchall(
        conn,
        f"""
        WITH attributed AS (
            SELECT
                su.user_id,
                flag.workload_status,
                flag.submission_date,
                {SS_WORK_DAY_SQL} AS work_day,
                {SS_PAYMENT_SQL} AS payment_amt
            FROM ops.pr_queue_flag flag
            {SS_SUBMITTER_JOIN}
            WHERE {SS_WORKLOAD_WHERE}
              AND su.user_id = ANY(%s::uuid[])
        )
        SELECT user_id,
{SS_METRIC_SELECT}
        FROM attributed
        GROUP BY user_id
        """,
        (user_ids, *_ss_metric_params(start, end, today)),
    )


def ss_claims_today_for_user(
    conn: psycopg.Connection,
    user_id: str,
    *,
    today: date | None = None,
) -> int:
    """Claims credited to this submitter whose work day is today."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0
    today = today or date.today()
    row = client.fetchone(
        conn,
        f"""
        SELECT count(*)::int AS n
        FROM ops.pr_queue_flag flag
        {SS_SUBMITTER_JOIN}
        WHERE {SS_WORKLOAD_WHERE}
          AND su.user_id = %s::uuid
          AND {SS_WORK_DAY_SQL} = %s
        """,
        (uid, today),
    )
    return int((row or {}).get("n") or 0)


MY_TODAY_AREAS = (TEAM_ELIGIBILITY, TEAM_COLLECTION, TEAM_SUBMISSION)


def completed_today_for_user(
    conn: psycopg.Connection,
    user_id: str,
    area: str,
    *,
    today: date | None = None,
) -> int:
    """Personal completed-today count, using the same filters as team analytics."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0
    key = (area or "").strip().lower()
    if key not in MY_TODAY_AREAS:
        raise ValueError("area must be eligibility, collection, or submission")
    today = today or date.today()
    if key == TEAM_ELIGIBILITY:
        sql = f"""
        SELECT count(*)::int AS n
        FROM ops.eligibility_work_item wi
        WHERE COALESCE(wi.assigned_to, wi.updated_by) = %s::uuid
          AND wi.eligibility_status IN ('completed', 'rejected')
          AND {ELIG_SHEET_WHERE}
          AND wi.completed_at::date = %s
        """
    elif key == TEAM_COLLECTION:
        sql = f"""
        SELECT count(*)::int AS n
        FROM ops.eligibility_work_item wi
        WHERE COALESCE(wi.assigned_to, wi.updated_by) = %s::uuid
          AND {COLLECTION_QUEUE_WHERE}
          AND {COLLECTION_WORKED_SQL}
          AND ({COLLECTION_WORK_DAY_SQL}) = %s
        """
    else:
        sql = f"""
        SELECT count(*)::int AS n
        FROM ops.cpt_audit_work_item wi
        JOIN billing.cpt_audit_finding f ON f.finding_id = wi.finding_id
        WHERE wi.resolved_by = %s::uuid
          AND wi.workflow_status IN ('resolved', 'ignored')
          AND {CPT_LIVE_DOMAIN_SQL}
          AND wi.resolved_at::date = %s
        """
    row = client.fetchone(conn, sql, (uid, today))
    return int((row or {}).get("n") or 0)


def _ss_team_totals(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> dict[str, Any]:
    row = client.fetchone(
        conn,
        f"""
        WITH scoped AS (
            SELECT
                flag.workload_status,
                flag.submission_date,
                {SS_DOS_SQL} AS work_day,
                {SS_PAYMENT_SQL} AS payment_amt
            FROM ops.pr_queue_flag flag
            WHERE {SS_WORKLOAD_WHERE}
        )
        SELECT
{SS_METRIC_SELECT}
        FROM scoped
        """,
        _ss_metric_params(start, end, today),
    )
    return row or {}


SS_TEAM_KPI_KEYS = (
    "ss_claims",
    "ss_claims_today",
    "ss_claims_week",
    "ss_claims_month",
    "ss_money",
    "ss_paid",
    "ss_denied",
    "ss_pending",
    "ss_submitted",
    "ss_corrected",
    "ss_timely_filing",
    "ss_not_entered",
    "ss_paid_today",
    "ss_denied_today",
    "ss_timely_filing_today",
)


def apply_team_ss_kpis(
    kpis: dict[str, Any],
    team_ss: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(kpis)
    src = team_ss or {}
    for key in SS_TEAM_KPI_KEYS:
        if key == "ss_money":
            out[key] = _num(src.get(key))
        else:
            out[key] = _int(src.get(key))
    out["money_total"] = _num(out.get("ss_money"))
    return out


def _cpt_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    params = _period_count_params(start, end, today)
    touched = client.fetchall(
        conn,
        f"""
        SELECT h.changed_by AS user_id,
               {CPT_DOMAIN_SQL} AS audit_domain,
               count(DISTINCT h.work_item_id) FILTER (
                   WHERE h.changed_at >= %s AND h.changed_at < %s
               )::int AS n,
               count(DISTINCT h.work_item_id) FILTER (
                   WHERE h.changed_at::date = %s
               )::int AS n_today,
               count(DISTINCT h.work_item_id) FILTER (
                   WHERE h.changed_at::date >= %s AND h.changed_at::date < %s
               )::int AS n_week,
               count(DISTINCT h.work_item_id) FILTER (
                   WHERE h.changed_at::date >= %s AND h.changed_at::date < %s
               )::int AS n_month
        FROM ops.cpt_audit_work_history h
        JOIN ops.cpt_audit_work_item wi ON wi.work_item_id = h.work_item_id
        JOIN billing.cpt_audit_finding f ON f.finding_id = wi.finding_id
        WHERE h.changed_by = ANY(%s::uuid[])
          AND {CPT_LIVE_DOMAIN_SQL}
        GROUP BY h.changed_by, 2
        """,
        (*params, user_ids),
    )
    resolved = client.fetchall(
        conn,
        f"""
        SELECT wi.resolved_by AS user_id,
               {CPT_DOMAIN_SQL} AS audit_domain,
               count(*) FILTER (
                   WHERE wi.resolved_at >= %s AND wi.resolved_at < %s
               )::int AS n,
               count(*) FILTER (
                   WHERE wi.resolved_at::date = %s
               )::int AS n_today,
               count(*) FILTER (
                   WHERE wi.resolved_at::date >= %s AND wi.resolved_at::date < %s
               )::int AS n_week,
               count(*) FILTER (
                   WHERE wi.resolved_at::date >= %s AND wi.resolved_at::date < %s
               )::int AS n_month
        FROM ops.cpt_audit_work_item wi
        JOIN billing.cpt_audit_finding f ON f.finding_id = wi.finding_id
        WHERE wi.resolved_by = ANY(%s::uuid[])
          AND wi.workflow_status IN ('resolved', 'ignored')
          AND {CPT_LIVE_DOMAIN_SQL}
        GROUP BY wi.resolved_by, 2
        """,
        (*params, user_ids),
    )
    return _fold_metric_rows(
        _expand_domain_counts(touched, "touched"),
        _expand_domain_counts(resolved, "resolved"),
    )


def _tracker_rows(
    conn: psycopg.Connection,
    user_ids: list[str],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    return client.fetchall(
        conn,
        """
        SELECT actor_user_id AS user_id,
               count(*)::int AS tracker_rows,
               COALESCE(SUM(
                   COALESCE(
                       (regexp_match(
                           COALESCE(after_json->>'amount', ''),
                           '-?[0-9]+(\\.[0-9]+)?'
                       ))[1]::numeric,
                       0
                   )
               ), 0) AS tracker_money
        FROM billing.transaction_tracker_audit
        WHERE actor_user_id = ANY(%s::uuid[])
          AND acted_at >= %s AND acted_at < %s
          AND entity_type = 'row'
          AND action IN ('create', 'update', 'upload_apply')
        GROUP BY actor_user_id
        """,
        (user_ids, start, end),
    )


def _chart_payload(
    people: list[dict[str, Any]],
    team_key: str = TEAM_SS,
) -> dict[str, list[dict[str, Any]]]:
    hours = [
        {
            "name": p["display_name"],
            "hours": round(_int(p.get("seconds_period")) / 3600, 2),
        }
        for p in people
        if _int(p.get("seconds_period")) > 0
    ]
    outcomes = []
    if team_key == TEAM_SS:
        outcomes = [
            {
                "name": p["display_name"],
                "paid": _int(p.get("ss_paid")),
                "denied": _int(p.get("ss_denied")),
                "timely_filing": _int(p.get("ss_timely_filing")),
            }
            for p in people
            if _int(p.get("ss_claims")) > 0
        ]
    return {"hours": hours, "outcomes": outcomes}


def _team_sort_key(team_key: str, row: dict[str, Any]) -> tuple[Any, ...]:
    is_system = str(row.get("display_name") or "").strip().casefold() == "system"
    if team_key == TEAM_ELIGIBILITY:
        primary = _int(row.get("elig_completed_month"))
    elif team_key == TEAM_COLLECTION:
        primary = _int(row.get("coll_worked_month"))
    elif team_key == TEAM_SUBMISSION:
        primary = _int(row.get("cpt_resolved_month")) + _int(row.get("icd_resolved_month"))
    else:
        primary = _int(row.get("ss_claims_month"))
    return (
        is_system,
        -primary,
        -_int(row.get("seconds_period")),
        str(row.get("display_name") or ""),
    )


_ASSIGNEE_UUID_SQL = """
assignee_id ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
"""

LATEST_ASSIGNMENT_SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (h.work_item_id)
        h.work_item_id,
        h.changed_by AS assigner_id,
        NULLIF(btrim(h.new_value), '') AS assignee_id,
        h.changed_at
    FROM ops.eligibility_history h
    WHERE h.column_name = 'assigned_to'
    ORDER BY h.work_item_id, h.changed_at DESC
),
open_assign AS (
    SELECT work_item_id, assigner_id, assignee_id, changed_at
    FROM latest
    WHERE {_ASSIGNEE_UUID_SQL}
)
SELECT
    open_assign.assignee_id,
    open_assign.assigner_id,
    open_assign.work_item_id,
    EXISTS (
        SELECT 1
        FROM ops.eligibility_history s
        WHERE s.work_item_id = open_assign.work_item_id
          AND s.changed_by = open_assign.assignee_id::uuid
          AND s.column_name = 'collection_status'
          AND NULLIF(btrim(s.new_value), '') IS NOT NULL
          AND s.changed_at > open_assign.changed_at
    ) AS finished
FROM open_assign
"""


def _viewer_assignment_rows(
    conn: psycopg.Connection,
    viewer_id: str,
) -> list[dict[str, Any]]:
    """Claims whose latest assign was made by this viewer, grouped by assignee."""
    rows = client.fetchall(
        conn,
        f"""
        SELECT assignee_id AS user_id,
               count(*)::int AS coll_assigned,
               count(*) FILTER (WHERE finished)::int AS coll_finished
        FROM (
{LATEST_ASSIGNMENT_SQL}
        ) assigned
        WHERE assigner_id = %s::uuid
        GROUP BY assignee_id
        """,
        (viewer_id,),
    )
    return [
        {
            "user_id": str(row["user_id"]),
            "coll_assigned": _int(row.get("coll_assigned")),
            "coll_finished": _int(row.get("coll_finished")),
        }
        for row in rows
    ]


def my_assignment_progress(
    conn: psycopg.Connection,
    user_id: str,
) -> dict[str, int]:
    """Claims currently assigned to this person, and how many they finished."""
    uid = str(user_id or "").strip()
    if not uid:
        return {"assigned": 0, "finished": 0}
    row = client.fetchone(
        conn,
        f"""
        SELECT count(*)::int AS assigned,
               count(*) FILTER (WHERE finished)::int AS finished
        FROM (
{LATEST_ASSIGNMENT_SQL}
        ) assigned
        WHERE assignee_id = %s
        """,
        (uid,),
    )
    return {
        "assigned": _int((row or {}).get("assigned")),
        "finished": _int((row or {}).get("finished")),
    }


def team_summary(
    conn: psycopg.Connection,
    roles: list[str] | None,
    *,
    start: datetime,
    end: datetime,
    today: date | None = None,
    team: str | None = None,
    viewer_id: str | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    team_key = resolve_team(roles, team)
    people = list_scoped_users(conn, roles, team_key)
    ids = [p["user_id"] for p in people]
    kpis = summarize_kpis([])
    merged: list[dict[str, Any]] = []
    if people:
        hours = _index_by_user(_hours_rows(conn, ids, start, end, today))
        logins = _index_by_user(_login_rows(conn, ids, start, end))
        work: dict[str, dict[str, Any]] = {}
        if team_key == TEAM_SS:
            work = _index_by_user(_ss_rows(conn, ids, start, end, today))
        elif team_key == TEAM_ELIGIBILITY:
            work = _index_by_user(_elig_rows(conn, ids, start, end, today))
        elif team_key == TEAM_COLLECTION:
            work = _index_by_user(_collection_rows(conn, ids, start, end, today))
        elif team_key == TEAM_SUBMISSION:
            work = _index_by_user(_cpt_rows(conn, ids, start, end, today))
        for person in people:
            uid = person["user_id"]
            row = merge_user_row(person, hours.get(uid), logins.get(uid), work.get(uid))
            merged.append(row)
        merged.sort(key=lambda r: _team_sort_key(team_key, r))
        kpis = summarize_kpis(merged)
        if team_key == TEAM_COLLECTION:
            kpis.update(_collection_team_recovered(conn, ids, today))
            assigned_by_viewer = (
                _index_by_user(_viewer_assignment_rows(conn, viewer_id))
                if viewer_id
                else {}
            )
            for row in merged:
                extra = assigned_by_viewer.get(str(row["user_id"])) or {}
                row["coll_assigned"] = _int(extra.get("coll_assigned"))
                row["coll_finished"] = _int(extra.get("coll_finished"))
    return {
        "team": team_key,
        "teams": visible_teams(roles),
        "period": {"start": _iso(start), "end": _iso(end)},
        "kpis": kpis,
        "people": merged,
        "charts": _chart_payload(merged, team_key),
        "collection_statuses": list(COLLECTION_STATUS_LABELS),
    }


def _ss_recent_rows(
    conn: psycopg.Connection,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = client.fetchall(
        conn,
        f"""
        SELECT flag.dos, flag.carc_kind, flag.workload_status, flag.payment,
               flag.submitter, flag.claim_number, flag.updated_at, flag.submission_date
        FROM ops.pr_queue_flag flag
        {SS_SUBMITTER_JOIN}
        WHERE {SS_WORKLOAD_WHERE}
          AND su.user_id = %s::uuid
          AND {SS_WORK_DAY_SQL} >= %s AND {SS_WORK_DAY_SQL} < %s
        ORDER BY {SS_WORK_DAY_SQL} DESC, flag.updated_at DESC
        LIMIT 25
        """,
        (user_id, start.date(), end.date()),
    )
    return [
        {
            "dos": _iso(r.get("dos")),
            "carc_kind": r.get("carc_kind"),
            "workload_status": r.get("workload_status"),
            "payment": r.get("payment"),
            "submitter": r.get("submitter"),
            "claim_number": r.get("claim_number"),
            "updated_at": _iso(r.get("updated_at")),
            "submission_date": _iso(r.get("submission_date")),
        }
        for r in rows
    ]


def _elig_recent_rows(
    conn: psycopg.Connection,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = client.fetchall(
        conn,
        f"""
        SELECT wi.dos, wi.patient_name, wi.facility_name, wi.eligibility_status,
               h.column_name, h.new_value, h.changed_at
        FROM ops.eligibility_history h
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = h.work_item_id
        WHERE h.changed_by = %s::uuid
          AND h.changed_at >= %s AND h.changed_at < %s
          AND {ELIG_SHEET_WHERE}
        ORDER BY h.changed_at DESC
        LIMIT 25
        """,
        (user_id, start, end),
    )
    return [
        {
            "dos": _iso(r.get("dos")),
            "patient_name": r.get("patient_name"),
            "facility_name": r.get("facility_name"),
            "eligibility_status": r.get("eligibility_status"),
            "column_name": r.get("column_name"),
            "new_value": r.get("new_value"),
            "changed_at": _iso(r.get("changed_at")),
        }
        for r in rows
    ]


def _collection_recent_rows(
    conn: psycopg.Connection,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = client.fetchall(
        conn,
        f"""
        SELECT wi.dos, wi.patient_name, wi.facility_name,
               wi.manual_overrides->>'collection_status' AS collection_status,
               h.column_name, h.new_value, h.changed_at
        FROM ops.eligibility_history h
        JOIN ops.eligibility_work_item wi ON wi.work_item_id = h.work_item_id
        WHERE h.changed_by = %s::uuid
          AND h.changed_at >= %s AND h.changed_at < %s
          AND {COLLECTION_QUEUE_WHERE}
        ORDER BY h.changed_at DESC
        LIMIT 25
        """,
        (user_id, start, end),
    )
    return [
        {
            "dos": _iso(r.get("dos")),
            "patient_name": r.get("patient_name"),
            "facility_name": r.get("facility_name"),
            "collection_status": r.get("collection_status"),
            "column_name": r.get("column_name"),
            "new_value": r.get("new_value"),
            "changed_at": _iso(r.get("changed_at")),
        }
        for r in rows
    ]


def _cpt_recent_rows(
    conn: psycopg.Connection,
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    rows = client.fetchall(
        conn,
        f"""
        SELECT f.dos, f.patient_name, f.facility_name, {CPT_DOMAIN_SQL} AS audit_domain,
               wi.workflow_status, f.rule_code, h.column_name, h.new_value, h.changed_at
        FROM ops.cpt_audit_work_history h
        JOIN ops.cpt_audit_work_item wi ON wi.work_item_id = h.work_item_id
        JOIN billing.cpt_audit_finding f ON f.finding_id = wi.finding_id
        WHERE h.changed_by = %s::uuid
          AND h.changed_at >= %s AND h.changed_at < %s
          AND {CPT_LIVE_DOMAIN_SQL}
        ORDER BY h.changed_at DESC
        LIMIT 25
        """,
        (user_id, start, end),
    )
    return [
        {
            "dos": _iso(r.get("dos")),
            "patient_name": r.get("patient_name"),
            "facility_name": r.get("facility_name"),
            "audit_domain": r.get("audit_domain"),
            "workflow_status": r.get("workflow_status"),
            "rule_code": r.get("rule_code"),
            "column_name": r.get("column_name"),
            "new_value": r.get("new_value"),
            "changed_at": _iso(r.get("changed_at")),
        }
        for r in rows
    ]


def user_detail(
    conn: psycopg.Connection,
    roles: list[str] | None,
    user_id: str,
    *,
    start: datetime,
    end: datetime,
    team: str | None = None,
) -> dict[str, Any] | None:
    team_key = resolve_team(roles, team)
    people = list_scoped_users(conn, roles, team_key)
    match = next((p for p in people if p["user_id"] == user_id), None)
    if not match:
        return None
    hours = client.fetchall(
        conn,
        """
        SELECT started_at::date AS day, COALESCE(SUM(seconds_active), 0)::int AS seconds
        FROM ops.user_activity_slice
        WHERE user_id = %s::uuid
          AND started_at >= %s AND started_at < %s
        GROUP BY 1
        ORDER BY 1
        """,
        (user_id, start, end),
    )
    ss = _ss_recent_rows(conn, user_id, start, end) if team_key == TEAM_SS else []
    eligibility = (
        _elig_recent_rows(conn, user_id, start, end) if team_key == TEAM_ELIGIBILITY else []
    )
    collection = (
        _collection_recent_rows(conn, user_id, start, end) if team_key == TEAM_COLLECTION else []
    )
    cpt_audit = (
        _cpt_recent_rows(conn, user_id, start, end) if team_key == TEAM_SUBMISSION else []
    )
    return {
        "team": team_key,
        "user": {
            "user_id": match["user_id"],
            "display_name": match["display_name"],
            "username": match["username"],
            "roles": match["roles"],
            "last_login_at": _iso(match.get("last_login_at")),
        },
        "hours": [{"day": _iso(r["day"]), "seconds": _int(r["seconds"])} for r in hours],
        "second_submission": ss,
        "eligibility": eligibility,
        "collection": collection,
        "cpt_audit": cpt_audit,
    }


def ss_breakdown(
    conn: psycopg.Connection,
    roles: list[str] | None,
    *,
    grain: str | None = "month",
    year: int | None = None,
    month: str | int | None = None,
    user_id: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    kind = (grain or "month").strip().lower()
    if kind not in {"month", "week", "day"}:
        raise ValueError("grain must be month, week, or day")
    resolve_team(roles, TEAM_SS)
    y = int(year or today.year)
    month_num: int | None = None
    if kind == "day":
        y, month_num = parse_breakdown_month(month, y)
        start_d = date(y, month_num, 1)
        end_d = date(y, month_num, monthrange(y, month_num)[1]) + timedelta(days=1)
    else:
        start_d = date(y, 1, 1)
        end_d = date(y + 1, 1, 1)

    people = list_scoped_users(conn, roles, TEAM_SS)
    member_id = str(user_id) if user_id else None
    if member_id:
        match = next((p for p in people if p["user_id"] == member_id), None)
        if not match:
            raise PermissionError("user not in second_submission scope")
        ids = [match["user_id"]]
        member = {
            "user_id": match["user_id"],
            "display_name": match["display_name"],
        }
        join_sql = SS_SUBMITTER_JOIN
        extra_sql = "AND su.user_id = ANY(%s::uuid[])"
        query_params: tuple[Any, ...] = (ids, start_d, end_d)
    else:
        ids = [p["user_id"] for p in people]
        member = None
        join_sql = ""
        extra_sql = ""
        query_params = (start_d, end_d)

    grouped: dict[date, dict[str, Any]] = {}
    if member_id and not ids:
        rows: list[dict[str, Any]] = []
    else:
        rows = client.fetchall(
            conn,
            f"""
            SELECT
                {SS_DOS_SQL} AS work_day,
                flag.workload_status,
                count(*)::int AS claims,
                COALESCE(SUM({SS_PAYMENT_SQL}), 0) AS payment
            FROM ops.pr_queue_flag flag
            {join_sql}
            WHERE {SS_WORKLOAD_WHERE}
              {extra_sql}
              AND {SS_DOS_SQL} >= %s
              AND {SS_DOS_SQL} < %s
            GROUP BY 1, 2
            """,
            query_params,
        )
    for row in rows:
        work_day = row["work_day"]
        if isinstance(work_day, datetime):
            work_day = work_day.date()
        key = period_start_for(work_day, kind)
        metrics = grouped.setdefault(key, empty_breakdown_row())
        add_ss_status_batch(
            metrics,
            status=row.get("workload_status"),
            claims=_int(row.get("claims")),
            payment=row.get("payment"),
            submitted_with_date=0,
        )

    payload = fill_breakdown_rows(kind, y, grouped, month=month_num)
    payload["member"] = member
    payload["people"] = [
        {"user_id": p["user_id"], "display_name": p["display_name"]} for p in people
    ]
    return payload


NO_ROOT_CAUSE = "No root cause"


def dead_root_cause_sql() -> str:
    """Current Dead claims, one row each, with the root cause shown on the visit."""
    return f"""
        SELECT root_cause, count(*)::int AS n
        FROM (
            SELECT COALESCE(
                NULLIF(btrim(wi.manual_overrides->>'root_cause'), ''),
                NULLIF(btrim(wi.context->>'root_cause'), ''),
                (
                    SELECT COALESCE(
                        NULLIF(btrim(sf.payload->>'ROOTCAUSE'), ''),
                        NULLIF(btrim(sf.payload->>'ROOT_CAUSE'), ''),
                        NULLIF(btrim(sf.payload->>'root_cause'), '')
                    )
                    FROM analytics.snowflake_visit_kpi sf
                    WHERE sf.emr_id = wi.emr_patient_id
                      AND sf.date_of_service = wi.dos
                    LIMIT 1
                )
            ) AS root_cause
            FROM ops.eligibility_work_item wi
            JOIN analytics.collection_queue_member m
              ON m.work_item_id = wi.work_item_id
             AND m.bucket = 'dead'
        ) dead_claims
        GROUP BY root_cause
    """


def rollup_dead_root_causes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count and share of current Dead claims for each root cause."""
    counts: dict[str, int] = {}
    for row in rows:
        raw = row.get("root_cause")
        label = canonical_root_cause(None if raw is None else str(raw))
        if not label:
            label = NO_ROOT_CAUSE
        counts[label] = _int(counts.get(label)) + _int(row.get("n"))
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    out: list[dict[str, Any]] = []
    for label, count in ordered:
        percent = round(100.0 * count / total, 1) if total else 0.0
        out.append({"label": label, "count": count, "percent": percent})
    return {"total": total, "rows": out}


def collection_dead_root_causes(
    conn: psycopg.Connection,
    roles: list[str] | None,
) -> dict[str, Any]:
    resolve_team(roles, TEAM_COLLECTION)
    rows = client.fetchall(conn, dead_root_cause_sql())
    return rollup_dead_root_causes(rows)


def collection_root_cause_breakdown(
    conn: psycopg.Connection,
    roles: list[str] | None,
    *,
    year: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Last root cause on each claim, counted in the DOS month for whoever wrote it last."""
    today = today or date.today()
    resolve_team(roles, TEAM_COLLECTION)
    y = int(year or today.year)
    start_d = date(y, 1, 1)
    end_d = date(y + 1, 1, 1)
    rows = client.fetchall(
        conn,
        """
        SELECT month_start, user_id, root_cause, count(*)::int AS n
        FROM (
            SELECT DISTINCT ON (h.work_item_id)
                date_trunc('month', wi.dos)::date AS month_start,
                h.changed_by AS user_id,
                NULLIF(btrim(h.new_value), '') AS root_cause
            FROM ops.eligibility_history h
            JOIN ops.eligibility_work_item wi ON wi.work_item_id = h.work_item_id
            WHERE h.column_name = 'root_cause'
              AND wi.dos >= %s
              AND wi.dos < %s
              AND NULLIF(btrim(h.new_value), '') IS NOT NULL
            ORDER BY h.work_item_id, h.changed_at DESC
        ) last_cause
        GROUP BY month_start, user_id, root_cause
        """,
        (start_d, end_d),
    )
    payload = rollup_root_causes(y, rows)
    members = list_scoped_users(conn, roles, TEAM_COLLECTION)
    payload["people"] = rollup_root_cause_people(rows, members, payload["labels"])
    return payload

