"""Match WebPT billing lines to RevFlow payment lines."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .insurance_map import InsuranceRule, payor_matches_insurance
from .load_webpt import WebptLine
from .normalize import format_money, split_carcs
from .parse_revflow_eob import PaymentLine


@dataclass
class MatchedLine:
    webpt: WebptLine
    payment: PaymentLine | None = None
    status: str = "pending"
    match_level: str = "none"
    confidence: float = 0.0
    insurance_mismatch: str = "no"
    unmatched_reason: str = ""


@dataclass
class MatchResult:
    lines: list[MatchedLine] = field(default_factory=list)
    orphan_payments: list[PaymentLine] = field(default_factory=list)


def _classify_status(payment: PaymentLine) -> str:
    carcs = {code.upper() for code in split_carcs(payment.carcs)}
    if payment.paid_amount > 0:
        if carcs & {"PR-2", "OA-23"}:
            return "secondary_pending"
        if carcs & {"PR-1", "PR-3"} or payment.deductible_amount > 0:
            return "patient_responsibility"
        return "paid"
    if carcs & {"PR-2", "OA-23"}:
        return "secondary_pending"
    if carcs & {"PR-1", "PR-3"} or payment.deductible_amount > 0:
        return "patient_responsibility"
    return "zero_pay"


def _pick_payment(candidates: list[PaymentLine]) -> PaymentLine:
    return max(
        candidates,
        key=lambda item: (item.paid_amount, item.allowed_amount, item.billed_amount),
    )


def _confidence(
    *,
    match_level: str,
    insurance_mismatch: bool,
    has_payment: bool,
) -> float:
    if not has_payment:
        return 0.0
    score = {"line": 1.0, "line_no_modifier": 0.9, "visit": 0.75}.get(match_level, 0.5)
    if insurance_mismatch:
        score -= 0.2
    return max(0.0, min(1.0, score))


def match_lines(
    webpt_lines: list[WebptLine],
    payments: list[PaymentLine],
    rules: list[InsuranceRule],
) -> MatchResult:
    by_full: dict[tuple, list[PaymentLine]] = defaultdict(list)
    by_no_mod: dict[tuple, list[PaymentLine]] = defaultdict(list)
    by_visit: dict[tuple, list[PaymentLine]] = defaultdict(list)

    for payment in payments:
        by_full[
            (payment.name_key, payment.date_of_service, payment.cpt_code, payment.modifier)
        ].append(payment)
        by_no_mod[(payment.name_key, payment.date_of_service, payment.cpt_code)].append(payment)
        by_visit[(payment.name_key, payment.date_of_service)].append(payment)

    used_payment_ids: set[int] = set()
    matched: list[MatchedLine] = []

    for webpt in webpt_lines:
        payment: PaymentLine | None = None
        match_level = "none"

        full_key = (webpt.name_key, webpt.date_of_service, webpt.cpt_code, webpt.modifier)
        no_mod_key = (webpt.name_key, webpt.date_of_service, webpt.cpt_code)
        visit_key = (webpt.name_key, webpt.date_of_service)

        lookup_by_level = {
            "line": by_full,
            "line_no_modifier": by_no_mod,
            "visit": by_visit,
        }
        for key, level in (
            (full_key, "line"),
            (no_mod_key, "line_no_modifier"),
            (visit_key, "visit"),
        ):
            candidates = [
                item
                for item in lookup_by_level[level][key]
                if id(item) not in used_payment_ids
            ]
            if level == "visit":
                candidates = [
                    item
                    for item in candidates
                    if item.cpt_code == webpt.cpt_code or item.paid_amount > 0
                ]
            if candidates:
                payment = _pick_payment(candidates)
                used_payment_ids.add(id(payment))
                match_level = level
                break

        insurance_values = [webpt.ins_name, webpt.insurance_note]
        insurance_ok = True
        if payment is not None:
            insurance_ok = payor_matches_insurance(payment.payor, insurance_values, rules)

        if payment is None:
            matched.append(
                MatchedLine(
                    webpt=webpt,
                    status="pending",
                    match_level="none",
                    confidence=0.0,
                    insurance_mismatch="no",
                    unmatched_reason="no_payment_in_window",
                )
            )
            continue

        status = _classify_status(payment)
        matched.append(
            MatchedLine(
                webpt=webpt,
                payment=payment,
                status=status,
                match_level=match_level,
                confidence=_confidence(
                    match_level=match_level,
                    insurance_mismatch=not insurance_ok,
                    has_payment=True,
                ),
                insurance_mismatch="yes" if not insurance_ok else "no",
                unmatched_reason="insurance_mismatch" if not insurance_ok else "",
            )
        )

    orphan_payments = [payment for payment in payments if id(payment) not in used_payment_ids]
    return MatchResult(lines=matched, orphan_payments=orphan_payments)


def aggregate_visits(matched_lines: list[MatchedLine]) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = {}

    for item in matched_lines:
        webpt = item.webpt
        key = (webpt.patient_id, webpt.date_of_service)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
                "facility_name": webpt.facility_name,
                "date_of_service": webpt.date_of_service,
                "total_billed_cpts": 0,
                "total_paid": 0.0,
                "paid_lines": 0,
                "pending_lines": 0,
            }
            buckets[key] = bucket

        bucket["total_billed_cpts"] += 1
        if item.payment is not None:
            bucket["total_paid"] += item.payment.paid_amount
            if item.payment.paid_amount > 0:
                bucket["paid_lines"] += 1
        if item.status == "pending":
            bucket["pending_lines"] += 1

    rows: list[dict] = []
    for bucket in buckets.values():
        if bucket["pending_lines"] == 0 and bucket["paid_lines"] > 0:
            visit_status = "paid"
        elif bucket["paid_lines"] > 0:
            visit_status = "partial"
        else:
            visit_status = "pending"
        bucket["total_paid"] = format_money(bucket["total_paid"])
        bucket["visit_status"] = visit_status
        rows.append(bucket)
    rows.sort(key=lambda row: (row["webpt_patient_id"], row["date_of_service"]))
    return rows


def aggregate_patients(matched_lines: list[MatchedLine]) -> list[dict]:
    buckets: dict[str, dict] = {}

    for item in matched_lines:
        webpt = item.webpt
        bucket = buckets.get(webpt.patient_id)
        if bucket is None:
            bucket = {
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
                "facility_name": webpt.facility_name,
                "case_id": webpt.case_id,
                "ins_name": webpt.ins_name,
                "assigned_therapist": webpt.assigned_therapist,
                "auth_ins_visits": webpt.auth_ins_visits,
                "visit_keys": set(),
                "paid_visit_keys": set(),
                "pending_visit_keys": set(),
                "total_paid": 0.0,
                "payors": Counter(),
            }
            buckets[webpt.patient_id] = bucket

        visit_key = webpt.date_of_service
        bucket["visit_keys"].add(visit_key)
        if item.status == "pending":
            bucket["pending_visit_keys"].add(visit_key)
        if item.payment is not None and item.payment.paid_amount > 0:
            bucket["paid_visit_keys"].add(visit_key)
            bucket["total_paid"] += item.payment.paid_amount
            bucket["payors"][item.payment.payor] += 1

    rows: list[dict] = []
    for bucket in buckets.values():
        payors: Counter = bucket.pop("payors")
        primary_payor = payors.most_common(1)[0][0] if payors else ""
        rows.append(
            {
                "webpt_patient_id": bucket["webpt_patient_id"],
                "patient_name": bucket["patient_name"],
                "dob": bucket["dob"],
                "facility_name": bucket["facility_name"],
                "case_id": bucket["case_id"],
                "ins_name": bucket["ins_name"],
                "assigned_therapist": bucket["assigned_therapist"],
                "auth_ins_visits": bucket["auth_ins_visits"],
                "visits_total": len(bucket["visit_keys"]),
                "visits_paid": len(bucket["paid_visit_keys"]),
                "visits_pending": len(bucket["pending_visit_keys"]),
                "total_paid": format_money(bucket["total_paid"]),
                "primary_payor": primary_payor,
            }
        )
    rows.sort(key=lambda row: row["webpt_patient_id"])
    return rows
