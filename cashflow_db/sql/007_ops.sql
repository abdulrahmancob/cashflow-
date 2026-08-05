-- Ops workflows (mail Phase 1; CC Phase 2 reserved)
CREATE TABLE IF NOT EXISTS ops.mail_work_item (
    work_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payer_label text,
    collector text,
    item_type text,
    notes text,
    previously_posted_flag boolean NOT NULL DEFAULT false,
    linked_eob_check_id uuid REFERENCES billing.eob_check (eob_check_id),
    linked_deposit_id uuid REFERENCES billing.bank_deposit (deposit_id),
    status text,
    source_system text,
    source_natural_key text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE TABLE IF NOT EXISTS ops.note_issue (
    note_issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    visit_id uuid REFERENCES core.visit (visit_id),
    note_id uuid REFERENCES core.clinical_note (note_id),
    issue_type text,
    severity text,
    therapist text,
    status text,
    opened_at timestamptz,
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS ops.outreach_event (
    outreach_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id uuid REFERENCES core.patient (patient_id),
    visit_id uuid REFERENCES core.visit (visit_id),
    channel text
        CHECK (channel IS NULL OR channel IN ('sms', 'call', 'email')),
    campaign_type text
        CHECK (campaign_type IS NULL OR campaign_type IN (
            'next_day_confirm', 'no_upcoming', 'auth_approval', 'self_discharge'
        )),
    response text,
    disposition text,
    agent_id text,
    sent_at timestamptz,
    responded_at timestamptz
);

CREATE TABLE IF NOT EXISTS ops.coverage_hold_action (
    hold_action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    coverage_id uuid REFERENCES core.patient_coverage (coverage_id),
    auth_id uuid REFERENCES core.authorization (auth_id),
    action text,
    appointments_cancelled int,
    agent_id text,
    acted_at timestamptz NOT NULL DEFAULT now()
);
