-- Purge all pre-2026 data (bad loads). Run once after a fresh pg_dump backup.
-- Derived analytics (forecast, reconciliation) are rebuilt by the nightly
-- pipeline, so pre-2026 rows there are deleted outright.

BEGIN;

CREATE TEMP TABLE old_visits AS
SELECT visit_id FROM core.visit WHERE service_date < DATE '2026-01-01';

CREATE TEMP TABLE old_notes AS
SELECT note_id FROM core.clinical_note
WHERE visit_id IN (SELECT visit_id FROM old_visits);

CREATE TEMP TABLE old_claim_lines AS
SELECT claim_line_id FROM billing.claim_line
WHERE visit_id IN (SELECT visit_id FROM old_visits);

-- 1) Forecast outputs: full wipe (single derived run; regenerated nightly)
DELETE FROM analytics.forecast_prediction;
DELETE FROM analytics.forecast_feature;
DELETE FROM analytics.forecast_run;

-- 2) Reconciliation spine rows tied to pre-2026 service dates / visits
DELETE FROM billing.reconciliation_line
WHERE date_of_service < DATE '2026-01-01'
   OR visit_id IN (SELECT visit_id FROM old_visits)
   OR eob_line_id IN (
        SELECT eob_line_id FROM billing.eob_line
        WHERE date_of_service < DATE '2026-01-01'
   );
DELETE FROM billing.reconciliation_visit_agg
WHERE date_of_service < DATE '2026-01-01';

-- 3) EOB lines with pre-2026 DOS (checks themselves are all 2026-dated)
DELETE FROM billing.eob_line
WHERE date_of_service < DATE '2026-01-01'
   OR claim_line_id IN (SELECT claim_line_id FROM old_claim_lines);

-- 4) Claim graph attached to pre-2026 visits
DELETE FROM billing.claim_event
WHERE claim_line_id IN (SELECT claim_line_id FROM old_claim_lines);
DELETE FROM billing.denial_record
WHERE claim_line_id IN (SELECT claim_line_id FROM old_claim_lines);
DELETE FROM billing.claim_line
WHERE claim_line_id IN (SELECT claim_line_id FROM old_claim_lines);

CREATE TEMP TABLE empty_claims AS
SELECT c.claim_id FROM billing.claim c
WHERE NOT EXISTS (
    SELECT 1 FROM billing.claim_line cl WHERE cl.claim_id = c.claim_id
);
DELETE FROM billing.claim_event WHERE claim_id IN (SELECT claim_id FROM empty_claims);
DELETE FROM billing.denial_record WHERE claim_id IN (SELECT claim_id FROM empty_claims);
UPDATE billing.claim SET parent_claim_id = NULL
WHERE parent_claim_id IN (SELECT claim_id FROM empty_claims);
DELETE FROM billing.claim WHERE claim_id IN (SELECT claim_id FROM empty_claims);

-- 5) Visit children, then visits
DELETE FROM billing.audit_finding
WHERE visit_id IN (SELECT visit_id FROM old_visits)
   OR note_id IN (SELECT note_id FROM old_notes);
DELETE FROM ops.note_issue
WHERE visit_id IN (SELECT visit_id FROM old_visits)
   OR note_id IN (SELECT note_id FROM old_notes);
DELETE FROM ops.outreach_event WHERE visit_id IN (SELECT visit_id FROM old_visits);
DELETE FROM core.authorization_visit WHERE visit_id IN (SELECT visit_id FROM old_visits);
DELETE FROM billing.patient_payment WHERE visit_id IN (SELECT visit_id FROM old_visits);
UPDATE core.schedule_appointment SET visit_id = NULL
WHERE visit_id IN (SELECT visit_id FROM old_visits);
DELETE FROM core.clinical_note WHERE visit_id IN (SELECT visit_id FROM old_visits);
DELETE FROM core.visit_service_line WHERE visit_id IN (SELECT visit_id FROM old_visits);
DELETE FROM core.visit WHERE visit_id IN (SELECT visit_id FROM old_visits);

-- 6) Pre-2026 plans of care (bad extractions; Aug forward unaffected)
DELETE FROM docs.plan_of_care_detail
WHERE date_of_plan_of_care < DATE '2026-01-01';

-- Report what remains
SELECT 'visits_pre_2026' AS check, count(*) FROM core.visit WHERE service_date < DATE '2026-01-01'
UNION ALL
SELECT 'eob_lines_pre_2026', count(*) FROM billing.eob_line WHERE date_of_service < DATE '2026-01-01'
UNION ALL
SELECT 'recon_lines_pre_2026', count(*) FROM billing.reconciliation_line WHERE date_of_service < DATE '2026-01-01'
UNION ALL
SELECT 'poc_pre_2026', count(*) FROM docs.plan_of_care_detail WHERE date_of_plan_of_care < DATE '2026-01-01'
UNION ALL
SELECT 'visits_total_remaining', count(*) FROM core.visit;

COMMIT;
