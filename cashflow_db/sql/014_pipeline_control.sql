-- RCM Processing Platform control plane (workflow engine state).

CREATE TABLE IF NOT EXISTS ops.pipeline_run (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of_date date NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'partial', 'cancelled'
        )),
    trigger_source text NOT NULL DEFAULT 'manual',
    lookback_days int NOT NULL DEFAULT 14,
    started_at timestamptz,
    finished_at timestamptz,
    notes text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (as_of_date, trigger_source, started_at)
);

CREATE INDEX IF NOT EXISTS ix_pipeline_run_as_of
    ON ops.pipeline_run (as_of_date DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_pipeline_run_status
    ON ops.pipeline_run (status, started_at DESC);

CREATE TABLE IF NOT EXISTS ops.stage_run (
    stage_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES ops.pipeline_run (run_id) ON DELETE CASCADE,
    stage_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'skipped', 'blocked'
        )),
    attempt int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 1,
    on_failure text NOT NULL DEFAULT 'stop'
        CHECK (on_failure IN ('stop', 'retry', 'continue_with_alert')),
    inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    outputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, stage_key)
);

CREATE INDEX IF NOT EXISTS ix_stage_run_run_status
    ON ops.stage_run (run_id, status);

CREATE INDEX IF NOT EXISTS ix_stage_run_key
    ON ops.stage_run (stage_key, status);

CREATE TABLE IF NOT EXISTS ops.stage_artifact (
    artifact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES ops.pipeline_run (run_id) ON DELETE CASCADE,
    stage_key text NOT NULL,
    artifact_key text NOT NULL,
    uri text,
    row_count bigint,
    checksum text,
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, artifact_key)
);

CREATE INDEX IF NOT EXISTS ix_stage_artifact_run
    ON ops.stage_artifact (run_id, stage_key);

CREATE TABLE IF NOT EXISTS ops.retry_item (
    retry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    stage_key text NOT NULL,
    item_type text NOT NULL,
    item_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'exhausted', 'cancelled'
        )),
    attempt int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_retry_item_due
    ON ops.retry_item (status, next_attempt_at)
    WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS ix_retry_item_key
    ON ops.retry_item (item_type, item_key);

CREATE TABLE IF NOT EXISTS ops.alert_event (
    alert_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    stage_key text,
    severity text NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('info', 'warning', 'critical')),
    alert_key text NOT NULL,
    message text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    notified boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_alert_event_run
    ON ops.alert_event (run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ops.daily_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of_date date NOT NULL UNIQUE,
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    forecast_run_id uuid,
    reconciliation_run_id uuid,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    volumes jsonb NOT NULL DEFAULT '{}'::jsonb,
    stage_statuses jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_daily_snapshot_as_of
    ON ops.daily_snapshot (as_of_date DESC);

CREATE TABLE IF NOT EXISTS ops.forecast_accuracy_day (
    accuracy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of_date date NOT NULL,
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    forecast_run_id uuid,
    forecast_total numeric(14, 2),
    actual_total numeric(14, 2),
    error_total numeric(14, 2),
    mape numeric(12, 6),
    bias numeric(14, 2),
    rmse numeric(14, 2),
    accuracy numeric(12, 6),
    per_insurance jsonb NOT NULL DEFAULT '[]'::jsonb,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (as_of_date)
);

CREATE INDEX IF NOT EXISTS ix_forecast_accuracy_as_of
    ON ops.forecast_accuracy_day (as_of_date DESC);
