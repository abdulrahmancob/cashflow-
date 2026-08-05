-- Transaction Tracker portal: editable rows, ACL grants, audit, upload preview staging

CREATE TABLE IF NOT EXISTS billing.transaction_tracker_row (
    row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id text NOT NULL,
    month_date date,
    txn_date date,
    amount numeric(14, 2),
    eft_1 text,
    eft_2 text,
    transaction_type text,
    description text,
    check_reference text,
    bank_name text,
    billing_status text,
    collector text,
    posted boolean,
    notes text,
    assigned_date date,
    claims text,
    version int NOT NULL DEFAULT 1,
    deleted_at timestamptz,
    deleted_by uuid REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES auth.app_user (user_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid REFERENCES auth.app_user (user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tracker_payment_active
    ON billing.transaction_tracker_row (payment_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_tracker_txn_date_active
    ON billing.transaction_tracker_row (txn_date)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_tracker_month_date_active
    ON billing.transaction_tracker_row (month_date)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_tracker_eft1_active
    ON billing.transaction_tracker_row (eft_1)
    WHERE deleted_at IS NULL AND eft_1 IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_tracker_eft2_active
    ON billing.transaction_tracker_row (eft_2)
    WHERE deleted_at IS NULL AND eft_2 IS NOT NULL;

CREATE TABLE IF NOT EXISTS auth.resource_grant (
    grant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.app_user (user_id) ON DELETE CASCADE,
    resource_key text NOT NULL
        CHECK (resource_key IN ('transaction_tracker')),
    can_view boolean NOT NULL DEFAULT false,
    can_edit boolean NOT NULL DEFAULT false,
    can_upload boolean NOT NULL DEFAULT false,
    can_admin boolean NOT NULL DEFAULT false,
    granted_by uuid REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, resource_key),
    CONSTRAINT ck_resource_grant_view_required CHECK (
        NOT (can_edit OR can_upload OR can_admin) OR can_view
    )
);

CREATE INDEX IF NOT EXISTS ix_resource_grant_resource
    ON auth.resource_grant (resource_key);

CREATE TABLE IF NOT EXISTS billing.transaction_tracker_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL DEFAULT 'row'
        CHECK (entity_type IN ('row', 'grant')),
    row_id uuid,
    payment_id text,
    action text NOT NULL
        CHECK (action IN (
            'create', 'update', 'soft_delete', 'restore',
            'upload_apply', 'grant_change'
        )),
    actor_user_id uuid REFERENCES auth.app_user (user_id),
    acted_at timestamptz NOT NULL DEFAULT now(),
    before_json jsonb,
    after_json jsonb,
    upload_batch_id uuid,
    request_id text
);

CREATE INDEX IF NOT EXISTS ix_tracker_audit_row
    ON billing.transaction_tracker_audit (row_id, acted_at DESC);

CREATE INDEX IF NOT EXISTS ix_tracker_audit_payment
    ON billing.transaction_tracker_audit (payment_id, acted_at DESC);

CREATE INDEX IF NOT EXISTS ix_tracker_audit_batch
    ON billing.transaction_tracker_audit (upload_batch_id)
    WHERE upload_batch_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing.transaction_tracker_upload_preview (
    preview_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by uuid NOT NULL REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_tracker_upload_preview_expires
    ON billing.transaction_tracker_upload_preview (expires_at);
