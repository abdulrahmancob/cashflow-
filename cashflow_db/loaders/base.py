"""Shared ETL helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg
import yaml

from cashflow_db.config import RULES_YAML
from cashflow_db.util import (
    normalize_name_key,
    parse_ampm_on_date,
    parse_datetime,
    parse_money,
    safe_str,
)

# WebPT scheduler status → core.visit / schedule_appointment.status
VISIT_STATUS_MAP = {
    "checked out": "completed",
    "checked in": "unchecked_out",
    "cancelled/no show": "no_show",
    "cancelled": "cancelled",
    "no show": "no_show",
    "1": "scheduled",
    "2": "scheduled",
    "other": "scheduled",
}

PAYMENT_CATEGORIES = frozenset(
    {"Copay", "Other", "Wellness", "Deductible", "Supplies", "Internal Payment"}
)


@dataclass
class AppointmentCandidate:
    """One scheduler row competing for clinical visit selection."""

    appointment_at: datetime
    service_date: date
    status_raw: str = ""
    status: str = "scheduled"
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    has_daily_note: bool = False
    has_cpt: bool = False
    webpt_appointment_id: str | None = None
    insurance_name_raw: str | None = None
    facility_webpt_id: str | None = None
    facility_name: str | None = None
    patient_webpt_id: str | None = None
    case_webpt_id: str | None = None
    patient_name: str | None = None
    copay: Any = None
    deductible: Any = None
    source_row: dict[str, Any] = field(default_factory=dict)

    @property
    def chair_seconds(self) -> float:
        if self.check_in_at and self.check_out_at:
            return max(0.0, (self.check_out_at - self.check_in_at).total_seconds())
        return -1.0


def map_visit_status(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return "scheduled"
    return VISIT_STATUS_MAP.get(text, "scheduled")


def appointment_from_schedule_row(row: dict[str, Any]) -> AppointmentCandidate | None:
    """Build a candidate from a schedule_visits CSV row. Returns None if incomplete."""
    case_id = safe_str(row.get("case_id"))
    patient_id = safe_str(row.get("patient_id"))
    facility_id = safe_str(row.get("facility_id"))
    appt_at = parse_datetime(row.get("appointment_at"))
    if not case_id or not patient_id or not facility_id or not appt_at:
        return None
    service_date = appt_at.date()
    status_raw = safe_str(row.get("visit_status")) or ""
    return AppointmentCandidate(
        appointment_at=appt_at,
        service_date=service_date,
        status_raw=status_raw,
        status=map_visit_status(status_raw),
        check_in_at=parse_ampm_on_date(service_date, row.get("checkin_time")),
        check_out_at=parse_ampm_on_date(service_date, row.get("checkout_time")),
        webpt_appointment_id=safe_str(row.get("appointment_id")),
        insurance_name_raw=safe_str(row.get("ins_name")),
        facility_webpt_id=facility_id,
        facility_name=safe_str(row.get("facility_name")),
        patient_webpt_id=patient_id,
        case_webpt_id=case_id,
        patient_name=safe_str(row.get("patient_name")),
        copay=parse_copay_deductible(row.get("copay")),
        deductible=parse_copay_deductible(row.get("deductible")),
        source_row=dict(row),
    )


def select_clinical_appointment(
    candidates: list[AppointmentCandidate],
) -> AppointmentCandidate | None:
    """Documentation-first clinical winner among same case+DOS appointments.

    Order: Checked Out → has daily note → has CPT → longest chair time → earliest.
    """
    if not candidates:
        return None

    def sort_key(c: AppointmentCandidate) -> tuple:
        is_checked_out = 1 if c.status == "completed" else 0
        return (
            is_checked_out,
            1 if c.has_daily_note else 0,
            1 if c.has_cpt else 0,
            c.chair_seconds,
            # earliest appointment_at wins ties → invert via negative timestamp
            -c.appointment_at.timestamp(),
        )

    return max(candidates, key=sort_key)


def normalize_payment_category(payment_type: str | None) -> str | None:
    text = safe_str(payment_type)
    if not text:
        return None
    if text in PAYMENT_CATEGORIES:
        return text
    # Case-insensitive match
    for cat in PAYMENT_CATEGORIES:
        if cat.lower() == text.lower():
            return cat
    return None


def load_business_rules() -> dict[str, Any]:
    return yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))


def upsert_facility(
    conn: psycopg.Connection,
    *,
    webpt_facility_id: str | None,
    name: str | None,
) -> str | None:
    name = safe_str(name)
    webpt_facility_id = safe_str(webpt_facility_id)
    if not name and not webpt_facility_id:
        return None
    if webpt_facility_id:
        row = conn.execute(
            """
            INSERT INTO ref.facility (webpt_facility_id, name)
            VALUES (%s, COALESCE(%s, %s))
            ON CONFLICT (webpt_facility_id) DO UPDATE
            SET name = COALESCE(EXCLUDED.name, ref.facility.name)
            RETURNING facility_id
            """,
            (webpt_facility_id, name, webpt_facility_id),
        ).fetchone()
        return str(row["facility_id"])
    row = conn.execute(
        """
        SELECT facility_id FROM ref.facility WHERE name = %s LIMIT 1
        """,
        (name,),
    ).fetchone()
    if row:
        return str(row["facility_id"])
    row = conn.execute(
        """
        INSERT INTO ref.facility (name) VALUES (%s) RETURNING facility_id
        """,
        (name,),
    ).fetchone()
    return str(row["facility_id"])


def upsert_patient(
    conn: psycopg.Connection,
    *,
    webpt_patient_id: str | None,
    patient_name: str | None = None,
    revflow_patient_id: str | None = None,
    etl_run_id: str | None = None,
    source_system: str = "webpt",
) -> str | None:
    webpt_patient_id = safe_str(webpt_patient_id)
    revflow_patient_id = safe_str(revflow_patient_id)
    if not webpt_patient_id and not revflow_patient_id:
        return None
    name_key = normalize_name_key(patient_name) if patient_name else None
    if webpt_patient_id:
        row = conn.execute(
            """
            INSERT INTO core.patient (
                webpt_patient_id, revflow_patient_id, name_key,
                source_system, source_natural_key, etl_run_id
            )
            VALUES (%s, %s, %s, %s, %s, %s::uuid)
            ON CONFLICT (webpt_patient_id) DO UPDATE
            SET revflow_patient_id = COALESCE(EXCLUDED.revflow_patient_id, core.patient.revflow_patient_id),
                name_key = COALESCE(EXCLUDED.name_key, core.patient.name_key),
                etl_run_id = EXCLUDED.etl_run_id
            RETURNING patient_id
            """,
            (
                webpt_patient_id,
                revflow_patient_id,
                name_key,
                source_system,
                webpt_patient_id,
                etl_run_id,
            ),
        ).fetchone()
        return str(row["patient_id"])
    row = conn.execute(
        """
        SELECT patient_id FROM core.patient
        WHERE revflow_patient_id = %s
        LIMIT 1
        """,
        (revflow_patient_id,),
    ).fetchone()
    if row:
        return str(row["patient_id"])
    row = conn.execute(
        """
        INSERT INTO core.patient (
            revflow_patient_id, name_key, source_system, source_natural_key, etl_run_id
        )
        VALUES (%s, %s, %s, %s, %s::uuid)
        RETURNING patient_id
        """,
        (revflow_patient_id, name_key, source_system, revflow_patient_id, etl_run_id),
    ).fetchone()
    return str(row["patient_id"])


def ensure_patient_history(
    conn: psycopg.Connection,
    patient_id: str,
    *,
    patient_name: str | None = None,
    dob=None,
    mobile_phone: str | None = None,
    home_phone: str | None = None,
    work_phone: str | None = None,
    email: str | None = None,
    best_phone: str | None = None,
) -> None:
    current = conn.execute(
        """
        SELECT patient_history_id, patient_name, dob::text, mobile_phone, home_phone,
               work_phone, email, best_phone
        FROM core.patient_history
        WHERE patient_id = %s::uuid AND is_current
        """,
        (patient_id,),
    ).fetchone()
    snapshot = {
        "patient_name": safe_str(patient_name),
        "dob": str(dob) if dob else None,
        "mobile_phone": safe_str(mobile_phone),
        "home_phone": safe_str(home_phone),
        "work_phone": safe_str(work_phone),
        "email": safe_str(email),
        "best_phone": safe_str(best_phone),
    }
    if current:
        same = all(
            (current.get(k) or None) == (snapshot.get(k) or None)
            for k in snapshot
        )
        if same:
            return
        conn.execute(
            """
            UPDATE core.patient_history
            SET is_current = false, valid_to = now()
            WHERE patient_history_id = %s
            """,
            (current["patient_history_id"],),
        )
    conn.execute(
        """
        INSERT INTO core.patient_history (
            patient_id, patient_name, dob, mobile_phone, home_phone,
            work_phone, email, best_phone, is_current
        )
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, true)
        """,
        (
            patient_id,
            snapshot["patient_name"],
            dob,
            snapshot["mobile_phone"],
            snapshot["home_phone"],
            snapshot["work_phone"],
            snapshot["email"],
            snapshot["best_phone"],
        ),
    )


def upsert_case(
    conn: psycopg.Connection,
    *,
    webpt_case_id: str | None,
    patient_id: str,
    facility_id: str | None = None,
    assigned_therapist: str | None = None,
    diagnosis_raw: str | None = None,
    case_label: str | None = None,
    etl_run_id: str | None = None,
) -> str | None:
    webpt_case_id = safe_str(webpt_case_id)
    if not webpt_case_id:
        return None
    row = conn.execute(
        """
        INSERT INTO core.patient_case (
            webpt_case_id, patient_id, facility_id, assigned_therapist,
            diagnosis_raw, case_label, source_system, source_natural_key, etl_run_id
        )
        VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, 'webpt', %s, %s::uuid)
        ON CONFLICT (webpt_case_id) DO UPDATE
        SET facility_id = COALESCE(EXCLUDED.facility_id, core.patient_case.facility_id),
            assigned_therapist = COALESCE(EXCLUDED.assigned_therapist, core.patient_case.assigned_therapist),
            diagnosis_raw = COALESCE(EXCLUDED.diagnosis_raw, core.patient_case.diagnosis_raw),
            case_label = COALESCE(EXCLUDED.case_label, core.patient_case.case_label),
            etl_run_id = EXCLUDED.etl_run_id
        RETURNING case_pk
        """,
        (
            webpt_case_id,
            patient_id,
            facility_id,
            safe_str(assigned_therapist),
            safe_str(diagnosis_raw),
            safe_str(case_label),
            webpt_case_id,
            etl_run_id,
        ),
    ).fetchone()
    return str(row["case_pk"])


def ensure_cpt(conn: psycopg.Connection, cpt_code: str | None) -> str | None:
    cpt_code = safe_str(cpt_code)
    if not cpt_code:
        return None
    conn.execute(
        """
        INSERT INTO ref.cpt_code (cpt_code)
        VALUES (%s)
        ON CONFLICT (cpt_code) DO NOTHING
        """,
        (cpt_code,),
    )
    return cpt_code


def classify_auth_kind(auth_number: str | None, rules: dict[str, Any] | None = None) -> str:
    rules = rules or load_business_rules()
    dummy_vals = {
        str(v).strip().lower()
        for v in rules.get("auth", {}).get("dummy_auth_number_values", ["0"])
    }
    if auth_number is None or str(auth_number).strip().lower() in dummy_vals:
        return "dummy"
    return "hard"


def parse_copay_deductible(raw: str | None):
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in {"no", "n", "none", "-"}:
        return None
    return parse_money(raw.replace("$", ""))


def upsert_coverage(
    conn: psycopg.Connection,
    *,
    patient_id: str,
    raw_insurance_name: str | None,
    case_pk: str | None = None,
    deductible=None,
    copay=None,
    limit_per_year: int | None = None,
    referral_required: bool | None = None,
    etl_run_id: str | None = None,
) -> str | None:
    raw_insurance_name = safe_str(raw_insurance_name)
    if not raw_insurance_name:
        return None
    existing = conn.execute(
        """
        SELECT coverage_id FROM core.patient_coverage
        WHERE patient_id = %s::uuid
          AND raw_insurance_name = %s
          AND (
                (%s::uuid IS NULL AND case_pk IS NULL)
             OR case_pk IS NOT DISTINCT FROM %s::uuid
          )
        ORDER BY is_primary DESC, effective_from DESC NULLS LAST
        LIMIT 1
        """,
        (patient_id, raw_insurance_name, case_pk, case_pk),
    ).fetchone()
    if existing:
        return str(existing["coverage_id"])
    row = conn.execute(
        """
        INSERT INTO core.patient_coverage (
            patient_id, case_pk, raw_insurance_name, deductible, copay,
            limit_per_year, referral_required, is_primary,
            source_system, source_natural_key, etl_run_id
        )
        VALUES (
            %s::uuid, %s::uuid, %s, %s, %s, %s, %s, true,
            'webpt', %s, %s::uuid
        )
        RETURNING coverage_id
        """,
        (
            patient_id,
            case_pk,
            raw_insurance_name,
            deductible,
            copay,
            limit_per_year,
            referral_required,
            f"{patient_id}:{case_pk or ''}:{raw_insurance_name}",
            etl_run_id,
        ),
    ).fetchone()
    return str(row["coverage_id"])


def upsert_clinical_visit(
    conn: psycopg.Connection,
    *,
    case_pk: str,
    patient_id: str,
    facility_id: str | None,
    service_date: date,
    appointment_at: datetime | None,
    status: str,
    check_in_at: datetime | None = None,
    check_out_at: datetime | None = None,
    coverage_id: str | None = None,
    insurance_name_raw: str | None = None,
    webpt_appointment_id: str | None = None,
    etl_run_id: str | None = None,
) -> str:
    """Upsert the single clinical visit for (case_pk, service_date)."""
    natural = f"{case_pk}:{service_date.isoformat()}"
    existing = conn.execute(
        """
        SELECT visit_id FROM core.visit
        WHERE case_pk = %s::uuid AND service_date = %s
        """,
        (case_pk, service_date),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE core.visit SET
                facility_id = COALESCE(%s::uuid, facility_id),
                appointment_at = %s,
                status = %s,
                check_in_at = %s,
                check_out_at = %s,
                coverage_id = COALESCE(%s::uuid, coverage_id),
                insurance_name_raw = COALESCE(%s, insurance_name_raw),
                webpt_appointment_id = COALESCE(%s, webpt_appointment_id),
                source_natural_key = %s,
                etl_run_id = %s::uuid
            WHERE visit_id = %s::uuid
            """,
            (
                facility_id,
                appointment_at,
                status,
                check_in_at,
                check_out_at,
                coverage_id,
                safe_str(insurance_name_raw),
                safe_str(webpt_appointment_id),
                natural,
                etl_run_id,
                str(existing["visit_id"]),
            ),
        )
        return str(existing["visit_id"])
    row = conn.execute(
        """
        INSERT INTO core.visit (
            case_pk, patient_id, facility_id, service_date, appointment_at,
            status, check_in_at, check_out_at, coverage_id, insurance_name_raw,
            webpt_appointment_id, source_system, source_natural_key, etl_run_id
        )
        VALUES (
            %s::uuid, %s::uuid, %s::uuid, %s, %s,
            %s, %s, %s, %s::uuid, %s,
            %s, 'webpt', %s, %s::uuid
        )
        RETURNING visit_id
        """,
        (
            case_pk,
            patient_id,
            facility_id,
            service_date,
            appointment_at,
            status,
            check_in_at,
            check_out_at,
            coverage_id,
            safe_str(insurance_name_raw),
            safe_str(webpt_appointment_id),
            natural,
            etl_run_id,
        ),
    ).fetchone()
    return str(row["visit_id"])


