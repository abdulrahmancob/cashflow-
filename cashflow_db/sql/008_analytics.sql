-- Versioned forecast predictions
CREATE TABLE IF NOT EXISTS analytics.forecast_run (
    forecast_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    algorithm_version text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    as_of_date date NOT NULL,
    status text NOT NULL DEFAULT 'success'
        CHECK (status IN ('running', 'success', 'failed')),
    etl_run_id uuid REFERENCES etl.etl_run (etl_run_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analytics.forecast_prediction (
    prediction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    forecast_run_id uuid NOT NULL REFERENCES analytics.forecast_run (forecast_run_id),
    claim_line_id uuid REFERENCES billing.claim_line (claim_line_id),
    visit_id uuid REFERENCES core.visit (visit_id),
    outcome_stage text
        CHECK (outcome_stage IS NULL OR outcome_stage IN (
            'paid', 'on_track', 'overdue', 'rejected', 'denied', 'zero_pay'
        )),
    expected_amount numeric(14, 2),
    expected_pay_date date,
    overdue_days int,
    denied_amount numeric(14, 2),
    denial_category text,
    sla_lag_days int,
    forecast_shift_days int,
    risk_flags jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk_score numeric(8, 4)
);

CREATE INDEX IF NOT EXISTS ix_forecast_pred_run ON analytics.forecast_prediction (forecast_run_id);
CREATE INDEX IF NOT EXISTS ix_forecast_pred_line ON analytics.forecast_prediction (claim_line_id);
