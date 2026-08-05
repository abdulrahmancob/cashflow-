-- Core clinical / patient domain
CREATE TABLE IF NOT EXISTS core.patient (
    patient_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    webpt_patient_id text UNIQUE,
    revflow_patient_id text,
    name_key text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS core.patient_history (
    patient_history_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    patient_name text,
    dob date,
    mobile_phone text,
    home_phone text,
    work_phone text,
    email text,
    best_phone text,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    is_current boolean NOT NULL DEFAULT true
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_history_current
    ON core.patient_history (patient_id)
    WHERE is_current;

CREATE TABLE IF NOT EXISTS core.patient_case (
    case_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    webpt_case_id text UNIQUE,
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    facility_id uuid REFERENCES ref.facility (facility_id),
    assigned_therapist text,
    diagnosis_raw text,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'partial_discharge', 'full_discharge', 'closed')),
    discharge_reason text,
    opened_at date,
    closed_at date,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS core.patient_coverage (
    coverage_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    insurance_plan_id uuid REFERENCES ref.insurance_plan (insurance_plan_id),
    member_id text,
    payer_id_external text,
    is_network_eligible boolean,
    deductible numeric(14, 2),
    copay numeric(14, 2),
    limit_per_year int,
    referral_required boolean,
    effective_from date,
    effective_to date,
    is_primary boolean NOT NULL DEFAULT true,
    raw_insurance_name text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS core.lead_intake (
    lead_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid REFERENCES core.patient (patient_id),
    lead_source text
        CHECK (lead_source IS NULL OR lead_source IN (
            'phone', 'zocdoc', 'website', 'zoho', 'google_ads', 'other'
        )),
    campaign text,
    cost_per_lead numeric(14, 2),
    captured_at timestamptz,
    status text
);

CREATE TABLE IF NOT EXISTS core.authorization (
    auth_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_pk uuid REFERENCES core.patient_case (case_pk),
    coverage_id uuid REFERENCES core.patient_coverage (coverage_id),
    auth_kind text NOT NULL DEFAULT 'hard'
        CHECK (auth_kind IN ('hard', 'dummy')),
    auth_number text,
    visits_authorized int,
    visits_used int,
    start_date date,
    end_date date,
    status text NOT NULL DEFAULT 'approved'
        CHECK (status IN ('approved', 'non_ev', 'on_hold', 'expired', 'exhausted')),
    non_ev_tat_days int,
    hold_reason text
        CHECK (hold_reason IS NULL OR hold_reason IN (
            'cob', 'missing_referral', 'insurance_change', 'md_pending', 'other'
        )),
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    CONSTRAINT auth_end_date_wins CHECK (
        end_date IS NULL OR start_date IS NULL OR end_date >= start_date
    )
);

CREATE TABLE IF NOT EXISTS core.visit (
    visit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_pk uuid REFERENCES core.patient_case (case_pk),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    facility_id uuid REFERENCES ref.facility (facility_id),
    service_date date NOT NULL,
    appointment_at timestamptz,
    visit_type text
        CHECK (visit_type IS NULL OR visit_type IN (
            'follow_up', 'initial', 're_examination'
        )),
    visit_no int,
    status text NOT NULL DEFAULT 'completed'
        CHECK (status IN (
            'scheduled', 'confirmed', 'completed', 'cancelled', 'no_show', 'unchecked_out'
        )),
    check_in_at timestamptz,
    check_out_at timestamptz,
    confirmation_response text
        CHECK (confirmation_response IS NULL OR confirmation_response IN ('C', 'R', 'X', 'none')),
    green_board_units int,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_visit_case_service_date
    ON core.visit (case_pk, service_date)
    WHERE case_pk IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_visit_patient_service_nocase
    ON core.visit (patient_id, service_date)
    WHERE case_pk IS NULL;

CREATE TABLE IF NOT EXISTS core.authorization_visit (
    auth_visit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_id uuid NOT NULL REFERENCES core.authorization (auth_id),
    visit_id uuid NOT NULL REFERENCES core.visit (visit_id),
    units_consumed int NOT NULL DEFAULT 1,
    consumed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (auth_id, visit_id)
);

CREATE TABLE IF NOT EXISTS core.clinical_note (
    note_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    visit_id uuid NOT NULL REFERENCES core.visit (visit_id),
    external_daily_note_id text UNIQUE,
    note_kind text NOT NULL DEFAULT 'daily'
        CHECK (note_kind IN (
            'initial', 'daily', 'correction', 'addendum', 'poc', 'recert'
        )),
    note_date date,
    version_no int NOT NULL DEFAULT 1,
    note_file text,
    referring_physician text,
    diagnosis_raw text,
    diagnosis_icd_codes text,
    treatment_diagnosis_icd_codes text,
    insurance_name_raw text,
    extraction_method text,
    signed_at timestamptz,
    finalized_at timestamptz,
    sla_hours_target int NOT NULL DEFAULT 24,
    sla_breached boolean NOT NULL DEFAULT false,
    error text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS core.visit_service_line (
    service_line_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    visit_id uuid NOT NULL REFERENCES core.visit (visit_id),
    cpt_code text REFERENCES ref.cpt_code (cpt_code),
    modifiers text,
    units int,
    description text,
    billing_modifier_suffix text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE INDEX IF NOT EXISTS ix_patient_name_key ON core.patient (name_key);
CREATE INDEX IF NOT EXISTS ix_visit_service_date ON core.visit (service_date);
CREATE INDEX IF NOT EXISTS ix_visit_patient ON core.visit (patient_id);
CREATE INDEX IF NOT EXISTS ix_note_visit ON core.clinical_note (visit_id);
CREATE INDEX IF NOT EXISTS ix_svc_visit ON core.visit_service_line (visit_id);
