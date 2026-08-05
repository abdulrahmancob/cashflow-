-- ETL lineage + governance audit trail
CREATE TABLE IF NOT EXISTS etl.etl_run (
    etl_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system text NOT NULL
        CHECK (source_system IN (
            'webpt', 'revflow', 'tracker', 'mail', 'rules',
            'waystar', 'zaya', 'snowflake', 'manual', 'forecast'
        )),
    source_uri text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'failed', 'partial')),
    row_count int,
    checksum text,
    notes text
);

CREATE TABLE IF NOT EXISTS gov.system_audit_log (
    audit_log_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    at timestamptz NOT NULL DEFAULT now(),
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id)
);

CREATE INDEX IF NOT EXISTS ix_audit_entity ON gov.system_audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_etl_run_source ON etl.etl_run (source_system, started_at DESC);
