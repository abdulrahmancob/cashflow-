-- Anti-duplication views / marts replacing CSV rollups
-- Drop existing mart views so CREATE OR REPLACE can change column shapes idempotently.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT schemaname, viewname
    FROM pg_views
    WHERE schemaname = 'mart'
  LOOP
    EXECUTE format('DROP VIEW IF EXISTS %I.%I CASCADE', r.schemaname, r.viewname);
  END LOOP;
END $$;

CREATE OR REPLACE VIEW mart.v_unmatched_webpt AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    cov.raw_insurance_name AS ins_name,
    v.service_date AS date_of_service,
    sl.cpt_code,
    sl.modifiers,
    'no_eob_line'::text AS reason
FROM core.visit_service_line sl
JOIN core.visit v ON v.visit_id = sl.visit_id
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN core.patient_coverage cov
    ON cov.patient_id = p.patient_id AND cov.is_primary
LEFT JOIN billing.claim_line cl ON cl.service_line_id = sl.service_line_id
LEFT JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
WHERE el.eob_line_id IS NULL;

CREATE OR REPLACE VIEW mart.v_unmatched_payments AS
SELECT
    el.revflow_patient_id,
    el.date_of_service,
    el.cpt_code,
    el.modifiers,
    el.paid_amount,
    ec.payor_raw AS payor,
    ec.check_eft_num,
    ec.eob_date,
    ec.source_file,
    'no_claim_line'::text AS reason
FROM billing.eob_line el
JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
WHERE el.claim_line_id IS NULL;

CREATE OR REPLACE VIEW mart.v_reconciliation_visits AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    f.name AS facility_name,
    v.service_date AS date_of_service,
    COUNT(DISTINCT sl.service_line_id) AS total_billed_cpts,
    COALESCE(SUM(el.paid_amount), 0) AS total_paid,
    COALESCE(SUM(el.paid_amount) FILTER (WHERE el.claim_line_id IS NOT NULL), 0) AS matched_paid,
    COUNT(DISTINCT cl.claim_line_id) FILTER (
        WHERE cl.claim_line_id IS NOT NULL AND el.eob_line_id IS NULL
    ) AS pending_lines,
    COUNT(DISTINCT el.eob_line_id) AS paid_lines
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
LEFT JOIN core.visit_service_line sl ON sl.visit_id = v.visit_id
LEFT JOIN billing.claim_line cl ON cl.visit_id = v.visit_id
LEFT JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
GROUP BY p.webpt_patient_id, ph.patient_name, ph.dob, f.name, v.service_date;

CREATE OR REPLACE VIEW mart.v_reconciliation_patients AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    ph.dob,
    f.name AS facility_name,
    pc.webpt_case_id AS case_id,
    cov.raw_insurance_name AS ins_name,
    pc.assigned_therapist,
    (
        SELECT a.visits_authorized
        FROM core.authorization a
        WHERE a.case_pk = pc.case_pk
        ORDER BY a.end_date DESC NULLS LAST
        LIMIT 1
    ) AS auth_ins_visits,
    (
        SELECT COUNT(*) FROM core.visit v WHERE v.patient_id = p.patient_id
    ) AS visits_total,
    (
        SELECT COALESCE(SUM(el.paid_amount), 0)
        FROM billing.claim c
        JOIN billing.claim_line cl ON cl.claim_id = c.claim_id
        JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
        WHERE c.patient_id = p.patient_id
    ) AS total_paid
FROM core.patient p
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN LATERAL (
    SELECT *
    FROM core.patient_case pc2
    WHERE pc2.patient_id = p.patient_id
    ORDER BY pc2.opened_at DESC NULLS LAST
    LIMIT 1
) pc ON true
LEFT JOIN ref.facility f ON f.facility_id = pc.facility_id
LEFT JOIN core.patient_coverage cov
    ON cov.patient_id = p.patient_id AND cov.is_primary;

CREATE OR REPLACE VIEW mart.v_payor_behavior_summary AS
SELECT
    ec.payor_raw AS payor,
    COUNT(DISTINCT ec.eob_check_id) AS n_checks,
    COUNT(DISTINCT a.allocation_id) AS n_with_deposit,
    CASE
        WHEN COUNT(DISTINCT ec.eob_check_id) = 0 THEN 0::numeric
        ELSE ROUND(
            100.0 * COUNT(DISTINCT a.allocation_id)
            / COUNT(DISTINCT ec.eob_check_id),
            2
        )
    END AS deposit_coverage_pct,
    COALESCE(SUM(ec.paid_amount_sum), 0) AS paid_amount_sum,
    AVG(ec.paid_amount_sum) AS avg_paid_per_check
