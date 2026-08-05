"""WebPT patients / case-aware extracts / audit / edocs → core + docs + audit."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from cashflow_db.config import CASE_PIPELINE_DIR, WEBPT_LEGACY_OUTPUT, WEBPT_OUTPUT
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.loaders.base import (
    classify_auth_kind,
    ensure_cpt,
    ensure_patient_history,
    load_business_rules,
    parse_copay_deductible,
    upsert_case,
    upsert_coverage,
    upsert_facility,
    upsert_patient,
)
from cashflow_db.util import parse_date, parse_int, safe_str


def _pick_patients_csv(root: Path) -> Path:
    for name in (
        "patients_export_jan_aug_2026.csv",
        "patients_recent_273d.csv",
        "patients_export_273d.csv",
        "patients_export_10d.csv",
        "patients_export.csv",
    ):
        path = root / name
        if path.exists():
            return path
    # Fallback to legacy window
    for name in (
        "patients_recent_273d.csv",
        "patients_export_273d.csv",
        "patients_export_10d.csv",
        "patients_export.csv",
    ):
        path = WEBPT_LEGACY_OUTPUT / name
        if path.exists():
            return path
    raise FileNotFoundError(f"No patients CSV under {root} or {WEBPT_LEGACY_OUTPUT}")


def _case_aware_extract(name: str) -> Path | None:
    """Prefer case-pipeline extracts that include facility_id + case_id."""
    for base in (CASE_PIPELINE_DIR / "extracted", CASE_PIPELINE_DIR / "batch_extracted"):
        path = base / name
        if path.exists():
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                reader = csv.DictReader(fh)
                cols = set(reader.fieldnames or [])
            if "case_id" in cols and "facility_id" in cols:
                return path
    return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh))


def _resolve_visit_for_note(
    conn,
    *,
    webpt_case_id: str,
    patient_id: str,
    service_date,
) -> str | None:
    """Attach note only to clinical visit for this case+DOS — never guess case."""
    case_row = conn.execute(
        "SELECT case_pk FROM core.patient_case WHERE webpt_case_id = %s",
        (webpt_case_id,),
    ).fetchone()
    if not case_row:
        return None
    case_pk = str(case_row["case_pk"])
    visit = conn.execute(
        """
        SELECT visit_id FROM core.visit
        WHERE case_pk = %s::uuid AND service_date = %s
        """,
        (case_pk, service_date),
    ).fetchone()
    if visit:
        return str(visit["visit_id"])
    # Create minimal clinical visit if schedule not loaded yet
    from cashflow_db.loaders.base import upsert_clinical_visit

    return upsert_clinical_visit(
        conn,
        case_pk=case_pk,
        patient_id=patient_id,
        facility_id=None,
        service_date=service_date,
        appointment_at=None,
        status="completed",
        etl_run_id=None,
    )


def load_webpt(
    *,
    root: Path | None = None,
    database_url: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    root = root or WEBPT_OUTPUT
    legacy = WEBPT_LEGACY_OUTPUT
    rules = load_business_rules()
    sla_hours = int(rules.get("visit", {}).get("note_sla_hours", 24))
    counts = {
        "patients": 0,
        "cases": 0,
        "notes": 0,
        "service_lines": 0,
        "audit_findings": 0,
        "documents": 0,
        "authorizations": 0,
        "notes_skipped_no_case": 0,
        "plans_of_care": 0,
        "denial_letters": 0,
    }

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "webpt", str(root))
        try:
            doc_types = {
                r["code"]: str(r["document_type_id"])
                for r in conn.execute("SELECT document_type_id, code FROM ref.document_type")
            }

            patients_path = _pick_patients_csv(root)
            patients = _read_csv(patients_path)
            if limit:
                patients = patients[:limit]

            for row in patients:
                if not safe_str(row.get("case_id")):
                    continue
                pid = upsert_patient(
                    conn,
                    webpt_patient_id=row.get("patient_id"),
                    patient_name=row.get("patient_name"),
                    etl_run_id=etl_id,
                )
                if not pid:
                    continue
                counts["patients"] += 1
                ensure_patient_history(
                    conn,
                    pid,
                    patient_name=row.get("patient_name"),
                    dob=parse_date(row.get("dob")),
                )
                facility_id = upsert_facility(
                    conn,
                    webpt_facility_id=row.get("facility_id"),
                    name=row.get("facility_name"),
                )
                case_pk = upsert_case(
                    conn,
                    webpt_case_id=row.get("case_id"),
                    patient_id=pid,
                    facility_id=facility_id,
                    assigned_therapist=row.get("assigned_therapist"),
                    diagnosis_raw=row.get("diagnosis"),
                    case_label=row.get("case_label") or row.get("case_name"),
                    etl_run_id=etl_id,
                )
                if case_pk:
                    counts["cases"] += 1

                ins_name = safe_str(row.get("ins_name"))
                coverage_id = None
                if ins_name and case_pk:
                    coverage_id = upsert_coverage(
                        conn,
                        patient_id=pid,
                        case_pk=case_pk,
                        raw_insurance_name=ins_name,
                        deductible=parse_copay_deductible(row.get("deductible")),
                        copay=parse_copay_deductible(row.get("copay")),
                        limit_per_year=parse_int(row.get("limit_per_year")),
                        referral_required=(
                            True
                            if str(row.get("referral_required") or "").lower()
                            in {"yes", "y", "true", "1"}
                            else (
                                False
                                if str(row.get("referral_required") or "").lower()
                                in {"no", "n", "false", "0"}
                                else None
                            )
                        ),
                        etl_run_id=etl_id,
                    )

                auth_raw = safe_str(row.get("auth_ins_visits"))
                if case_pk and (auth_raw is not None or coverage_id):
                    visits_auth = parse_int(auth_raw)
                    kind = classify_auth_kind(auth_raw, rules)
                    conn.execute(
                        """
                        INSERT INTO core.authorization (
                            case_pk, coverage_id, auth_kind, auth_number,
                            visits_authorized, status, source_system,
                            source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, %s, %s, 'approved',
                            'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            visits_authorized = EXCLUDED.visits_authorized,
                            auth_kind = EXCLUDED.auth_kind,
                            coverage_id = COALESCE(EXCLUDED.coverage_id, core.authorization.coverage_id),
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            case_pk,
                            coverage_id,
                            kind,
                            auth_raw,
                            visits_auth,
                            f"{row.get('case_id')}:auth",
                            etl_id,
                        ),
                    )
                    counts["authorizations"] += 1

            # Case-aware daily notes only
            notes_path = _case_aware_extract("daily_notes.csv")
            if notes_path:
                notes = _read_csv(notes_path)
                if limit:
                    notes = notes[: limit * 3]
                for row in notes:
                    webpt_pid = safe_str(row.get("patient_id"))
                    webpt_case = safe_str(row.get("case_id"))
                    daily_note_id = safe_str(row.get("daily_note_id"))
                    if not webpt_pid or not daily_note_id:
                        continue
                    if not webpt_case:
                        counts["notes_skipped_no_case"] += 1
                        continue
                    pid = upsert_patient(
                        conn,
                        webpt_patient_id=webpt_pid,
                        patient_name=row.get("patient_name"),
                        etl_run_id=etl_id,
                    )
                    if not pid:
                        continue
                    ensure_patient_history(
                        conn,
                        pid,
                        patient_name=row.get("patient_name"),
                        dob=parse_date(row.get("date_of_birth")),
                    )
                    facility_id = upsert_facility(
                        conn,
                        webpt_facility_id=row.get("facility_id"),
                        name=row.get("facility_name"),
                    )
                    upsert_case(
                        conn,
                        webpt_case_id=webpt_case,
                        patient_id=pid,
                        facility_id=facility_id,
                        etl_run_id=etl_id,
                    )
                    service_date = parse_date(row.get("date_of_daily_note"))
                    if not service_date:
                        continue
                    visit_no = parse_int(row.get("visit_no"))
                    visit_type = "follow_up"
                    if visit_no == 1:
                        visit_type = "initial"
                    elif visit_no and visit_no % 10 == 0:
                        visit_type = "re_examination"

                    visit_id = _resolve_visit_for_note(
                        conn,
                        webpt_case_id=webpt_case,
                        patient_id=pid,
                        service_date=service_date,
                    )
                    if not visit_id:
                        counts["notes_skipped_no_case"] += 1
                        continue
                    conn.execute(
                        """
                        UPDATE core.visit
                        SET visit_type = COALESCE(%s, visit_type),
                            visit_no = COALESCE(%s, visit_no),
                            facility_id = COALESCE(%s::uuid, facility_id),
                            etl_run_id = %s::uuid
                        WHERE visit_id = %s::uuid
                        """,
                        (visit_type, visit_no, facility_id, etl_id, visit_id),
                    )

                    signed_at = None
                    if service_date:
                        signed_at = datetime(
                            service_date.year,
                            service_date.month,
                            service_date.day,
                            23,
                            59,
                            tzinfo=timezone.utc,
                        )
                    conn.execute(
                        """
                        INSERT INTO core.clinical_note (
                            visit_id, external_daily_note_id, note_kind, note_date,
                            note_file, referring_physician, diagnosis_raw,
                            diagnosis_icd_codes, treatment_diagnosis_icd_codes,
                            insurance_name_raw, extraction_method, signed_at,
                            sla_hours_target, sla_breached, error,
                            source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s, 'daily', %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, 'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (external_daily_note_id) DO UPDATE
                        SET diagnosis_icd_codes = EXCLUDED.diagnosis_icd_codes,
                            extraction_method = EXCLUDED.extraction_method,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            visit_id,
                            daily_note_id,
                            service_date,
                            safe_str(row.get("note_file")),
                            safe_str(row.get("referring_physician")),
                            safe_str(row.get("diagnosis_raw")),
                            safe_str(row.get("diagnosis_icd_codes")),
                            safe_str(row.get("treatment_diagnosis_icd_codes")),
                            safe_str(row.get("insurance_name")),
                            safe_str(row.get("extraction_method")),
                            signed_at,
                            sla_hours,
                            False,
                            safe_str(row.get("error")),
                            daily_note_id,
                            etl_id,
                        ),
                    )
                    counts["notes"] += 1

                    # Mark documentation flags on appointments for this case+DOS
                    case_row = conn.execute(
                        "SELECT case_pk FROM core.patient_case WHERE webpt_case_id = %s",
                        (webpt_case,),
                    ).fetchone()
                    if case_row:
                        # Re-select clinical winner with has_daily_note preference
                        from cashflow_db.loaders.base import (
                            AppointmentCandidate,
                            map_visit_status,
                            select_clinical_appointment,
                            upsert_clinical_visit,
                        )

                        appts = conn.execute(
                            """
                            SELECT schedule_appointment_id, appointment_at, service_date,
                                   visit_status_raw, status, check_in_at, check_out_at,
                                   insurance_name_raw, webpt_appointment_id
                            FROM core.schedule_appointment
                            WHERE case_pk = %s::uuid AND service_date = %s
                            """,
                            (str(case_row["case_pk"]), service_date),
                        ).fetchall()
                        if appts:
                            cands = []
                            for a in appts:
                                cands.append(
                                    AppointmentCandidate(
                                        appointment_at=a["appointment_at"].replace(tzinfo=None)
                                        if hasattr(a["appointment_at"], "tzinfo")
                                        and a["appointment_at"].tzinfo
                                        else a["appointment_at"],
                                        service_date=a["service_date"],
                                        status_raw=a["visit_status_raw"] or "",
                                        status=a["status"]
                                        or map_visit_status(a["visit_status_raw"]),
                                        check_in_at=a["check_in_at"],
                                        check_out_at=a["check_out_at"],
                                        has_daily_note=True,
                                        has_cpt=False,
                                        webpt_appointment_id=a["webpt_appointment_id"],
                                        insurance_name_raw=a["insurance_name_raw"],
                                    )
                                )
                            winner = select_clinical_appointment(cands)
                            if winner:
                                visit_id = upsert_clinical_visit(
                                    conn,
                                    case_pk=str(case_row["case_pk"]),
                                    patient_id=pid,
                                    facility_id=facility_id,
                                    service_date=service_date,
                                    appointment_at=winner.appointment_at,
                                    status=winner.status,
                                    check_in_at=winner.check_in_at,
                                    check_out_at=winner.check_out_at,
                                    insurance_name_raw=winner.insurance_name_raw,
                                    webpt_appointment_id=winner.webpt_appointment_id,
                                    etl_run_id=etl_id,
                                )
                                conn.execute(
                                    """
                                    UPDATE core.schedule_appointment
                                    SET is_selected_clinical = (appointment_at = %s),
                                        visit_id = %s::uuid
                                    WHERE case_pk = %s::uuid AND service_date = %s
                                    """,
                                    (
                                        winner.appointment_at,
                                        visit_id,
                                        str(case_row["case_pk"]),
                                        service_date,
                                    ),
                                )

            cpt_path = _case_aware_extract("cpt_codes.csv")
            if cpt_path:
                note_map = {
                    r["external_daily_note_id"]: str(r["visit_id"])
                    for r in conn.execute(
                        """
                        SELECT external_daily_note_id, visit_id
                        FROM core.clinical_note
                        WHERE external_daily_note_id IS NOT NULL
                        """
                    )
                }
                cpt_rows = _read_csv(cpt_path)
                if limit:
                    cpt_rows = cpt_rows[: limit * 10]
                for row in cpt_rows:
                    if not safe_str(row.get("case_id")):
                        counts["notes_skipped_no_case"] += 1
                        continue
                    daily_note_id = safe_str(row.get("daily_note_id"))
                    visit_id = note_map.get(daily_note_id or "")
                    if not visit_id:
                        continue
                    cpt = ensure_cpt(conn, row.get("cpt_code"))
                    if not cpt:
                        continue
                    conn.execute(
                        """
                        INSERT INTO core.visit_service_line (
                            visit_id, cpt_code, modifiers, units, description,
                            billing_modifier_suffix, source_system,
                            source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s, %s, %s, %s, %s, 'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            units = EXCLUDED.units,
                            modifiers = EXCLUDED.modifiers,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            visit_id,
                            cpt,
                            safe_str(row.get("modifier")),
                            parse_int(row.get("units")),
                            safe_str(row.get("description")),
                            safe_str(row.get("billing_modifier_suffix")),
                            f"{daily_note_id}:{cpt}:{row.get('modifier')}",
                            etl_id,
                        ),
                    )
                    counts["service_lines"] += 1

            # Plans of care + denial letters (legacy extracted; schema already exists)
            extracted_root = legacy / "extracted"
            poc_path = extracted_root / "plans_of_care.csv"
            if not poc_path.exists():
                poc_path = _case_aware_extract("plans_of_care.csv")
            if poc_path is not None and poc_path.exists():
                dtype_poc = doc_types.get("poc")
                for row in _read_csv(poc_path)[: (limit or 10**9)]:
                    poc_id = safe_str(row.get("poc_id") or row.get("plan_of_care_id"))
                    if not poc_id:
                        continue
                    pid = upsert_patient(
                        conn,
                        webpt_patient_id=row.get("patient_id"),
                        patient_name=row.get("patient_name"),
                        etl_run_id=etl_id,
                    )
                    case_pk = None
                    if safe_str(row.get("case_id")) and pid:
                        case_pk = upsert_case(
                            conn,
                            webpt_case_id=row.get("case_id"),
                            patient_id=pid,
                            etl_run_id=etl_id,
                        )
                    doc = conn.execute(
                        """
                        INSERT INTO docs.document (
                            patient_id, case_pk, document_type_id, ext_doc_id,
                            source, source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, 'edoc', 'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET etl_run_id = EXCLUDED.etl_run_id
                        RETURNING document_id
                        """,
                        (pid, case_pk, dtype_poc, poc_id, f"poc:{poc_id}", etl_id),
                    ).fetchone()
                    if not doc:
                        continue
                    conn.execute(
                        """
                        INSERT INTO docs.plan_of_care_detail (
                            document_id, poc_id, date_of_plan_of_care,
                            frequency, duration, plan_text
                        )
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        ON CONFLICT (poc_id) WHERE poc_id IS NOT NULL
                        DO UPDATE SET
                            frequency = EXCLUDED.frequency,
                            duration = EXCLUDED.duration,
                            plan_text = EXCLUDED.plan_text,
                            date_of_plan_of_care = EXCLUDED.date_of_plan_of_care
                        """,
                        (
                            str(doc["document_id"]),
                            poc_id,
                            parse_date(row.get("date_of_plan_of_care") or row.get("date")),
                            safe_str(row.get("frequency")),
                            safe_str(row.get("duration")),
                            safe_str(row.get("plan") or row.get("plan_text")),
                        ),
                    )
                    counts["plans_of_care"] += 1

            denial_path = extracted_root / "denial_reasons.csv"
            if denial_path.exists():
                dtype_den = doc_types.get("denial")
                for row in _read_csv(denial_path)[: (limit or 10**9)]:
                    natural = safe_str(row.get("source_natural_key")) or (
                        f"denial:{row.get('patient_id')}:{row.get('denial_date')}:"
                        f"{row.get('reason_raw') or row.get('reason')}"
                    )
                    pid = upsert_patient(
                        conn,
                        webpt_patient_id=row.get("patient_id"),
                        patient_name=row.get("patient_name"),
                        etl_run_id=etl_id,
                    )
                    doc = conn.execute(
                        """
                        INSERT INTO docs.document (
                            patient_id, document_type_id, ext_doc_id,
                            source, source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s, 'edoc', 'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET etl_run_id = EXCLUDED.etl_run_id
                        RETURNING document_id
                        """,
                        (pid, dtype_den, natural[:120], natural, etl_id),
                    ).fetchone()
                    if not doc:
                        continue
                    # one detail row per document (re-insert safe via delete+insert)
                    conn.execute(
                        "DELETE FROM docs.denial_letter_detail WHERE document_id = %s::uuid",
                        (str(doc["document_id"]),),
                    )
                    conn.execute(
                        """
                        INSERT INTO docs.denial_letter_detail (
                            document_id, denial_date, insurance_name,
                            payer_guess, reason_raw, reason_class
                        )
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(doc["document_id"]),
                            parse_date(row.get("denial_date") or row.get("date")),
                            safe_str(row.get("insurance_name") or row.get("ins_name")),
                            safe_str(row.get("payer_guess")),
                            safe_str(row.get("reason_raw") or row.get("reason")),
                            safe_str(row.get("reason_class")),
                        ),
                    )
                    counts["denial_letters"] += 1

            # Audit + edocs from legacy jun_jul when present
            audit_root = legacy if (legacy / "audit").exists() else root
            for kind, fname in (
                ("cpt_rule", "cpt_violations.csv"),
                ("icd_rule", "icd_violations.csv"),
            ):
                path = audit_root / "audit" / fname
                if not path.exists():
                    continue
                for row in _read_csv(path)[: (limit or 10**9)]:
                    daily_note_id = safe_str(row.get("daily_note_id"))
                    note = None
                    if daily_note_id:
                        note = conn.execute(
                            """
                            SELECT note_id, visit_id FROM core.clinical_note
                            WHERE external_daily_note_id = %s
                            """,
                            (daily_note_id,),
                        ).fetchone()
                    pid = upsert_patient(
                        conn,
                        webpt_patient_id=row.get("patient_id"),
                        patient_name=row.get("patient_name"),
                        etl_run_id=etl_id,
                    )
                    conn.execute(
                        """
                        INSERT INTO billing.audit_finding (
                            visit_id, note_id, patient_id, finding_kind, rule_id,
                            severity, detail, source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb,
                            'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            detail = EXCLUDED.detail,
                            severity = EXCLUDED.severity,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            str(note["visit_id"]) if note else None,
                            str(note["note_id"]) if note else None,
                            pid,
                            kind,
                            safe_str(row.get("rule_id")),
                            safe_str(row.get("severity")),
                            json.dumps(dict(row)),
                            f"{kind}:{daily_note_id}:{row.get('rule_id')}",
                            etl_id,
                        ),
                    )
                    counts["audit_findings"] += 1

            edoc_root = legacy if list(legacy.glob("edocs_manifest*.csv")) else root
            manifests = sorted(
                edoc_root.glob("edocs_manifest*.csv"), key=lambda p: p.stat().st_mtime
            )
            if manifests:
                for row in _read_csv(manifests[-1])[: (limit or 10**9)]:
                    pid = upsert_patient(
                        conn,
                        webpt_patient_id=row.get("patient_id"),
                        patient_name=row.get("patient_name"),
                        etl_run_id=etl_id,
                    )
                    src = safe_str(row.get("doc_source")) or "edoc"
                    if src not in {"edoc", "chart_note", "upload", "mail", "drive"}:
                        src = "edoc"
                    dtype = doc_types.get("daily_note" if src == "chart_note" else "other")
                    case_pk = None
                    if safe_str(row.get("case_id")):
                        case_pk = upsert_case(
                            conn,
                            webpt_case_id=row.get("case_id"),
                            patient_id=pid,
                            etl_run_id=etl_id,
                        )
                    conn.execute(
                        """
                        INSERT INTO docs.document (
                            patient_id, case_pk, document_type_id, ext_doc_id, filename,
                            storage_path, source, status, status_description, error,
                            source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                            'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            status = EXCLUDED.status,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            pid,
                            case_pk,
                            dtype,
                            safe_str(row.get("ext_doc_id")),
                            safe_str(row.get("filename")),
                            safe_str(row.get("path")),
                            src,
                            safe_str(row.get("status")),
                            safe_str(row.get("status_description")),
                            safe_str(row.get("error")),
                            safe_str(row.get("ext_doc_id")) or safe_str(row.get("filename")),
                            etl_id,
                        ),
                    )
                    counts["documents"] += 1

            finish_etl_run(conn, etl_id, status="success", row_count=sum(counts.values()))
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
