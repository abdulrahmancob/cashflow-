-- Platform Production Engineering: monitoring schema, feature store, dataset versioning.

CREATE SCHEMA IF NOT EXISTS monitoring;

-- ---------------------------------------------------------------------------
-- Typed pipeline metrics
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.pipeline_metric (
    metric_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE CASCADE,
    stage_key text,
    metric_key text NOT NULL,
    metric_type text NOT NULL DEFAULT 'count'
        CHECK (metric_type IN (
            'duration', 'count', 'money', 'quality', 'performance', 'business', 'system'
        )),
    entity_key text,
    value_num numeric,
    value_text text,
    unit text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_metric_run
    ON monitoring.pipeline_metric (run_id, metric_key);

CREATE INDEX IF NOT EXISTS ix_pipeline_metric_type
    ON monitoring.pipeline_metric (metric_type, recorded_at DESC);

CREATE INDEX IF NOT EXISTS ix_pipeline_metric_entity
    ON monitoring.pipeline_metric (entity_key)
    WHERE entity_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Quality history (status precomputed)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.quality_metric (
    quality_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of_date date NOT NULL,
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    metric_key text NOT NULL,
    value_num numeric,
    expected_value text,
    threshold text,
    status text NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'warning', 'critical')),
    dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_quality_metric_as_of
    ON monitoring.quality_metric (as_of_date DESC, metric_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_quality_metric_run_key
    ON monitoring.quality_metric (run_id, metric_key)
    WHERE run_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Quality rules (thresholds for status evaluation)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.quality_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_key text NOT NULL UNIQUE,
    expected_value text,
    threshold text NOT NULL,
    comparison text NOT NULL DEFAULT 'lte'
        CHECK (comparison IN ('lt', 'lte', 'gt', 'gte', 'eq')),
    warning_threshold text,
    critical_threshold text,
    enabled boolean NOT NULL DEFAULT true,
    notes text
);

-- ---------------------------------------------------------------------------
-- Job runtime (+ queue wait for future workers)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.job_runtime (
    runtime_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES ops.pipeline_run (run_id) ON DELETE CASCADE,
    stage_key text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    duration_sec numeric(14, 3),
    queue_wait_seconds numeric(14, 3) NOT NULL DEFAULT 0,
    sla_sec int,
    sla_breached boolean NOT NULL DEFAULT false,
    UNIQUE (run_id, stage_key)
);

CREATE INDEX IF NOT EXISTS ix_job_runtime_run
    ON monitoring.job_runtime (run_id);

