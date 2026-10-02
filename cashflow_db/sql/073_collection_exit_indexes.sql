-- Forecast lookups used by the collection SLA check.
-- Idempotent: CREATE INDEX IF NOT EXISTS.
-- migrate() runs inside a transaction, so this cannot use CONCURRENTLY.

CREATE OR REPLACE FUNCTION analytics.forecast_payload_dos(payload jsonb, service_date date)
RETURNS date
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        service_date,
        CASE
            WHEN payload->>'date_of_service' ~ '^\d{4}-\d{2}-\d{2}'
                THEN substring(payload->>'date_of_service' from 1 for 10)::date
            ELSE NULL::date
        END
    );
$$;

CREATE INDEX IF NOT EXISTS ix_forecast_pred_run_emr_dos
    ON analytics.forecast_prediction (
        forecast_run_id, webpt_patient_id, date_of_service
    );

CREATE INDEX IF NOT EXISTS ix_forecast_pred_payload_emr_dos
    ON analytics.forecast_prediction (
        forecast_run_id,
        (NULLIF(BTRIM(payload->>'webpt_patient_id'), '')),
        (analytics.forecast_payload_dos(payload, date_of_service))
    )
    WHERE webpt_patient_id IS NULL;