def upsert_schedule_appointment(
    conn: psycopg.Connection,
    *,
    case_pk: str,
    patient_id: str,
    facility_id: str | None,
    visit_id: str | None,
    candidate: AppointmentCandidate,
    is_selected_clinical: bool,
    etl_run_id: str | None = None,
) -> str:
    natural = f"{case_pk}:{candidate.appointment_at.isoformat(sep=' ')}"
    row = conn.execute(
        """
        INSERT INTO core.schedule_appointment (
            case_pk, patient_id, facility_id, visit_id, service_date,
            appointment_at, webpt_appointment_id, visit_status_raw, status,
            check_in_at, check_out_at, insurance_name_raw, copay, deductible,
            is_selected_clinical,
            source_system, source_natural_key, etl_run_id, updated_at
        )
        VALUES (
            %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            'webpt', %s, %s::uuid, now()
        )
        ON CONFLICT (case_pk, appointment_at) DO UPDATE SET
            facility_id = COALESCE(EXCLUDED.facility_id, core.schedule_appointment.facility_id),
            visit_id = EXCLUDED.visit_id,
            visit_status_raw = EXCLUDED.visit_status_raw,
            status = EXCLUDED.status,
            check_in_at = EXCLUDED.check_in_at,
            check_out_at = EXCLUDED.check_out_at,
            insurance_name_raw = COALESCE(
                EXCLUDED.insurance_name_raw, core.schedule_appointment.insurance_name_raw
            ),
            copay = COALESCE(EXCLUDED.copay, core.schedule_appointment.copay),
            deductible = COALESCE(EXCLUDED.deductible, core.schedule_appointment.deductible),
            is_selected_clinical = EXCLUDED.is_selected_clinical,
            webpt_appointment_id = COALESCE(
                EXCLUDED.webpt_appointment_id, core.schedule_appointment.webpt_appointment_id
            ),
            etl_run_id = EXCLUDED.etl_run_id,
            updated_at = now()
        RETURNING schedule_appointment_id
        """,
        (
            case_pk,
            patient_id,
            facility_id,
            visit_id,
            candidate.service_date,
            candidate.appointment_at,
            candidate.webpt_appointment_id,
            candidate.status_raw,
            candidate.status,
            candidate.check_in_at,
            candidate.check_out_at,
            candidate.insurance_name_raw,
            candidate.copay,
            candidate.deductible,
            is_selected_clinical,
            natural,
            etl_run_id,
        ),
    ).fetchone()
    return str(row["schedule_appointment_id"])
