"""Workbook tables for the Team Analytics dashboard."""

from __future__ import annotations

from typing import Any

from cashflow_db.repository.work_analytics import (
    TEAM_COLLECTION,
    TEAM_ELIGIBILITY,
    TEAM_SS,
    TEAM_SUBMISSION,
    KNOWN_TEAMS,
)

TEAM_COPY: dict[str, dict[str, str]] = {
    TEAM_SS: {
        "title": "Second Submission",
        "subtitle": (
            "Claims counted when the person sets themselves as Submitter. "
            "Hours are active time on the system."
        ),
        "people": (
            "Claims are Submitter = this person. Today / week / month use "
            "submission date, not Date of Service. Click a row for daily hours "
            "and recent claims."
        ),
        "prefix": "SS",
    },
    TEAM_ELIGIBILITY: {
        "title": "Eligibility",
        "subtitle": (
            "Sheet work for the posting team. Collection denied visits are "
            "counted on the Collection tab."
        ),
        "people": (
            "Touched is distinct sheet items this person edited. Completed is "
            "terminal sheet status. Click a row for recent edits."
        ),
        "prefix": "Eligibility",
    },
    TEAM_COLLECTION: {
        "title": "Collection",
        "subtitle": (
            "Denied-visit collection queue. Counts are today and this month. "
            "Sheet eligibility is on the Eligibility tab."
        ),
        "people": (
            "Edited is distinct claims this person changed. Assigned is claims "
            "you assigned them; Finished is how many of those they set a "
            "Collection Status on afterwards. Click a row for recent edits."
        ),
        "prefix": "Collection",
    },
    TEAM_SUBMISSION: {
        "title": "Submission",
        "subtitle": (
            "CPT and ICD audit work. Demo findings are excluded. Resolved includes ignored."
        ),
        "people": (
            "Touched is distinct audit items this person edited. Resolved is "
            "resolved or ignored. Click a row for recent edits."
        ),
        "prefix": "Submission",
    },
}

EXPORT_FILENAMES = {
    TEAM_SS: "ss-team-analytics.xlsx",
    TEAM_ELIGIBILITY: "eligibility-team-analytics.xlsx",
    TEAM_COLLECTION: "collection-team-analytics.xlsx",
    TEAM_SUBMISSION: "submission-team-analytics.xlsx",
}

CLAIM_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("payment", "Payment", True),
    ("claims", "Claims", False),
    ("total_submitted_claims", "Total submitted claims", False),
    ("submitted", "Submitted", False),
    ("paid", "Paid", False),
    ("pending", "Pending", False),
    ("denied", "Denied", False),
    ("corrected", "Corrected", False),
    ("timely_filing", "Timely Filing", False),
)

SS_HINT = "Submitter / submission date"
HOURS_TITLE = "Hours by person"
HOURS_DETAIL = "Active time in the selected period"
OUTCOMES_TITLE = "Second Submission outcomes"
OUTCOMES_DETAIL = "Paid / denied / Timely Filing"
CLAIM_TITLE = "Claim analysis"
CLAIM_MEMBER = "Claims this person set as Submitter."
CLAIM_ALL = (
    "All team is Date of Service for every clinic visit (Payment / Status). "
    "Pick a member to see only their Submitter rows."
)
ROOT_TITLE = "Root cause by month"
ROOT_DETAIL = (
    "Months follow Date of Service. Each claim counts once, under the last "
    "root cause saved on it. The people table is who wrote that last value."
)
DEAD_TITLE = "Dead by root cause"
DEAD_DETAIL = (
    "Denied and Overdue visits marked Dead. Each bar is one root cause, "
    "with its count and share of all Dead claims."
)


def format_hours(seconds: Any) -> str:
    total = max(0, int(round(_num(seconds))))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours == 0 and minutes == 0:
        return "<1m" if total > 0 else "0h"
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def format_money(value: Any) -> str:
    amount = int(round(_num(value)))
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,}"


