-- Precomputed collection date per Snowflake visit.
-- Order: tracker txn_date, Waystar trans_date, eligibility sheet date, Snowflake primary_check_date.
-- Populated by migrate. Nightly refreshes it. The page only reads it.

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.billing_collect_visit AS
WITH visits AS (
    SELECT btrim(kpi.emr_id) AS emr_id,
           kpi.date_of_service,
           kpi.clinic,
           kpi.primary_check_date,
           COALESCE(kpi.insurance_payment, 0) + COALESCE(kpi.client_payment, 0) AS amount,
           (SELECT CASE WHEN c ~ '^[0-9]+$'
                   THEN COALESCE(NULLIF(ltrim(c, '0'), ''), '0') ELSE c END
            FROM (SELECT regexp_replace(regexp_replace(upper(btrim(COALESCE(kpi.primary_check_number, ''))), '\.0+$', ''), '[^A-Z0-9]', '', 'g') AS c) compacted) AS sf_ref
    FROM analytics.snowflake_visit_kpi kpi
),
tracker_min AS (
    SELECT ref, MIN(txn_date) AS txn_date
    FROM (
        SELECT (SELECT CASE WHEN c ~ '^[0-9]+$'
                       THEN COALESCE(NULLIF(ltrim(c, '0'), ''), '0') ELSE c END
                FROM (SELECT regexp_replace(regexp_replace(upper(btrim(COALESCE(ref_raw, ''))), '\.0+$', ''), '[^A-Z0-9]', '', 'g') AS c) compacted) AS ref,
               t.txn_date
        FROM billing.transaction_tracker_row t
        CROSS JOIN LATERAL (
            VALUES (t.eft_1), (t.eft_2), (t.check_reference)
        ) AS refs(ref_raw)
        WHERE t.deleted_at IS NULL
          AND t.txn_date IS NOT NULL
    ) compact_refs
    WHERE ref <> ''
    GROUP BY ref
),
waystar_base AS (
    SELECT m.webpt_patient_id AS emr_id,
           c.from_date AS date_of_service,
           c.trans_date,
           c.total_remit_amount,
           c.remit_numbers
    FROM billing.waystar_claim c
    JOIN billing.waystar_webpt_map m
      ON m.waystar_claim_key = c.claim_key
    WHERE COALESCE(m.webpt_patient_id, '') <> ''
      AND c.from_date IS NOT NULL
),
waystar AS (
    SELECT emr_id,
           date_of_service,
           MIN(trans_date) AS trans_date
    FROM waystar_base
    WHERE COALESCE(total_remit_amount, 0) > 0
      AND trans_date IS NOT NULL
    GROUP BY emr_id, date_of_service
),
ws_tracker AS (
    SELECT refs.emr_id, refs.date_of_service, MIN(t.txn_date) AS txn_date
    FROM (
        SELECT w.emr_id,
               w.date_of_service,
               (SELECT CASE WHEN c ~ '^[0-9]+$'
                       THEN COALESCE(NULLIF(ltrim(c, '0'), ''), '0') ELSE c END
                FROM (SELECT regexp_replace(regexp_replace(upper(btrim(COALESCE(num, ''))), '\.0+$', ''), '[^A-Z0-9]', '', 'g') AS c) compacted) AS ref
        FROM waystar_base w
        CROSS JOIN LATERAL unnest(COALESCE(w.remit_numbers, ARRAY[]::text[])) AS num
    ) refs
    JOIN tracker_min t ON t.ref = refs.ref AND refs.ref <> ''
    GROUP BY refs.emr_id, refs.date_of_service
),
elig_base AS (
    SELECT wi.emr_patient_id AS emr_id,
           wi.dos AS date_of_service,
           COALESCE(
               CASE WHEN btrim(COALESCE(wi.manual_overrides->>'check_date', '')) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                    THEN substring(btrim(wi.manual_overrides->>'check_date') from 1 for 10)::date END,
               CASE WHEN btrim(COALESCE(wi.manual_overrides->>'insurance_check_date', '')) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                    THEN substring(btrim(wi.manual_overrides->>'insurance_check_date') from 1 for 10)::date END
           ) AS check_date,
           wi.manual_overrides
    FROM ops.eligibility_work_item wi
    WHERE wi.dos IS NOT NULL
      AND COALESCE(wi.emr_patient_id, '') <> ''
),
elig AS (
    SELECT emr_id, date_of_service, MIN(check_date) AS check_date
    FROM elig_base
    WHERE check_date IS NOT NULL
    GROUP BY emr_id, date_of_service
),
el_tracker AS (
    SELECT refs.emr_id, refs.date_of_service, MIN(t.txn_date) AS txn_date
    FROM (
        SELECT e.emr_id,
               e.date_of_service,
               (SELECT CASE WHEN c ~ '^[0-9]+$'
                       THEN COALESCE(NULLIF(ltrim(c, '0'), ''), '0') ELSE c END
                FROM (SELECT regexp_replace(regexp_replace(upper(btrim(COALESCE(num, ''))), '\.0+$', ''), '[^A-Z0-9]', '', 'g') AS c) compacted) AS ref
        FROM elig_base e
        CROSS JOIN LATERAL (
            VALUES
                (e.manual_overrides->>'check_number'),
                (e.manual_overrides->>'insurance_check_number')
        ) AS nums(num)
    ) refs
    JOIN tracker_min t ON t.ref = refs.ref AND refs.ref <> ''
    GROUP BY refs.emr_id, refs.date_of_service
),
sf_tracker AS (
    SELECT v.emr_id, v.date_of_service, MIN(t.txn_date) AS txn_date
    FROM visits v
    JOIN tracker_min t ON t.ref = v.sf_ref AND v.sf_ref <> ''
    GROUP BY v.emr_id, v.date_of_service
)
SELECT v.emr_id,
       v.date_of_service,
       v.clinic,
       v.amount,
       COALESCE(
           sf_tracker.txn_date,
           ws_tracker.txn_date,
           el_tracker.txn_date,
           waystar.trans_date,
           elig.check_date,
           v.primary_check_date
       ) AS collect_date
FROM visits v
LEFT JOIN sf_tracker
  ON sf_tracker.emr_id = v.emr_id
 AND sf_tracker.date_of_service = v.date_of_service
LEFT JOIN ws_tracker
  ON ws_tracker.emr_id = v.emr_id
 AND ws_tracker.date_of_service = v.date_of_service
LEFT JOIN el_tracker
  ON el_tracker.emr_id = v.emr_id
 AND el_tracker.date_of_service = v.date_of_service
LEFT JOIN waystar
  ON waystar.emr_id = v.emr_id
 AND waystar.date_of_service = v.date_of_service
LEFT JOIN elig
  ON elig.emr_id = v.emr_id
 AND elig.date_of_service = v.date_of_service
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_collect_visit
    ON analytics.billing_collect_visit (emr_id, date_of_service);

CREATE INDEX IF NOT EXISTS ix_billing_collect_visit_dos
    ON analytics.billing_collect_visit (date_of_service);
