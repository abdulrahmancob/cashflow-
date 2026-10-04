-- One row per eligibility work item for sheet filters.
-- collection_status is Snowflake, then context. Manual overrides stay on the work item
-- and are applied at query time, so a cell edit filters correctly before the next refresh.
-- primary_check_date is the latest successful recon row for the same EMR and DOS,
-- preferring the work item's facility.
-- Populated by migrate. Generate and nightly refresh it. List filters only read it.
-- Idempotent: CREATE ... IF NOT EXISTS.

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.eligibility_sheet_facet AS
SELECT
    wi.work_item_id,
    COALESCE(
        sf.collection_status,
        NULLIF(btrim(wi.context->>'collection_status'), '')
    ) AS collection_status,
    rv.primary_check_date
FROM ops.eligibility_work_item wi
LEFT JOIN LATERAL (
    SELECT COALESCE(
        NULLIF(btrim(sf.payload->>'COLLECTION_STATUS'), ''),
        NULLIF(btrim(sf.payload->>'collection_status'), '')
    ) AS collection_status
    FROM analytics.snowflake_visit_kpi sf
    WHERE sf.emr_id = wi.emr_patient_id
      AND sf.date_of_service = wi.dos
    LIMIT 1
) sf ON true
LEFT JOIN LATERAL (
    SELECT rv.primary_check_date
    FROM billing.reconciliation_visit_agg rv
    WHERE rv.reconciliation_run_id = (
        SELECT reconciliation_run_id
        FROM billing.reconciliation_run
        WHERE status = 'success'
        ORDER BY created_at DESC
        LIMIT 1
    )
      AND rv.webpt_patient_id = wi.emr_patient_id
      AND rv.date_of_service = wi.dos
    ORDER BY CASE
        WHEN lower(btrim(COALESCE(rv.facility_name, '')))
           = lower(btrim(COALESCE(wi.facility_name, ''))) THEN 0
        ELSE 1
    END,
    rv.primary_check_date DESC NULLS LAST
    LIMIT 1
) rv ON true
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS uq_eligibility_sheet_facet
    ON analytics.eligibility_sheet_facet (work_item_id);

CREATE INDEX IF NOT EXISTS ix_eligibility_sheet_facet_status
    ON analytics.eligibility_sheet_facet (lower(btrim(COALESCE(collection_status, ''))));

CREATE INDEX IF NOT EXISTS ix_eligibility_sheet_facet_check_date
    ON analytics.eligibility_sheet_facet (primary_check_date);