def export_tables(
    summaries: dict[str, dict[str, Any]],
    *,
    breakdown: dict[str, Any] | None = None,
    causes: dict[str, Any] | None = None,
    dead: dict[str, Any] | None = None,
) -> list[tuple[str, list[Any], list[list[Any]]]]:
    """One sheet per dashboard section, in tab order."""
    tables: list[tuple[str, list[Any], list[list[Any]]]] = []
    for team in KNOWN_TEAMS:
        summary = summaries.get(team)
        if not summary:
            continue
        copy = TEAM_COPY[team]
        prefix = copy["prefix"]
        tables.append((f"{prefix} Summary", *_summary_table(team, summary, breakdown, causes, dead)))
        tables.append((f"{prefix} Hours", *_hours_table(summary)))
        if team == TEAM_SS:
            tables.append((f"{prefix} Outcomes", *_outcomes_table(summary)))
        tables.append((f"{prefix} People", *_people_table(team, summary)))
        if team == TEAM_SS:
            tables.append((f"{prefix} Claim analysis", *_claim_table(breakdown)))
        if team == TEAM_COLLECTION:
            tables.append(("Collection Root causes", *_root_cause_table(causes)))
            tables.append(("Collection Root people", *_root_people_table(causes)))
            tables.append(("Collection Dead", *_dead_table(dead)))
    return tables


def _summary_table(
    team: str,
    summary: dict[str, Any],
    breakdown: dict[str, Any] | None,
    causes: dict[str, Any] | None,
    dead: dict[str, Any] | None,
) -> tuple[list[Any], list[list[Any]]]:
    copy = TEAM_COPY[team]
    period = summary.get("period") or {}
    rows: list[list[Any]] = [
        ["Title", copy["title"], ""],
        ["Description", copy["subtitle"], ""],
        ["People note", copy["people"], ""],
        ["Period start", period.get("start") or "", ""],
        ["Period end", period.get("end") or "", ""],
        [HOURS_TITLE, HOURS_DETAIL, ""],
    ]
    rows.extend(_kpi_rows(team, summary.get("kpis") or {}))
    if team == TEAM_SS:
        rows.append([OUTCOMES_TITLE, OUTCOMES_DETAIL, ""])
        member = (breakdown or {}).get("member") or {}
        member_name = member.get("display_name") or "All team"
        note = CLAIM_MEMBER if member.get("display_name") else CLAIM_ALL
        grain = str((breakdown or {}).get("grain") or "")
        rows.append([CLAIM_TITLE, note, member_name])
        rows.append(["Claim grain", grain, (breakdown or {}).get("year") or ""])
    if team == TEAM_COLLECTION:
        year = (causes or {}).get("year") or ""
        rows.append([ROOT_TITLE, ROOT_DETAIL, year])
        rows.append(
            [
                "Root cause people",
                f"Each person, for claims whose Date of Service is in {year}.",
                "",
            ]
        )
        total = (dead or {}).get("total") or 0
        rows.append([DEAD_TITLE, DEAD_DETAIL, total])
    return ["Item", "Value", "Detail"], rows


def _kpi_rows(team: str, kpis: dict[str, Any]) -> list[list[Any]]:
    if team == TEAM_ELIGIBILITY:
        cards = [
            ("Touched today", _int(kpis.get("elig_touched_today")), "Sheet edits"),
            ("Touched week", _int(kpis.get("elig_touched_week")), "Sheet edits"),
            ("Touched month", _int(kpis.get("elig_touched_month")), "Sheet edits"),
            ("Completed today", _int(kpis.get("elig_completed_today")), "Completed / rejected"),
            ("Completed week", _int(kpis.get("elig_completed_week")), "Completed / rejected"),
            ("Completed month", _int(kpis.get("elig_completed_month")), "Completed / rejected"),
            ("Sheet money", format_money(kpis.get("elig_money")), "Ledger in this period"),
        ]
    elif team == TEAM_COLLECTION:
        cards = [
            ("Edited today", _int(kpis.get("coll_touched_today")), "Distinct claims"),
            ("Edited month", _int(kpis.get("coll_touched_month")), "Distinct claims"),
            ("Recovered today", _int(kpis.get("coll_recovered_today")), "Paid after their work"),
            ("Recovered month", _int(kpis.get("coll_recovered_month")), "Paid after their work"),
            ("Money today", format_money(kpis.get("coll_money_today")), "Insurance + patient"),
            ("Money month", format_money(kpis.get("coll_money_month")), "Insurance + patient"),
        ]
    elif team == TEAM_SUBMISSION:
        cards = [
            ("Touched today", _touched(kpis, "_today"), "CPT + ICD"),
            ("Touched week", _touched(kpis, "_week"), "CPT + ICD"),
            ("Touched month", _touched(kpis, "_month"), "CPT + ICD"),
            ("Resolved today", _resolved(kpis, "_today"), "Resolved / ignored"),
            ("Resolved week", _resolved(kpis, "_week"), "Resolved / ignored"),
            ("Resolved month", _resolved(kpis, "_month"), "Resolved / ignored"),
            ("CPT resolved", _int(kpis.get("cpt_resolved_month")), "This month"),
            ("ICD resolved", _int(kpis.get("icd_resolved_month")), "This month"),
        ]
    else:
        cards = [
            ("Claims today", _int(kpis.get("ss_claims_today")), SS_HINT),
            ("Claims week", _int(kpis.get("ss_claims_week")), SS_HINT),
            ("Claims month", _int(kpis.get("ss_claims_month")), SS_HINT),
            ("Payment", format_money(kpis.get("ss_money")), SS_HINT),
            ("Paid", _int(kpis.get("ss_paid")), SS_HINT),
            ("Denied", _int(kpis.get("ss_denied")), SS_HINT),
            ("Timely Filing", _int(kpis.get("ss_timely_filing")), SS_HINT),
        ]
    return [[label, value, hint] for label, value, hint in cards]


