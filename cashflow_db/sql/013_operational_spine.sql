-- Operational Data Platform: spine, lineage, warehouse gaps, forecast features.

-- ---------------------------------------------------------------------------
-- Schema gaps (case_label, schedule copay/deductible)
-- ---------------------------------------------------------------------------
ALTER TABLE core.patient_case
    ADD COLUMN IF NOT EXISTS case_label text;

ALTER TABLE core.schedule_appointment
    ADD COLUMN IF NOT EXISTS copay numeric(14, 2);

ALTER TABLE core.schedule_appointment
    ADD COLUMN IF NOT EXISTS deductible numeric(14, 2);

-- ---------------------------------------------------------------------------
-- Forecast run lineage
-- ---------------------------------------------------------------------------
ALTER TABLE analytics.forecast_run
    ADD COLUMN IF NOT EXISTS reconciliation_run_id uuid;

ALTER TABLE analytics.forecast_run
    ADD COLUMN IF NOT EXISTS rules_version text;

ALTER TABLE analytics.forecast_run
    ADD COLUMN IF NOT EXISTS source_etl_run_ids jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE analytics.forecast_prediction
    ADD COLUMN IF NOT EXISTS webpt_patient_id text;

ALTER TABLE analytics.forecast_prediction
    ADD COLUMN IF NOT EXISTS case_id text;

ALTER TABLE analytics.forecast_prediction
    ADD COLUMN IF NOT EXISTS cpt_code text;

ALTER TABLE analytics.forecast_prediction
    ADD COLUMN IF NOT EXISTS date_of_service date;