-- ---------------------------------------------------------------------------
-- System health probes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.system_health (
    health_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    checked_at timestamptz NOT NULL DEFAULT now(),
    system_key text NOT NULL,
    probe_name text NOT NULL DEFAULT 'default',
    status text NOT NULL
        CHECK (status IN ('up', 'down', 'degraded', 'maintenance', 'unknown')),
    response_ms numeric(14, 3),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_system_health_system
    ON monitoring.system_health (system_key, checked_at DESC);

-- ---------------------------------------------------------------------------
-- Event log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.pipeline_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE CASCADE,
    stage_key text,
    event_key text NOT NULL,
    severity text NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    message text,
    entity_key text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_event_run
    ON monitoring.pipeline_event (run_id, created_at);

CREATE INDEX IF NOT EXISTS ix_pipeline_event_key
    ON monitoring.pipeline_event (event_key, created_at DESC);

-- ---------------------------------------------------------------------------
-- SLA definitions (DB-managed)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.sla_definition (
    sla_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type text NOT NULL
        CHECK (scope_type IN ('stage', 'facility', 'system')),
    scope_key text NOT NULL,
    max_seconds int NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    notes text,
    UNIQUE (scope_type, scope_key)
);

-- ---------------------------------------------------------------------------
-- System config / maintenance (DB-managed)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.system_config (
    system_key text PRIMARY KEY,
    mode text NOT NULL DEFAULT 'auto'
        CHECK (mode IN ('auto', 'force_up', 'maintenance')),
    probe_name text NOT NULL DEFAULT 'default',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    notes text
);

-- ---------------------------------------------------------------------------
-- Backfill control plane
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring.backfill_run (
    backfill_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_date date NOT NULL,
    to_date date NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'partial', 'cancelled'
        )),
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS monitoring.backfill_day (
    backfill_day_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backfill_id uuid NOT NULL
        REFERENCES monitoring.backfill_run (backfill_id) ON DELETE CASCADE,
    as_of_date date NOT NULL,
    pipeline_run_id uuid REFERENCES ops.pipeline_run (run_id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'running', 'success', 'failed', 'skipped'
        )),
    attempt int NOT NULL DEFAULT 0,
    error_message text,
    UNIQUE (backfill_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS ix_backfill_day_status
    ON monitoring.backfill_day (backfill_id, status, as_of_date);

-- ---------------------------------------------------------------------------
-- Feature store (analytics)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.feature_definition (
    feature_key text PRIMARY KEY,
    description text,
    grain text,
    owner text,
    version text NOT NULL DEFAULT '1',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analytics.feature_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    as_of_date date NOT NULL,
    feature_key text NOT NULL
        REFERENCES analytics.feature_definition (feature_key),
    entity_key text NOT NULL DEFAULT '',
    value_num numeric,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    dataset_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (as_of_date, feature_key, entity_key, dataset_version)
);

CREATE INDEX IF NOT EXISTS ix_feature_snapshot_as_of
    ON analytics.feature_snapshot (as_of_date, feature_key);

CREATE INDEX IF NOT EXISTS ix_feature_snapshot_dataset
    ON analytics.feature_snapshot (dataset_version)
    WHERE dataset_version IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Dataset version columns on existing control / lineage tables
-- ---------------------------------------------------------------------------
ALTER TABLE ops.pipeline_run
    ADD COLUMN IF NOT EXISTS dataset_version text;

ALTER TABLE ops.daily_snapshot
    ADD COLUMN IF NOT EXISTS dataset_version text;

ALTER TABLE billing.reconciliation_run
    ADD COLUMN IF NOT EXISTS dataset_version text;

ALTER TABLE analytics.forecast_run
    ADD COLUMN IF NOT EXISTS dataset_version text;

-- ---------------------------------------------------------------------------
-- Seeds: SLA defaults, system config, quality rules, feature definitions
-- ---------------------------------------------------------------------------
INSERT INTO monitoring.sla_definition (scope_type, scope_key, max_seconds, notes)
VALUES
    ('stage', 'acquire', 1200, '20 min'),
    ('stage', 'validate_sources', 120, '2 min'),
    ('stage', 'enrich_clinical', 7200, '2 h'),
    ('stage', 'load_warehouse', 3600, '1 h'),
    ('stage', 'reconciliation', 1800, '30 min'),
    ('stage', 'analytics', 600, '10 min'),
    ('stage', 'feature_store', 600, '10 min'),
    ('stage', 'forecast', 300, '5 min'),
    ('stage', 'publish_monitor', 300, '5 min')
ON CONFLICT (scope_type, scope_key) DO NOTHING;

INSERT INTO monitoring.system_config (system_key, mode, probe_name, notes)
VALUES
    ('webpt', 'auto', 'session_file', 'WebPT scraper'),
    ('revflow', 'auto', 'output_dir', 'RevFlow EOB exporter'),
    ('waystar', 'auto', 'output_dir', 'Waystar rejections/denials'),
    ('snowflake', 'auto', 'module', 'Snowflake KPI pull'),
    ('postgres', 'auto', 'connect', 'Warehouse')
ON CONFLICT (system_key) DO NOTHING;

INSERT INTO monitoring.quality_rule (
    metric_key, expected_value, threshold, comparison,
    warning_threshold, critical_threshold, notes
)
VALUES
    ('notes_missing', '<10', '10', 'lt', '10', '50', 'Missing clinical notes'),
    ('cpt_missing', '<10', '10', 'lt', '10', '50', 'Missing CPT extracts'),
    ('schedule_rows', '>0', '0', 'gt', NULL, '0', 'Schedule must be non-empty'),
    ('revflow_files', '>0', '0', 'gt', NULL, '0', 'RevFlow exports required'),
    ('ocr_success_pct', '>=90', '90', 'gte', '90', '70', 'OCR success rate'),
    ('payments_rows', '>0', '0', 'gt', NULL, NULL, 'Patient payments present')
ON CONFLICT (metric_key) DO NOTHING;

INSERT INTO analytics.feature_definition (feature_key, description, grain, owner, version)
VALUES
    ('payor.avg_cash_velocity_days', 'Median/avg payor cash velocity days', 'payor', 'analytics', '1'),
    ('facility.cancellation_rate', 'Facility appointment cancellation rate', 'facility', 'analytics', '1'),
    ('case.authorization_risk', 'Auth remaining vs upcoming visits risk score', 'case', 'analytics', '1')
ON CONFLICT (feature_key) DO NOTHING;