def _hours_table(summary: dict[str, Any]) -> tuple[list[Any], list[list[Any]]]:
    charts = summary.get("charts") or {}
    rows = [
        [row.get("name") or "", row.get("hours") or 0]
        for row in charts.get("hours") or []
    ]
    if not rows:
        rows = [["No tracked hours yet", "Hours start after this feature is deployed."]]
    return ["Person", "Hours"], rows


def _outcomes_table(summary: dict[str, Any]) -> tuple[list[Any], list[list[Any]]]:
    charts = summary.get("charts") or {}
    rows = [
        [
            row.get("name") or "",
            _int(row.get("paid")),
            _int(row.get("denied")),
            _int(row.get("timely_filing")),
        ]
        for row in charts.get("outcomes") or []
    ]
    if not rows:
        rows = [["No claims in this period", "", "", ""]]
    return ["Person", "Paid", "Denied", "Timely Filing"], rows


def _people_table(
    team: str, summary: dict[str, Any]
) -> tuple[list[Any], list[list[Any]]]:
    people = summary.get("people") or []
    statuses = list(summary.get("collection_statuses") or [])
    headers = ["Person", "Roles", "Hours today"]
    if team != TEAM_COLLECTION:
        headers.append("Hours week")
    headers.append("Hours month")
    if team == TEAM_ELIGIBILITY:
        headers.extend(
            [
                "Touched today",
                "Touched week",
                "Touched month",
                "Completed today",
                "Completed week",
                "Completed month",
                "Sheet money",
            ]
        )
    elif team == TEAM_COLLECTION:
        headers.extend(["Edited today", "Edited month", "Assigned", "Finished"])
        headers.extend(f"{label} today" for label in statuses)
        headers.extend(f"{label} month" for label in statuses)
        headers.extend(
            ["Recovered today", "Recovered month", "Money today", "Money month"]
        )
    elif team == TEAM_SUBMISSION:
        headers.extend(
            [
                "Touched today",
                "Touched week",
                "Touched month",
                "Resolved today",
                "Resolved week",
                "Resolved month",
                "CPT resolved",
                "ICD resolved",
            ]
        )
    else:
        headers.extend(
            [
                "Claims today",
                "Claims week",
                "Claims month",
                "Paid",
                "Denied",
                "Timely Filing",
            ]
        )
    if not people:
        return headers, [["No people in scope", "Leads only see their own team."]]
    return headers, [_person_row(team, person, statuses) for person in people]


