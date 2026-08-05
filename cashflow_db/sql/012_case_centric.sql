-- Case-centric alignment: schedule appointments, coverage/visit links,
-- patient payments, Snowflake KPI staging, refreshed marts.

-- ---------------------------------------------------------------------------
-- core.visit: insurance snapshot + coverage FK
-- ---------------------------------------------------------------------------
ALTER TABLE core.visit
    ADD COLUMN IF NOT EXISTS coverage_id uuid REFERENCES core.patient_coverage (coverage_id);

ALTER TABLE core.visit
    ADD COLUMN IF NOT EXISTS insurance_name_raw text;

ALTER TABLE core.visit
    ADD COLUMN IF NOT EXISTS webpt_appointment_id text;

-- ---------------------------------------------------------------------------
-- core.patient_coverage: optional case scope
-- ---------------------------------------------------------------------------
ALTER TABLE core.patient_coverage
    ADD COLUMN IF NOT EXISTS case_pk uuid REFERENCES core.patient_case (case_pk);

CREATE INDEX IF NOT EXISTS ix_coverage_case
    ON core.patient_coverage (case_pk)
    WHERE case_pk IS NOT NULL;

-- ---------------------------------------------------------------------------
-- First-class scheduler appointments (analytics + clinical selection)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.schedule_appointment (
    schedule_appointment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_pk uuid NOT NULL REFERENCES core.patient_case (case_pk),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    facility_id uuid REFERENCES ref.facility (facility_id),
    visit_id uuid REFERENCES core.visit (visit_id),
    service_date date NOT NULL,
    appointment_at timestamptz NOT NULL,
    webpt_appointment_id text,
    visit_status_raw text,
    status text NOT NULL DEFAULT 'scheduled'
        CHECK (status IN (
            'scheduled', 'confirmed', 'completed', 'cancelled', 'no_show', 'unchecked_out'
        )),
    check_in_at timestamptz,
    check_out_at timestamptz,
    insurance_name_raw text,
    is_selected_clinical boolean NOT NULL DEFAULT false,
    source_system text NOT NULL DEFAULT 'webpt',
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_appt_case_at
    ON core.schedule_appointment (case_pk, appointment_at);

CREATE INDEX IF NOT EXISTS ix_schedule_appt_service_date
    ON core.schedule_appointment (service_date);

CREATE INDEX IF NOT EXISTS ix_schedule_appt_visit
    ON core.schedule_appointment (visit_id)
    WHERE visit_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_schedule_appt_selected
    ON core.schedule_appointment (case_pk, service_date)
    WHERE is_selected_clinical;

-- ---------------------------------------------------------------------------
-- billing.patient_payment
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing.patient_payment (
    patient_payment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    case_pk uuid REFERENCES core.patient_case (case_pk),
    visit_id uuid REFERENCES core.visit (visit_id),
    facility_id uuid REFERENCES ref.facility (facility_id),
    service_date date,
    transaction_date date,
    payment_category text NOT NULL
        CHECK (payment_category IN (
            'Copay', 'Other', 'Wellness', 'Deductible', 'Supplies', 'Internal Payment'
        )),
    payment_type text,
    description text,
    amount_due numeric(14, 2),
    amount_paid numeric(14, 2),
    paid_method text,
    credit_type text,
    auth_check text,
    total_charge numeric(14, 2),
    total_paid numeric(14, 2),
    balance numeric(14, 2),
    source_system text NOT NULL DEFAULT 'webpt',
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_payment_natural
    ON billing.patient_payment (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_patient_payment_service
    ON billing.patient_payment (service_date);

CREATE INDEX IF NOT EXISTS ix_patient_payment_patient
    ON billing.patient_payment (patient_id);

-- ---------------------------------------------------------------------------
-- analytics.snowflake_visit_kpi (EMR+DOS staging; never picks case_id)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.snowflake_visit_kpi (
    snowflake_visit_kpi_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    emr_id text NOT NULL,
    date_of_service date NOT NULL,
    patient_id uuid REFERENCES core.patient (patient_id),
    patient_name text,
    insurance text,
    clinic text,
    status text,
    charged_amount numeric(14, 2),
    insurance_payment numeric(14, 2),
    client_payment numeric(14, 2),
    co_insurance_payment numeric(14, 2),
    reductions numeric(14, 2),
    adjusted numeric(14, 2),
    sf_visit_id text,
    sf_billing_id text,
    primary_check_number text,
    primary_check_date date,
    primary_check_amount numeric(14, 2),
    secondary_check_number text,
    secondary_check_date date,
    secondary_check_amount numeric(14, 2),
    billed_date date,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_system text NOT NULL DEFAULT 'snowflake',
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sf_visit_kpi_emr_dos
    ON analytics.snowflake_visit_kpi (emr_id, date_of_service);

CREATE INDEX IF NOT EXISTS ix_sf_visit_kpi_dos
    ON analytics.snowflake_visit_kpi (date_of_service);

-- ---------------------------------------------------------------------------
-- Case-keyed / appointment marts
-- ---------------------------------------------------------------------------
-- DROP first: CREATE OR REPLACE cannot rename/reorder columns vs 010_views.sql
DROP VIEW IF EXISTS mart.v_snowflake_visit_dedupe_keys CASCADE;
DROP VIEW IF EXISTS mart.v_schedule_appointment_facts CASCADE;
DROP VIEW IF EXISTS mart.v_sf_vs_case_coverage CASCADE;
DROP VIEW IF EXISTS mart.v_reconciliation_patients CASCADE;
DROP VIEW IF EXISTS mart.v_reconciliation_visits CASCADE;
DROP VIEW IF EXISTS mart.v_unmatched_payments CASCADE;
DROP VIEW IF EXISTS mart.v_unmatched_webpt CASCADE;

CREATE OR REPLACE VIEW mart.v_unmatched_webpt AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    f.webpt_facility_id AS facility_id,
    f.name AS facility_name,
    pc.webpt_case_id AS case_id,
    cov.raw_insurance_name AS ins_name,
    v.service_date AS date_of_service,
    v.appointment_at,
    sl.cpt_code,
    sl.modifiers,
    'no_eob_line'::text AS reason
FROM core.visit_service_line sl
JOIN core.visit v ON v.visit_id = sl.visit_id
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN core.patient_coverage cov
    ON cov.coverage_id = v.coverage_id
    OR (v.coverage_id IS NULL AND cov.patient_id = p.patient_id AND cov.is_primary)
LEFT JOIN billing.claim_line cl ON cl.service_line_id = sl.service_line_id
LEFT JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
WHERE el.eob_line_id IS NULL;

CREATE OR REPLACE VIEW mart.v_unmatched_payments AS
SELECT
    el.revflow_patient_id,
    el.date_of_service,
    el.cpt_code,
    el.modifiers,
    el.paid_amount,
    ec.payor_raw AS payor,
    ec.check_eft_num,
    ec.eob_date,
    ec.source_file,
    'no_claim_line'::text AS reason
FROM billing.eob_line el
JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
WHERE el.claim_line_id IS NULL;

CREATE OR REPLACE VIEW mart.v_reconciliation_visits AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    f.webpt_facility_id AS facility_id,
    f.name AS facility_name,
    pc.webpt_case_id AS case_id,
    v.service_date AS date_of_service,
    v.appointment_at,
    v.status AS visit_status,
    COUNT(DISTINCT sl.service_line_id) AS total_billed_cpts,
    COALESCE(SUM(el.paid_amount), 0) AS total_paid,
    COALESCE(SUM(el.paid_amount) FILTER (WHERE el.claim_line_id IS NOT NULL), 0) AS matched_paid,
    COUNT(DISTINCT cl.claim_line_id) FILTER (
        WHERE cl.claim_line_id IS NOT NULL AND el.eob_line_id IS NULL
    ) AS pending_lines,
    COUNT(DISTINCT el.eob_line_id) AS paid_lines
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
LEFT JOIN core.visit_service_line sl ON sl.visit_id = v.visit_id
LEFT JOIN billing.claim_line cl ON cl.visit_id = v.visit_id
LEFT JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
GROUP BY
    p.webpt_patient_id, ph.patient_name, ph.dob,
    f.webpt_facility_id, f.name, pc.webpt_case_id,
    v.service_date, v.appointment_at, v.status;

CREATE OR REPLACE VIEW mart.v_reconciliation_patients AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    f.webpt_facility_id AS facility_id,
    f.name AS facility_name,
    pc.webpt_case_id AS case_id,
    cov.raw_insurance_name AS ins_name,
    pc.assigned_therapist,
    (
        SELECT a.visits_authorized
        FROM core.authorization a
        WHERE a.case_pk = pc.case_pk
        ORDER BY a.end_date DESC NULLS LAST
        LIMIT 1
    ) AS auth_ins_visits,
    (
        SELECT COUNT(*) FROM core.visit v
        WHERE v.case_pk = pc.case_pk
    ) AS visits_total,
    (
        SELECT COALESCE(SUM(el.paid_amount), 0)
        FROM billing.claim c
        JOIN billing.claim_line cl ON cl.claim_id = c.claim_id
        JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
        WHERE c.case_pk = pc.case_pk
    ) AS total_paid
FROM core.patient_case pc
JOIN core.patient p ON p.patient_id = pc.patient_id
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN ref.facility f ON f.facility_id = pc.facility_id
LEFT JOIN LATERAL (
    SELECT *
    FROM core.patient_coverage cov2
    WHERE cov2.patient_id = p.patient_id
      AND (cov2.case_pk = pc.case_pk OR cov2.case_pk IS NULL)
    ORDER BY
        CASE WHEN cov2.case_pk = pc.case_pk THEN 0 ELSE 1 END,
        cov2.is_primary DESC,
        cov2.effective_from DESC NULLS LAST
    LIMIT 1
) cov ON true;

CREATE OR REPLACE VIEW mart.v_sf_vs_case_coverage AS
SELECT
    sf.emr_id,
    sf.date_of_service,
    sf.clinic AS sf_clinic,
    sf.status AS sf_status,
    sf.charged_amount AS sf_charged,
    sf.insurance_payment AS sf_insurance_payment,
    p.webpt_patient_id,
    COUNT(DISTINCT v.visit_id) AS case_visit_count,
    COUNT(DISTINCT pc.webpt_case_id) AS case_count,
    CASE
        WHEN COUNT(DISTINCT v.visit_id) = 0 THEN 'sf_only'
        WHEN COUNT(DISTINCT v.visit_id) = 1 THEN 'matched'
        ELSE 'multi_case_same_dos'
    END AS coverage_flag
FROM analytics.snowflake_visit_kpi sf
LEFT JOIN core.patient p ON p.webpt_patient_id = sf.emr_id
LEFT JOIN core.visit v
    ON v.patient_id = p.patient_id AND v.service_date = sf.date_of_service
LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
GROUP BY
    sf.emr_id, sf.date_of_service, sf.clinic, sf.status,
    sf.charged_amount, sf.insurance_payment, p.webpt_patient_id;

CREATE OR REPLACE VIEW mart.v_schedule_appointment_facts AS
SELECT
    sa.schedule_appointment_id,
    f.webpt_facility_id AS facility_id,
    f.name AS facility_name,
    pc.webpt_case_id AS case_id,
    p.webpt_patient_id,
    ph.patient_name,
    sa.service_date,
    sa.appointment_at,
    sa.visit_status_raw,
    sa.status,
    sa.check_in_at,
    sa.check_out_at,
    sa.is_selected_clinical,
    sa.visit_id,
    CASE
        WHEN sa.check_in_at IS NOT NULL AND sa.appointment_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (sa.check_in_at - sa.appointment_at)) / 60.0
        ELSE NULL
    END AS arrival_delay_minutes,
    CASE
        WHEN sa.check_in_at IS NOT NULL AND sa.check_out_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (sa.check_out_at - sa.check_in_at)) / 60.0
        ELSE NULL
    END AS chair_minutes,
    (sa.status = 'no_show' OR sa.status = 'cancelled') AS is_no_show_or_cancel
FROM core.schedule_appointment sa
JOIN core.patient p ON p.patient_id = sa.patient_id
JOIN core.patient_case pc ON pc.case_pk = sa.case_pk
LEFT JOIN ref.facility f ON f.facility_id = sa.facility_id
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current;

CREATE OR REPLACE VIEW mart.v_snowflake_visit_dedupe_keys AS
SELECT
    p.name_key,
    v.service_date AS date_of_service,
    f.name AS facility_name,
    p.webpt_patient_id,
    pc.webpt_case_id AS case_id,
    v.visit_id,
    COUNT(*) OVER (PARTITION BY p.name_key, v.service_date) AS name_dos_dup_count
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN core.patient_case pc ON pc.case_pk = v.case_pk
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id;
