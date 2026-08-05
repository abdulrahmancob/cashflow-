-- Reference tables
CREATE TABLE IF NOT EXISTS ref.payer_org (
    payer_org_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    short_code text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ref.insurance_product (
    insurance_product_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payer_org_id uuid NOT NULL REFERENCES ref.payer_org (payer_org_id),
    name text NOT NULL,
    product_class text NOT NULL DEFAULT 'other'
        CHECK (product_class IN (
            'commercial', 'medicaid', 'medicare', 'workers_comp', 'no_fault', 'other'
        )),
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ref.insurance_plan (
    insurance_plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    insurance_product_id uuid NOT NULL REFERENCES ref.insurance_product (insurance_product_id),
    name text NOT NULL,
    plan_type text,
    reimbursement_model text NOT NULL DEFAULT 'percent_of_charge'
        CHECK (reimbursement_model IN ('percent_of_charge', 'flat_per_visit')),
    flat_fee_amount numeric(14, 2),
    auth_pattern text NOT NULL DEFAULT 'pre_service'
        CHECK (auth_pattern IN ('pattern_based', 'pre_service', 'none_dummy')),
    direct_access_visit_limit int,
    default_sla_days int,
    high_season_sla_days int,
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ref.submission_route (
    submission_route_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE
        CHECK (code IN ('waystar', 'zaya', 'manual')),
    name text NOT NULL,
    description text
);

CREATE TABLE IF NOT EXISTS ref.facility (
    facility_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    webpt_facility_id text UNIQUE,
    name text NOT NULL,
    clinic_cluster text,
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ref.cpt_code (
    cpt_code text PRIMARY KEY,
    description text,
    is_timed boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ref.icd10_code (
    icd10_code text PRIMARY KEY,
    description text,
    is_billable boolean NOT NULL DEFAULT true,
    is_header boolean NOT NULL DEFAULT false,
    effective_from date,
    effective_to date
);

CREATE TABLE IF NOT EXISTS ref.document_type (
    document_type_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE
        CHECK (code IN (
            'referral', 'poc', 'prescription', 'denial', 'appeal',
            'mri', 'lab', 'daily_note', 'remittance', 'other'
        )),
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS ref.denial_reason_taxonomy (
    reason_taxonomy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    category text NOT NULL,
    default_error_owner text NOT NULL
        CHECK (default_error_owner IN (
            'front_desk', 'medical_audit', 'authorization', 'coding', 'payer_behavior'
        )),
    description text
);

CREATE TABLE IF NOT EXISTS ref.insurance_alias (
    alias_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system text NOT NULL,
    raw_name text NOT NULL,
    payer_org_id uuid REFERENCES ref.payer_org (payer_org_id),
    insurance_plan_id uuid REFERENCES ref.insurance_plan (insurance_plan_id),
    revflow_payor text,
    match_count int NOT NULL DEFAULT 0,
    is_mapped boolean NOT NULL DEFAULT false,
    UNIQUE (source_system, raw_name)
);

CREATE TABLE IF NOT EXISTS ref.payer_cpt_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    insurance_plan_id uuid REFERENCES ref.insurance_plan (insurance_plan_id),
    rule_kind text NOT NULL,
    expected_value text,
    severity text NOT NULL DEFAULT 'warning',
    detail text
);

CREATE TABLE IF NOT EXISTS ref.icd_denial_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    category text NOT NULL,
    description text,
    examples text,
    correct_approach text,
    severity text NOT NULL DEFAULT 'error'
);

CREATE INDEX IF NOT EXISTS ix_insurance_alias_raw ON ref.insurance_alias (raw_name);
CREATE INDEX IF NOT EXISTS ix_facility_name ON ref.facility (name);
