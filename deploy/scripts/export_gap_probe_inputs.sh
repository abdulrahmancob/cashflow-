#!/usr/bin/env bash
# Export Postgres facts needed for true-match / coverage gap probes.
set -euo pipefail
OUT_DIR="${OUT_DIR:-/home/abdu/sf_eval}"
RUN="${RUN:-aae72074-5b88-438e-b238-960dd08208a3}"
mkdir -p "$OUT_DIR"

echo "[export] eob payments 2026+"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT
    el.date_of_service::text AS date_of_service,
    COALESCE(el.cpt_code,'') AS cpt_code,
    COALESCE(el.modifiers,'') AS modifiers,
    COALESCE(el.paid_amount::text,'0') AS paid_amount,
    COALESCE(btrim(ec.check_eft_num),'') AS check_eft_num,
    COALESCE(p.webpt_patient_id,'') AS webpt_patient_id,
    COALESCE(p.name_key,'') AS name_key
  FROM billing.eob_line el
  JOIN billing.eob_check ec ON ec.eob_check_id = el.eob_check_id
  LEFT JOIN core.patient p ON p.patient_id = el.patient_id
  WHERE el.date_of_service IS NULL OR el.date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/eob_payments_2026.csv"

echo "[export] tracked refs (bank_deposit)"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT x AS eft_ref FROM (
    SELECT btrim(eft_1) AS x FROM billing.bank_deposit WHERE eft_1 IS NOT NULL AND btrim(eft_1) <> ''
    UNION ALL
    SELECT btrim(eft_2) FROM billing.bank_deposit WHERE eft_2 IS NOT NULL AND btrim(eft_2) <> ''
    UNION ALL
    SELECT btrim(eft_last4) FROM billing.bank_deposit WHERE eft_last4 IS NOT NULL AND btrim(eft_last4) <> ''
  ) s
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/tracked_refs.csv"

echo "[export] clinical visits 2026+"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT p.webpt_patient_id AS emr_id, v.service_date::text AS dos
  FROM core.visit v
  JOIN core.patient p ON p.patient_id = v.patient_id
  WHERE v.service_date >= DATE '2026-01-01'
    AND p.webpt_patient_id IS NOT NULL
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/clinical_visits_2026.csv"

echo "[export] clinical notes 2026+"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT p.webpt_patient_id AS emr_id, COALESCE(cn.note_date, v.service_date)::text AS dos
  FROM core.clinical_note cn
  JOIN core.visit v ON v.visit_id = cn.visit_id
  JOIN core.patient p ON p.patient_id = v.patient_id
  WHERE COALESCE(cn.note_date, v.service_date) >= DATE '2026-01-01'
    AND p.webpt_patient_id IS NOT NULL
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/clinical_notes_2026.csv"

echo "[export] schedule 2026+"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT p.webpt_patient_id AS emr_id, sa.service_date::text AS dos
  FROM core.schedule_appointment sa
  JOIN core.patient p ON p.patient_id = sa.patient_id
  WHERE sa.service_date >= DATE '2026-01-01'
    AND p.webpt_patient_id IS NOT NULL
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/schedule_2026.csv"

echo "[export] patients"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT webpt_patient_id FROM core.patient WHERE webpt_patient_id IS NOT NULL AND btrim(webpt_patient_id) <> ''
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/patients_emr.csv"

echo "[export] recon lines (with modifier)"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT webpt_patient_id, patient_name, date_of_service::text, cpt_code, status,
         COALESCE(match_level,'') AS match_level,
         COALESCE(check_eft_num,'') AS check_eft_num,
         COALESCE(modifier,'') AS modifier,
         COALESCE(paid_amount::text,'') AS paid_amount
  FROM billing.reconciliation_line
  WHERE reconciliation_run_id = '${RUN}'::uuid
    AND date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_lines_2026.csv"

wc -l "${OUT_DIR}"/*.csv
echo "OUT_DIR=${OUT_DIR}"
