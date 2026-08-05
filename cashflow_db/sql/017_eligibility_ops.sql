-- Eligibility operational work queue (isolated from reconciliation facts)

CREATE TABLE IF NOT EXISTS ref.eligibility_status (
    status_key text PRIMARY KEY,
    display_name text NOT NULL,
    sort_order int NOT NULL DEFAULT 0,
    is_terminal boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true
);

INSERT INTO ref.eligibility_status (status_key, display_name, sort_order, is_terminal) VALUES
    ('pending', 'Pending', 10, false),
    ('checking', 'Checking', 20, false),
    ('waiting_patient', 'Waiting Patient', 30, false),
    ('waiting_insurance', 'Waiting Insurance', 40, false),
    ('completed', 'Completed', 50, true),
    ('rejected', 'Rejected', 60, true)
ON CONFLICT (status_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS ref.eligibility_change_reason (
    reason_key text PRIMARY KEY,
    display_name text NOT NULL,
    requires_text boolean NOT NULL DEFAULT false,
    sort_order int NOT NULL DEFAULT 0
);

INSERT INTO ref.eligibility_change_reason (reason_key, display_name, requires_text, sort_order) VALUES
    ('wrong_insurance', 'Wrong Insurance', false, 10),
    ('verified_by_phone', 'Verified by Phone', false, 20),
    ('duplicate', 'Duplicate', false, 30),
    ('corrected_after_eob', 'Corrected after EOB', false, 40),
    ('other', 'Other', true, 90)
ON CONFLICT (reason_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS ops.eligibility_work_item (
    work_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_name text NOT NULL,
    emr_patient_id text NOT NULL,
    dos date NOT NULL,
    patient_name text,
    dob date,
    insurance_name text,
    source_visit_status text,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    eligibility_status text NOT NULL DEFAULT 'pending'
        REFERENCES ref.eligibility_status (status_key),
    reference_number text,
    notes text,
    assigned_to uuid REFERENCES auth.app_user (user_id),
    assigned_at timestamptz,
    completed_at timestamptz,
    locked_by uuid REFERENCES auth.app_user (user_id),
    locked_at timestamptz,
    lock_expires_at timestamptz,
    updated_by uuid REFERENCES auth.app_user (user_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    source_recon_run_id uuid,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    priority int NOT NULL DEFAULT 0,
    UNIQUE (facility_name, emr_patient_id, dos)
);

CREATE INDEX IF NOT EXISTS ix_elig_wi_status
    ON ops.eligibility_work_item (eligibility_status);
CREATE INDEX IF NOT EXISTS ix_elig_wi_facility
    ON ops.eligibility_work_item (facility_name);
CREATE INDEX IF NOT EXISTS ix_elig_wi_dos
    ON ops.eligibility_work_item (dos);
CREATE INDEX IF NOT EXISTS ix_elig_wi_assigned
    ON ops.eligibility_work_item (assigned_to);
CREATE INDEX IF NOT EXISTS ix_elig_wi_updated
    ON ops.eligibility_work_item (updated_at DESC);

CREATE TABLE IF NOT EXISTS ops.eligibility_history (
    history_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id uuid NOT NULL REFERENCES ops.eligibility_work_item (work_item_id) ON DELETE CASCADE,
    column_name text NOT NULL,
    old_value text,
    new_value text,
    changed_by uuid REFERENCES auth.app_user (user_id),
    changed_at timestamptz NOT NULL DEFAULT now(),
    reason_key text REFERENCES ref.eligibility_change_reason (reason_key),
    reason_text text
);

CREATE INDEX IF NOT EXISTS ix_elig_hist_item
    ON ops.eligibility_history (work_item_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS ops.eligibility_comment (
    comment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id uuid NOT NULL REFERENCES ops.eligibility_work_item (work_item_id) ON DELETE CASCADE,
    body text NOT NULL,
    created_by uuid REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_elig_comment_item
    ON ops.eligibility_comment (work_item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ops.eligibility_attachment (
    attachment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id uuid NOT NULL REFERENCES ops.eligibility_work_item (work_item_id) ON DELETE CASCADE,
    document_id uuid REFERENCES docs.document (document_id),
    storage_path text,
    filename text,
    doc_kind text NOT NULL DEFAULT 'eligibility_pdf',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_elig_attach_doc
    ON ops.eligibility_attachment (work_item_id, doc_kind, document_id)
    WHERE document_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_elig_attach_path
    ON ops.eligibility_attachment (work_item_id, doc_kind, storage_path)
    WHERE storage_path IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_elig_attach_item
    ON ops.eligibility_attachment (work_item_id);