FROM billing.eob_check ec
LEFT JOIN billing.deposit_check_allocation a ON a.eob_check_id = ec.eob_check_id
GROUP BY ec.payor_raw;

CREATE OR REPLACE VIEW mart.v_actual_cash_daily AS
SELECT
    bd.bank_posting_date AS period,
    COALESCE(SUM(bd.amount), 0) AS amount,
    COUNT(*)::int AS line_count
FROM billing.bank_deposit bd
WHERE bd.bank_posting_date IS NOT NULL
GROUP BY bd.bank_posting_date;

CREATE OR REPLACE VIEW mart.v_projected_cash_daily AS
SELECT
    fp.expected_pay_date AS period,
    COALESCE(SUM(fp.expected_amount), 0) AS amount,
    COUNT(*)::int AS line_count,
    fr.forecast_run_id,
    fr.algorithm_version
FROM analytics.forecast_prediction fp
JOIN analytics.forecast_run fr ON fr.forecast_run_id = fp.forecast_run_id
WHERE fp.expected_pay_date IS NOT NULL
  AND fr.created_at = (
      SELECT MAX(created_at)
      FROM analytics.forecast_run
      WHERE status = 'success'
  )
GROUP BY fp.expected_pay_date, fr.forecast_run_id, fr.algorithm_version;

CREATE OR REPLACE VIEW mart.v_outcome_stages_latest AS
SELECT
    p.webpt_patient_id,
    ph.patient_name,
    p.name_key,
    ph.dob,
    f.name AS facility_name,
    cov.raw_insurance_name AS ins_name,
    v.service_date AS date_of_service,
    cl.cpt_code,
    cl.modifiers,
    c.status_current AS reconcile_status,
    el.paid_amount,
    el.allowed_amount,
    ec.eob_date,
    fp.outcome_stage,
    fp.expected_amount,
    fp.expected_pay_date,
    fr.as_of_date AS forecast_date,
    fp.overdue_days,
    fp.denied_amount,
    fp.denial_category,
    fp.sla_lag_days,
    fp.forecast_shift_days,
    cl.units,
    fr.algorithm_version AS source
FROM analytics.forecast_prediction fp
JOIN analytics.forecast_run fr ON fr.forecast_run_id = fp.forecast_run_id
LEFT JOIN billing.claim_line cl ON cl.claim_line_id = fp.claim_line_id
LEFT JOIN billing.claim c ON c.claim_id = cl.claim_id
LEFT JOIN core.visit v ON v.visit_id = COALESCE(fp.visit_id, cl.visit_id)
LEFT JOIN core.patient p ON p.patient_id = COALESCE(c.patient_id, v.patient_id)
LEFT JOIN core.patient_history ph
    ON ph.patient_id = p.patient_id AND ph.is_current
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id
LEFT JOIN core.patient_coverage cov
    ON cov.patient_id = p.patient_id AND cov.is_primary
LEFT JOIN LATERAL (
    SELECT el2.*
    FROM billing.eob_line el2
    WHERE el2.claim_line_id = cl.claim_line_id
    ORDER BY el2.paid_amount DESC NULLS LAST
    LIMIT 1
) el ON true
LEFT JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
WHERE fr.created_at = (
    SELECT MAX(created_at)
    FROM analytics.forecast_run
    WHERE status = 'success'
);

-- Snowflake dedupe keys: flag duplicated (name_key, DOS) rows
CREATE OR REPLACE VIEW mart.v_snowflake_visit_dedupe_keys AS
SELECT
    p.name_key,
    v.service_date AS date_of_service,
    f.name AS facility_name,
    p.webpt_patient_id,
    v.visit_id,
    COUNT(*) OVER (PARTITION BY p.name_key, v.service_date) AS name_dos_dup_count
FROM core.visit v
JOIN core.patient p ON p.patient_id = v.patient_id
LEFT JOIN ref.facility f ON f.facility_id = v.facility_id;
