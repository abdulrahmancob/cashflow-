-- Checks & Deposits portal ledger (Deposit Date on or after 2026-01-01).
-- Idempotent: migrate() re-runs every SQL file.

CREATE TABLE IF NOT EXISTS billing.checks_deposits_row (
    row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sheet_key text NOT NULL,
    check_date date,
    payer text,
    amount numeric(14, 2),
    check_number text,
    deposit_date date NOT NULL,
    deposit_month date,
    link text,
    notes text,
    version int NOT NULL DEFAULT 1,
    deleted_at timestamptz,
    deleted_by uuid REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES auth.app_user (user_id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid REFERENCES auth.app_user (user_id),
    CONSTRAINT ck_checks_deposits_deposit_year CHECK (deposit_date >= DATE '2026-01-01')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_checks_deposits_sheet_key_active
    ON billing.checks_deposits_row (sheet_key)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_checks_deposits_deposit_date_active
    ON billing.checks_deposits_row (deposit_date)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_checks_deposits_deposit_month_active
    ON billing.checks_deposits_row (deposit_month)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_checks_deposits_check_number_active
    ON billing.checks_deposits_row (check_number)
    WHERE deleted_at IS NULL AND check_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_checks_deposits_payer_active
    ON billing.checks_deposits_row (payer)
    WHERE deleted_at IS NULL AND payer IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing.checks_deposits_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL DEFAULT 'row'
        CHECK (entity_type IN ('row', 'grant')),
    row_id uuid,
    sheet_key text,
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

CREATE INDEX IF NOT EXISTS ix_checks_deposits_audit_row
    ON billing.checks_deposits_audit (row_id, acted_at DESC);

CREATE INDEX IF NOT EXISTS ix_checks_deposits_audit_sheet_key
    ON billing.checks_deposits_audit (sheet_key, acted_at DESC);

CREATE INDEX IF NOT EXISTS ix_checks_deposits_audit_batch
    ON billing.checks_deposits_audit (upload_batch_id)
    WHERE upload_batch_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing.checks_deposits_upload_preview (
    preview_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by uuid NOT NULL REFERENCES auth.app_user (user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_checks_deposits_upload_preview_expires
    ON billing.checks_deposits_upload_preview (expires_at);

-- Allow checks_deposits grants without dropping the tracker resource.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'auth.resource_grant'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%resource_key%'
    LOOP
        EXECUTE format('ALTER TABLE auth.resource_grant DROP CONSTRAINT %I', r.conname);
    END LOOP;
    ALTER TABLE auth.resource_grant ADD CONSTRAINT resource_grant_resource_key_check
        CHECK (resource_key IN ('transaction_tracker', 'checks_deposits'));
END $$;