def _person_row(team: str, person: dict[str, Any], statuses: list[str]) -> list[Any]:
    roles = person.get("roles") or []
    row: list[Any] = [
        person.get("display_name") or "",
        ", ".join(str(role) for role in roles),
        format_hours(person.get("seconds_today")),
    ]
    if team != TEAM_COLLECTION:
        row.append(format_hours(person.get("seconds_week")))
    row.append(format_hours(person.get("seconds_month")))
    if team == TEAM_ELIGIBILITY:
        row.extend(
            [
                _int(person.get("elig_touched_today")),
                _int(person.get("elig_touched_week")),
                _int(person.get("elig_touched_month")),
                _int(person.get("elig_completed_today")),
                _int(person.get("elig_completed_week")),
                _int(person.get("elig_completed_month")),
                format_money(person.get("elig_money")),
            ]
        )
    elif team == TEAM_COLLECTION:
        row.extend(
            [
                _int(person.get("coll_touched_today")),
                _int(person.get("coll_touched_month")),
                _int(person.get("coll_assigned")),
                _int(person.get("coll_finished")),
            ]
        )
        row.extend(_status_count(person, "today", label) for label in statuses)
        row.extend(_status_count(person, "month", label) for label in statuses)
        row.extend(
            [
                _int(person.get("coll_recovered_today")),
                _int(person.get("coll_recovered_month")),
                format_money(person.get("coll_money_today")),
                format_money(person.get("coll_money_month")),
            ]
        )
    elif team == TEAM_SUBMISSION:
        row.extend(
            [
                _touched(person, "_today"),
                _touched(person, "_week"),
                _touched(person, "_month"),
                _resolved(person, "_today"),
                _resolved(person, "_week"),
                _resolved(person, "_month"),
                _int(person.get("cpt_resolved_month")),
                _int(person.get("icd_resolved_month")),
            ]
        )
    else:
        row.extend(
            [
                _int(person.get("ss_claims_today")),
                _int(person.get("ss_claims_week")),
                _int(person.get("ss_claims_month")),
                _int(person.get("ss_paid")),
                _int(person.get("ss_denied")),
                _int(person.get("ss_timely_filing")),
            ]
        )
    return row


def _claim_table(
    breakdown: dict[str, Any] | None,
) -> tuple[list[Any], list[list[Any]]]:
    headers = ["Period", *[label for _key, label, _money in CLAIM_COLUMNS]]
    rows = list((breakdown or {}).get("rows") or [])
    totals = (breakdown or {}).get("totals")
    if totals:
        rows = [*rows, totals]
    if not rows:
        return headers, [["No claims in this window"]]
    return headers, [_claim_row(row) for row in rows]


def _claim_row(row: dict[str, Any]) -> list[Any]:
    values: list[Any] = [row.get("period") or ""]
    for key, _label, as_money in CLAIM_COLUMNS:
        raw = row.get(key) or 0
        values.append(format_money(raw) if as_money else _int(raw))
    return values


def _root_cause_table(
    causes: dict[str, Any] | None,
) -> tuple[list[Any], list[list[Any]]]:
    labels = list((causes or {}).get("labels") or [])
    headers = ["Month", "Top", *labels]
    rows = (causes or {}).get("rows") or []
    if not rows:
        return headers, [["No root causes in this year"]]
    return headers, [_cause_row(row, labels) for row in rows]


def _root_people_table(
    causes: dict[str, Any] | None,
) -> tuple[list[Any], list[list[Any]]]:
    labels = list((causes or {}).get("labels") or [])
    headers = ["Person", "Top", *labels]
    people = (causes or {}).get("people") or []
    if not people:
        return headers, [["No collectors in scope"]]
    body = []
    for person in people:
        body.append(
            [
                person.get("display_name") or "",
                _top_label(person),
                *[_int((person.get("counts") or {}).get(label)) for label in labels],
            ]
        )
    return headers, body


def _cause_row(row: dict[str, Any], labels: list[str]) -> list[Any]:
    counts = row.get("counts") or {}
    return [
        row.get("period") or "",
        _top_label(row),
        *[_int(counts.get(label)) for label in labels],
    ]


def _dead_table(dead: dict[str, Any] | None) -> tuple[list[Any], list[list[Any]]]:
    headers = ["Root cause", "Count", "Percent", "Shown"]
    rows = (dead or {}).get("rows") or []
    if not rows:
        return headers, [["No dead claims"]]
    body = []
    for row in rows:
        count = _int(row.get("count"))
        percent = _num(row.get("percent"))
        share = f"{percent:.1f}%"
        body.append([row.get("label") or "", count, share, f"{count} ({share})"])
    return headers, body


def _top_label(row: dict[str, Any]) -> str:
    top = row.get("top") or ""
    if not top:
        return "—"
    return f"{top} ({_int(row.get('top_count'))})"


def _status_count(person: dict[str, Any], period: str, label: str) -> int:
    key = "coll_status_today" if period == "today" else "coll_status_month"
    bag = person.get(key) or {}
    return _int(bag.get(label))


def _touched(row: dict[str, Any], suffix: str) -> int:
    return _int(row.get(f"cpt_touched{suffix}")) + _int(row.get(f"icd_touched{suffix}"))


def _resolved(row: dict[str, Any], suffix: str) -> int:
    return _int(row.get(f"cpt_resolved{suffix}")) + _int(row.get(f"icd_resolved{suffix}"))


def _int(value: Any) -> int:
    return int(round(_num(value)))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