ALTER TABLE analytics.forecast_prediction
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Operational spine: reconciliation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing.reconciliation_run (
    reconciliation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'failed')),
    source_etl_run_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    rules_version text,
    row_count int,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS billing.reconciliation_line (
    reconciliation_line_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_run_id uuid NOT NULL
        REFERENCES billing.reconciliation_run (reconciliation_run_id) ON DELETE CASCADE,
    webpt_patient_id text,
    patient_name text,
    dob date,
    facility_id text,
    facility_name text,
    case_id text,
    ins_name text,
    insurance_note text,
    insurance_revflow text,
    date_of_service date,
    cpt_code text,
    modifier text,
    status text,
    paid_amount numeric(14, 2),
    allowed_amount numeric(14, 2),
    adjustment_amount numeric(14, 2),
    deductible_amount numeric(14, 2),
    eob_date date,
    check_eft_num text,
    carcs text,
    expected_copay numeric(14, 2),
    expected_deductible numeric(14, 2),
    match_level text,
    confidence numeric(8, 4),
    insurance_mismatch boolean,
    daily_note_id text,
    note_file text,
    visit_id uuid REFERENCES core.visit (visit_id),
    service_line_id uuid REFERENCES core.visit_service_line (service_line_id),
    eob_line_id uuid REFERENCES billing.eob_line (eob_line_id),
    patient_id uuid REFERENCES core.patient (patient_id),
    case_pk uuid REFERENCES core.patient_case (case_pk),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_recon_line_run
    ON billing.reconciliation_line (reconciliation_run_id);

CREATE INDEX IF NOT EXISTS ix_recon_line_dos
    ON billing.reconciliation_line (date_of_service);

CREATE INDEX IF NOT EXISTS ix_recon_line_patient
    ON billing.reconciliation_line (webpt_patient_id);

CREATE TABLE IF NOT EXISTS billing.reconciliation_visit_agg (
    reconciliation_visit_agg_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_run_id uuid NOT NULL
        REFERENCES billing.reconciliation_run (reconciliation_run_id) ON DELETE CASCADE,
    facility_id text,
    case_id text,
    webpt_patient_id text,
    patient_name text,
    dob date,
    facility_name text,
    date_of_service date,
    total_billed_cpts int,
    total_paid numeric(14, 2),
    matched_paid numeric(14, 2),
    bonus_paid numeric(14, 2),
    unmatched_paid numeric(14, 2),
    visit_paid_total numeric(14, 2),
    unmatched_cpts int,
    paid_lines int,
    pending_lines int,
    visit_status text,
    primary_check_number text,
    primary_check_date date,
    primary_check_amount numeric(14, 2),
    secondary_check_number text,
    secondary_check_date date,
    secondary_check_amount numeric(14, 2)
);

CREATE INDEX IF NOT EXISTS ix_recon_visit_run
    ON billing.reconciliation_visit_agg (reconciliation_run_id);

-- FK for forecast_run.reconciliation_run_id (added after table exists)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_forecast_run_recon'
    ) THEN
        ALTER TABLE analytics.forecast_run
            ADD CONSTRAINT fk_forecast_run_recon
            FOREIGN KEY (reconciliation_run_id)
            REFERENCES billing.reconciliation_run (reconciliation_run_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Insurance behavior facts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.payor_behavior_summary (
    payor_behavior_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_run_id uuid REFERENCES billing.reconciliation_run (reconciliation_run_id),
    payor_key text NOT NULL,
    payor_raw text,
    check_count int,
    median_cash_velocity_days numeric(10, 2),
    p75_cash_velocity_days numeric(10, 2),
    median_eob_to_deposit_days numeric(10, 2),
    deposit_weekday_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_payor_behavior_run
    ON analytics.payor_behavior_summary (reconciliation_run_id);

CREATE TABLE IF NOT EXISTS analytics.checks_timeline (
    checks_timeline_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_run_id uuid REFERENCES billing.reconciliation_run (reconciliation_run_id),
    check_eft_num text,
    payor_raw text,
    eob_date date,
    deposit_date date,
    paid_amount numeric(14, 2),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_checks_timeline_run
    ON analytics.checks_timeline (reconciliation_run_id);

-- ---------------------------------------------------------------------------
-- Forecast feature store (payer_sla, risk, cash series, …)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.forecast_feature (
    forecast_feature_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    forecast_run_id uuid NOT NULL REFERENCES analytics.forecast_run (forecast_run_id)
        ON DELETE CASCADE,
    feature_kind text NOT NULL,
    feature_key text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_forecast_feature_run_kind
    ON analytics.forecast_feature (forecast_run_id, feature_kind);

-- ---------------------------------------------------------------------------
-- Docs extras (poc goals / referral icd) — lightweight
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS docs.poc_goal (
    poc_goal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid REFERENCES docs.document (document_id),
    poc_id text,
    goal_text text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_poc_goal_natural
    ON docs.poc_goal (source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS docs.referral_icd (
    referral_icd_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid REFERENCES docs.document (document_id),
    patient_id uuid REFERENCES core.patient (patient_id),
    icd_code text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_icd_natural
    ON docs.referral_icd (source_natural_key)
    WHERE source_natural_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Denial natural key for Waystar idempotency
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_denial_record_natural
    ON billing.denial_record (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_eob_check_source_natural
    ON billing.eob_check (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_eob_line_natural
    ON billing.eob_line (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_source_natural
    ON billing.claim (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_line_source_natural
    ON billing.claim_line (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_finding_natural
    ON billing.audit_finding (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_service_line_natural
    ON core.visit_service_line (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_authorization_natural
    ON core.authorization (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_natural
    ON docs.document (source_system, source_natural_key)
    WHERE source_natural_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_of_care_poc_id
    ON docs.plan_of_care_detail (poc_id)
    WHERE poc_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Marts: spine + patient payments
-- ---------------------------------------------------------------------------
-- DROP first: CREATE OR REPLACE cannot rename/reorder columns vs earlier migrations
DROP VIEW IF EXISTS mart.v_payor_behavior_summary CASCADE;
DROP VIEW IF EXISTS mart.v_patient_payments_daily CASCADE;
DROP VIEW IF EXISTS mart.v_reconciliation_lines CASCADE;

CREATE OR REPLACE VIEW mart.v_reconciliation_lines AS
SELECT rl.*
FROM billing.reconciliation_line rl
JOIN billing.reconciliation_run rr ON rr.reconciliation_run_id = rl.reconciliation_run_id
WHERE rr.status = 'success'
  AND rr.created_at = (
      SELECT MAX(created_at) FROM billing.reconciliation_run WHERE status = 'success'
  );

CREATE OR REPLACE VIEW mart.v_patient_payments_daily AS
SELECT
    service_date AS cash_date,
    payment_category,
    SUM(COALESCE(amount_paid, 0)) AS amount_paid,
    COUNT(*) AS payment_count
FROM billing.patient_payment
WHERE service_date IS NOT NULL
GROUP BY service_date, payment_category;

CREATE OR REPLACE VIEW mart.v_payor_behavior_summary AS
SELECT
    payor_key,
    payor_raw,
    check_count,
    median_cash_velocity_days,
    p75_cash_velocity_days,
    median_eob_to_deposit_days,
    deposit_weekday_profile,
    payload,
    reconciliation_run_id,
    created_at
FROM analytics.payor_behavior_summary
WHERE created_at = (
    SELECT MAX(created_at) FROM analytics.payor_behavior_summary p2
    WHERE p2.payor_key = analytics.payor_behavior_summary.payor_key
);
