-- Billing: claims, EOBs, deposits
CREATE TABLE IF NOT EXISTS billing.claim (
    claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_number text,
    case_pk uuid REFERENCES core.patient_case (case_pk),
    patient_id uuid NOT NULL REFERENCES core.patient (patient_id),
    coverage_id uuid REFERENCES core.patient_coverage (coverage_id),
    insurance_plan_id uuid REFERENCES ref.insurance_plan (insurance_plan_id),
    submission_route_id uuid REFERENCES ref.submission_route (submission_route_id),
    payer_sequence text NOT NULL DEFAULT 'primary'
        CHECK (payer_sequence IN ('primary', 'secondary')),
    parent_claim_id uuid REFERENCES billing.claim (claim_id),
    force_rejected_from_waystar boolean NOT NULL DEFAULT false,
    submit_date date,
    service_date_from date,
    service_date_to date,
    status_current text,
    billed_total numeric(14, 2),
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing.claim_line (
    claim_line_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES billing.claim (claim_id),
    visit_id uuid REFERENCES core.visit (visit_id),
    service_line_id uuid REFERENCES core.visit_service_line (service_line_id),
    line_no int,
    cpt_code text,
    modifiers text,
    units int,
    billed_amount numeric(14, 2),
    allowed_amount numeric(14, 2),
    expected_amount numeric(14, 2),
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.claim_event (
    claim_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid NOT NULL REFERENCES billing.claim (claim_id),
    claim_line_id uuid REFERENCES billing.claim_line (claim_line_id),
    event_type text NOT NULL
        CHECK (event_type IN (
            'created', 'internal_hold', 'ready_for_submission', 'submitted',
            'force_rejected_waystar', 'submitted_zaya', 'dropped_unsubmitted',
            'clearinghouse_rejected', 'accepted', 'payer_denied', 'era_received',
            'appealed', 'adjusted', 'patient_responsibility', 'self_pay_converted',
            'deposit_posted', 'closed', 'merged', 'relinked'
        )),
    event_at timestamptz NOT NULL DEFAULT now(),
    actor_user_id text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_system text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.denial_record (
    denial_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id uuid REFERENCES billing.claim (claim_id),
    claim_line_id uuid REFERENCES billing.claim_line (claim_line_id),
    facility_id uuid REFERENCES ref.facility (facility_id),
    reason_taxonomy_id uuid REFERENCES ref.denial_reason_taxonomy (reason_taxonomy_id),
    reason_code text,
    error_owner text
        CHECK (error_owner IS NULL OR error_owner IN (
            'front_desk', 'medical_audit', 'authorization', 'coding', 'payer_behavior'
        )),
    is_partial_denial boolean NOT NULL DEFAULT false,
    source text,
    denied_amount numeric(14, 2),
    denial_date date,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.audit_finding (
    finding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    visit_id uuid REFERENCES core.visit (visit_id),
    note_id uuid REFERENCES core.clinical_note (note_id),
    patient_id uuid REFERENCES core.patient (patient_id),
    finding_kind text NOT NULL
        CHECK (finding_kind IN ('cpt_rule', 'icd_rule')),
    rule_id text,
    severity text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.eob_check (
    eob_check_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    eob_key text,
    company_id text,
    check_eft_num text,
    payor_raw text,
    check_date date,
    eob_date date,
    report_from date,
    report_to date,
    paid_amount_sum numeric(14, 2),
    source_file text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

-- NULLS NOT DISTINCT avoids non-IMMUTABLE COALESCE/cast expressions in the index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_eob_check_natural
    ON billing.eob_check (eob_key, check_eft_num, eob_date)
    NULLS NOT DISTINCT;

CREATE TABLE IF NOT EXISTS billing.eob_line (
    eob_line_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    eob_check_id uuid NOT NULL REFERENCES billing.eob_check (eob_check_id),
    revflow_patient_id text,
    patient_id uuid REFERENCES core.patient (patient_id),
    claim_line_id uuid REFERENCES billing.claim_line (claim_line_id),
    date_of_service date,
    cpt_code text,
    modifiers text,
    units int,
    billed_amount numeric(14, 2),
    allowed_amount numeric(14, 2),
    paid_amount numeric(14, 2),
    adjustment_amount numeric(14, 2),
    deductible_amount numeric(14, 2),
    carcs text,
    pr_oa_codes text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.eob_carc_raw (
    eob_carc_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    eob_check_id uuid NOT NULL REFERENCES billing.eob_check (eob_check_id),
    revflow_patient_id text,
    date_of_service date,
    cpt_code text,
    carc_code text,
    adjustment_amount numeric(14, 2),
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.bank_deposit (
    deposit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id_external text UNIQUE,
    channel text NOT NULL DEFAULT 'eft'
        CHECK (channel IN (
            'eft', 'mail_check', 'v_card', 'direct_deposit', 'ach', 'other'
        )),
    check_date_recognized date,
    bank_posting_date date,
    amount numeric(14, 2),
    transaction_type text,
    bank_name text,
    description text,
    billing_status text,
    collector text,
    posted boolean,
    notes text,
    eft_1 text,
    eft_2 text,
    eft_last4 text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS billing.deposit_check_allocation (
    allocation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deposit_id uuid NOT NULL REFERENCES billing.bank_deposit (deposit_id),
    eob_check_id uuid NOT NULL REFERENCES billing.eob_check (eob_check_id),
    allocated_amount numeric(14, 2),
    match_method text
        CHECK (match_method IS NULL OR match_method IN (
            'eft1', 'eft2', 'manual', 'mail_sheet', 'last4'
        )),
    confidence numeric(5, 4),
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    UNIQUE (deposit_id, eob_check_id)
);

CREATE INDEX IF NOT EXISTS ix_claim_patient ON billing.claim (patient_id);
CREATE INDEX IF NOT EXISTS ix_claim_line_claim ON billing.claim_line (claim_id);
CREATE INDEX IF NOT EXISTS ix_claim_event_claim ON billing.claim_event (claim_id, event_at);
CREATE INDEX IF NOT EXISTS ix_eob_line_check ON billing.eob_line (eob_check_id);
CREATE INDEX IF NOT EXISTS ix_eob_line_dos ON billing.eob_line (date_of_service);
CREATE INDEX IF NOT EXISTS ix_deposit_posting ON billing.bank_deposit (bank_posting_date);
CREATE INDEX IF NOT EXISTS ix_deposit_eft ON billing.bank_deposit (eft_1, eft_2);
