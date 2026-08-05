"""Match WebPT billing lines to RevFlow payment lines."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .insurance_map import InsuranceRule, payor_matches_insurance
from .load_webpt import WebptLine
from .normalize import format_date, format_money, parse_date, split_carcs
from .parse_revflow_eob import PaymentLine, is_bonus_payment
from .load_transaction_tracker import apply_deposit_dates

# Reject near-zero garbage pairings from linear_sum_assignment.
ASSIGNMENT_SCORE_FLOOR = 0.2


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


@dataclass
class _VisitProfile:
    patient_id: str
    name_key: str
    date_of_service: str
    cpt_codes: set[str]
    insurance_values: list[str]


@dataclass
class _PaymentPerson:
    person_key: str
    payments: list[PaymentLine]
    cpt_codes: set[str]
    total_paid: float
    total_billed: float


def _normalize_dos(value: str) -> str:
    return format_date(parse_date(value)) or (value or "").strip()


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


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _collision_keys(webpt_lines: list[WebptLine]) -> set[tuple[str, str]]:
    patients_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for line in webpt_lines:
        patients_by_key[(line.name_key, line.date_of_service)].add(line.patient_id)
    return {key for key, patients in patients_by_key.items() if len(patients) >= 2}


def _visit_profiles(webpt_lines: list[WebptLine], name_key: str, dos: str) -> list[_VisitProfile]:
    by_patient: dict[str, _VisitProfile] = {}
    for line in webpt_lines:
        if line.name_key != name_key or line.date_of_service != dos:
            continue
        profile = by_patient.get(line.patient_id)
        if profile is None:
            profile = _VisitProfile(
                patient_id=line.patient_id,
                name_key=line.name_key,
                date_of_service=line.date_of_service,
                cpt_codes=set(),
                insurance_values=[],
            )
            by_patient[line.patient_id] = profile
        if line.cpt_code:
            profile.cpt_codes.add(line.cpt_code)
        for value in (line.ins_name, line.insurance_note):
            text = (value or "").strip()
            if text and text not in profile.insurance_values:
                profile.insurance_values.append(text)
    return list(by_patient.values())


def _payment_persons(
    payments: list[PaymentLine],
    name_key: str,
    dos: str,
) -> list[_PaymentPerson]:
    groups: dict[str, _PaymentPerson] = {}
    for payment in payments:
        pay_dos = _normalize_dos(payment.date_of_service)
        if payment.name_key != name_key or pay_dos != dos:
            continue
        person_key = (payment.revflow_patient_id or "").strip() or f"__anon_{id(payment)}"
        person = groups.get(person_key)
        if person is None:
            person = _PaymentPerson(
                person_key=person_key,
                payments=[],
                cpt_codes=set(),
                total_paid=0.0,
                total_billed=0.0,
            )
            groups[person_key] = person
        person.payments.append(payment)
        if payment.cpt_code:
            person.cpt_codes.add(payment.cpt_code)
        person.total_paid += payment.paid_amount
        person.total_billed += payment.billed_amount
    return list(groups.values())


def score_person_visit(
    person: _PaymentPerson,
    visit: _VisitProfile,
    rules: list[InsuranceRule],
) -> float:
    """Score a payment-person against a WebPT visit for collision assignment."""
    jaccard = _jaccard(person.cpt_codes, visit.cpt_codes)
    if jaccard <= 0:
        return 0.0

    payor = next((p.payor for p in person.payments if (p.payor or "").strip()), "")
    insurance_ok = payor_matches_insurance(payor, visit.insurance_values, rules) if payor else True
    insurance_term = 0.2 if insurance_ok else -0.1

    n_person = max(len(person.cpt_codes), 1)
    n_visit = max(len(visit.cpt_codes), 1)
    count_proximity = 1.0 - (abs(n_person - n_visit) / max(n_person, n_visit))

    # Secondary amount signal: prefer similar CPT cardinality already; scale paid
    # lightly so equal-CPT ties break toward fuller payment groups without dominating.
    amount_term = 0.05 * min(1.0, person.total_paid / 500.0) if person.total_paid > 0 else 0.0

    return jaccard + insurance_term + 0.1 * count_proximity + amount_term


def optimal_assignment(
    score_matrix: np.ndarray,
    *,
    score_floor: float = ASSIGNMENT_SCORE_FLOOR,
) -> list[tuple[int, int, float]]:
    """Max-weight 1:1 assignment via SciPy (minimize -score).

    Returns (row, col, score) pairs that clear ``score_floor``.
    """
    if score_matrix.size == 0:
        return []
    if score_matrix.ndim != 2:
        raise ValueError("score_matrix must be 2-D")

    row_ind, col_ind = linear_sum_assignment(-score_matrix)
    assigned: list[tuple[int, int, float]] = []
    for row, col in zip(row_ind, col_ind):
        score = float(score_matrix[row, col])
        if score >= score_floor:
            assigned.append((int(row), int(col), score))
    return assigned


def _assign_collision_scopes(
    webpt_lines: list[WebptLine],
    payments: list[PaymentLine],
    rules: list[InsuranceRule],
    collision_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], set[int]]:
    """Map (webpt patient_id, DOS) -> allowed payment object ids for collisions."""
    scopes: dict[tuple[str, str], set[int]] = {}

    for name_key, dos in collision_keys:
        visits = _visit_profiles(webpt_lines, name_key, dos)
        persons = _payment_persons(payments, name_key, dos)

        # Default: collision visits get no payments until assigned.
        for visit in visits:
            scopes[(visit.patient_id, dos)] = set()

        if not visits or not persons:
            continue

        matrix = np.zeros((len(persons), len(visits)), dtype=float)
        for i, person in enumerate(persons):
            for j, visit in enumerate(visits):
                matrix[i, j] = score_person_visit(person, visit, rules)

        for person_idx, visit_idx, _score in optimal_assignment(matrix):
            visit = visits[visit_idx]
            person = persons[person_idx]
            scopes[(visit.patient_id, dos)] = {id(payment) for payment in person.payments}

    return scopes


def _match_one_line(
    webpt: WebptLine,
    *,
    by_full: dict[tuple, list[PaymentLine]],
    by_no_mod: dict[tuple, list[PaymentLine]],
    by_visit: dict[tuple, list[PaymentLine]],
    used_payment_ids: set[int],
    allowed_payment_ids: set[int] | None,
    rules: list[InsuranceRule],
) -> MatchedLine:
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
            and (allowed_payment_ids is None or id(item) in allowed_payment_ids)
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
        return MatchedLine(
            webpt=webpt,
            status="pending",
            match_level="none",
            confidence=0.0,
            insurance_mismatch="no",
            unmatched_reason="no_payment_in_window",
        )

    status = _classify_status(payment)
    return MatchedLine(
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


def match_lines(
    webpt_lines: list[WebptLine],
    payments: list[PaymentLine],
    rules: list[InsuranceRule],
) -> MatchResult:
    by_full: dict[tuple, list[PaymentLine]] = defaultdict(list)
    by_no_mod: dict[tuple, list[PaymentLine]] = defaultdict(list)
    by_visit: dict[tuple, list[PaymentLine]] = defaultdict(list)

    for payment in payments:
        dos = _normalize_dos(payment.date_of_service)
        by_full[(payment.name_key, dos, payment.cpt_code, payment.modifier)].append(payment)
        by_no_mod[(payment.name_key, dos, payment.cpt_code)].append(payment)
        by_visit[(payment.name_key, dos)].append(payment)

    collision_keys = _collision_keys(webpt_lines)
    collision_scopes = _assign_collision_scopes(
        webpt_lines, payments, rules, collision_keys
    )

    used_payment_ids: set[int] = set()
    matched: list[MatchedLine] = []

    for webpt in webpt_lines:
        visit_key = (webpt.patient_id, webpt.date_of_service)
        name_dos = (webpt.name_key, webpt.date_of_service)
        if name_dos in collision_keys:
            allowed: set[int] | None = collision_scopes.get(visit_key, set())
        else:
            allowed = None

        matched.append(
            _match_one_line(
                webpt,
                by_full=by_full,
                by_no_mod=by_no_mod,
                by_visit=by_visit,
                used_payment_ids=used_payment_ids,
                allowed_payment_ids=allowed,
                rules=rules,
            )
        )

    orphan_payments = [payment for payment in payments if id(payment) not in used_payment_ids]
    return MatchResult(lines=matched, orphan_payments=orphan_payments)


def _empty_check_fields() -> dict[str, str]:
    return {
        "primary_check_number": "",
        "primary_check_date": "",
        "primary_check_amount": "",
        "secondary_check_number": "",
        "secondary_check_date": "",
        "secondary_check_amount": "",
    }


def _check_fields_from_rollup(checks: dict[str, dict]) -> dict[str, str]:
    """Pick primary/secondary checks ordered by eob_date then check number."""
    fields = _empty_check_fields()
    if not checks:
        return fields

    def sort_key(item: tuple[str, dict]) -> tuple:
        check_num, meta = item
        parsed = parse_date(meta["date"])
        # Unparsed/missing dates sort last
        return (parsed is None, parsed or format_date(None), check_num)

    ordered = sorted(checks.items(), key=sort_key)
    slots = (
        ("primary_check_number", "primary_check_date", "primary_check_amount"),
        ("secondary_check_number", "secondary_check_date", "secondary_check_amount"),
    )
    for (num_key, date_key, amount_key), (check_num, meta) in zip(slots, ordered):
        fields[num_key] = check_num
        fields[date_key] = meta["date"]
        fields[amount_key] = format_money(meta["amount"])
    return fields


def _add_payment_to_checks(checks: dict[str, dict], payment: PaymentLine) -> None:
    check_num = (payment.check_eft_num or "").strip()
    if not check_num:
        return
    entry = checks.get(check_num)
    if entry is None:
        checks[check_num] = {
            "date": (payment.eob_date or "").strip(),
            "amount": payment.paid_amount,
        }
        return
    entry["amount"] += payment.paid_amount
    eob = (payment.eob_date or "").strip()
    if eob:
        cur = parse_date(entry["date"])
        new = parse_date(eob)
        if new is not None and (cur is None or new < cur):
            entry["date"] = eob


def _attach_orphan_to_bucket(bucket: dict, payment: PaymentLine) -> None:
    bucket["unmatched_paid"] += payment.paid_amount
    cpt = (payment.cpt_code or "").strip() or "?"
    bucket["unmatched_cpt_parts"].append(f"{cpt}={format_money(payment.paid_amount)}")
    _add_payment_to_checks(bucket["checks"], payment)


def _attach_bonus_to_bucket(bucket: dict, payment: PaymentLine) -> None:
    bucket["bonus_paid"] += payment.paid_amount
    _add_payment_to_checks(bucket["checks"], payment)


def _pick_bonus_visit(
    candidates: list[dict],
    payment: PaymentLine,
) -> dict | None:
    """Choose one visit for a patient-level bonus payment.

    Bonuses are check-scoped: only attach to a visit that already has matched
    payments from the same source file or check. If multiple such visits exist,
    use the earliest DOS.
    """
    if not candidates:
        return None

    source_file = (payment.source_file or "").strip()
    check_num = (payment.check_eft_num or "").strip()
    same_scope = [
        bucket
        for bucket in candidates
        if (source_file and source_file in bucket.get("source_files", set()))
        or (check_num and check_num in bucket.get("check_nums", set()))
    ]
    if not same_scope:
        return None
    if len(same_scope) == 1:
        return same_scope[0]
    return min(same_scope, key=lambda bucket: bucket["date_of_service"])


def _visit_bucket_key(webpt: WebptLine) -> tuple[str, str, str, str]:
    """Case-centric visit identity: facility + case + patient + DOS.

    Empty case_id/facility_id still partitions (legacy rows collapse only when
    both case and facility are blank for the same patient+DOS).
    """
    return (
        (webpt.facility_id or "").strip(),
        (webpt.case_id or "").strip(),
        webpt.patient_id,
        webpt.date_of_service,
    )


def aggregate_visits(
    matched_lines: list[MatchedLine],
    orphan_payments: list[PaymentLine] | None = None,
    deposit_dates: dict[str, str] | None = None,
) -> list[dict]:
    buckets: dict[tuple[str, str, str, str], dict] = {}
    by_revflow_patient: dict[str, list[dict]] = defaultdict(list)
    by_name_dos: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for item in matched_lines:
        webpt = item.webpt
        key = _visit_bucket_key(webpt)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "facility_id": (webpt.facility_id or "").strip(),
                "case_id": (webpt.case_id or "").strip(),
                "webpt_patient_id": webpt.patient_id,
                "patient_name": webpt.patient_name,
                "dob": webpt.dob,
                "facility_name": webpt.facility_name,
                "date_of_service": webpt.date_of_service,
                "name_key": webpt.name_key,
                "total_billed_cpts": 0,
                "matched_paid": 0.0,
                "unmatched_paid": 0.0,
                "bonus_paid": 0.0,
                "unmatched_cpt_parts": [],
                "paid_lines": 0,
                "pending_lines": 0,
                "checks": {},
                "source_files": set(),
                "check_nums": set(),
            }
            buckets[key] = bucket
            by_name_dos[(webpt.name_key, webpt.date_of_service)].append(bucket)

        bucket["total_billed_cpts"] += 1
        if item.payment is not None:
            payment = item.payment
            bucket["matched_paid"] += payment.paid_amount
            if payment.paid_amount > 0:
                bucket["paid_lines"] += 1
            _add_payment_to_checks(bucket["checks"], payment)
            revflow_id = (payment.revflow_patient_id or "").strip()
            if revflow_id:
                visits = by_revflow_patient[revflow_id]
                if bucket not in visits:
                    visits.append(bucket)
            source_file = (payment.source_file or "").strip()
            if source_file:
                bucket["source_files"].add(source_file)
            check_num = (payment.check_eft_num or "").strip()
            if check_num:
                bucket["check_nums"].add(check_num)
        if item.status == "pending":
            bucket["pending_lines"] += 1

    for payment in orphan_payments or []:
        if is_bonus_payment(payment):
            revflow_id = (payment.revflow_patient_id or "").strip()
            if not revflow_id:
                continue
            bucket = _pick_bonus_visit(by_revflow_patient.get(revflow_id, []), payment)
            if bucket is None:
                continue
            _attach_bonus_to_bucket(bucket, payment)
            continue

        dos = _normalize_dos(payment.date_of_service)
        if not dos or not payment.name_key:
            continue

        revflow_id = (payment.revflow_patient_id or "").strip()
        bucket = None
        if revflow_id:
            # Always require DOS agreement — never dump other-day orphans onto
            # the only matched visit for this RevFlow patient.
            dos_matches = [
                candidate
                for candidate in by_revflow_patient.get(revflow_id, [])
                if _normalize_dos(candidate["date_of_service"]) == dos
            ]
            if len(dos_matches) == 1:
                bucket = dos_matches[0]
        if bucket is None:
            candidates = by_name_dos.get((payment.name_key, dos), [])
            if len(candidates) == 1:
                bucket = candidates[0]
            else:
                # Ambiguous same-name/same-day collision, or no same-DOS visit.
                continue
        _attach_orphan_to_bucket(bucket, payment)

    rows: list[dict] = []
    for bucket in buckets.values():
        if bucket["pending_lines"] == 0 and bucket["paid_lines"] > 0:
            visit_status = "paid"
        elif bucket["paid_lines"] > 0:
            visit_status = "partial"
        else:
            visit_status = "pending"
        checks = bucket.pop("checks")
        apply_deposit_dates(checks, deposit_dates)
        matched_paid = float(bucket.pop("matched_paid"))
        unmatched_paid = float(bucket.pop("unmatched_paid"))
        bonus_paid = float(bucket.pop("bonus_paid"))
        unmatched_parts: list[str] = bucket.pop("unmatched_cpt_parts")
        bucket.pop("name_key", None)
        bucket.pop("source_files", None)
        bucket.pop("check_nums", None)
        bucket["total_paid"] = format_money(matched_paid + bonus_paid)
        bucket["matched_paid"] = format_money(matched_paid)
        bucket["bonus_paid"] = format_money(bonus_paid)
        bucket["unmatched_paid"] = format_money(unmatched_paid)
        bucket["visit_paid_total"] = format_money(
            matched_paid + unmatched_paid + bonus_paid
        )
        bucket["unmatched_cpts"] = "; ".join(unmatched_parts)
        bucket["visit_status"] = visit_status
        bucket.update(_check_fields_from_rollup(checks))
        rows.append(bucket)
    rows.sort(
        key=lambda row: (
            row.get("facility_id", ""),
            row.get("case_id", ""),
            row["webpt_patient_id"],
            row["date_of_service"],
        )
    )
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
