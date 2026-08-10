#!/usr/bin/env bash
set -euo pipefail
OUT_DIR="${OUT_DIR:-/home/abdu/sf_eval}"
RUN="${RUN:-fd2d3697-e407-49f7-b7e2-76ce48fc6be2}"
mkdir -p "$OUT_DIR"

echo "[export] checks"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT btrim(check_eft_num) AS check_eft_num
  FROM billing.eob_check
  WHERE check_eft_num IS NOT NULL AND btrim(check_eft_num) <> ''
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_checks.csv"

echo "[export] visits"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT webpt_patient_id, patient_name, date_of_service::text, facility_name,
         visit_status, primary_check_number, secondary_check_number,
         total_paid::text, matched_paid::text, pending_lines::text, paid_lines::text,
         COALESCE(pending_reason,'') AS pending_reason
  FROM billing.reconciliation_visit_agg
  WHERE reconciliation_run_id = '${RUN}'::uuid
    AND date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_visits_2026.csv" || \
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT webpt_patient_id, patient_name, date_of_service::text, facility_name,
         visit_status, primary_check_number, secondary_check_number,
         total_paid::text, matched_paid::text, pending_lines::text, paid_lines::text
  FROM billing.reconciliation_visit_agg
  WHERE reconciliation_run_id = '${RUN}'::uuid
    AND date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_visits_2026.csv"

echo "[export] lines"
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

wc -l "${OUT_DIR}/our_checks.csv" "${OUT_DIR}/our_visits_2026.csv" "${OUT_DIR}/our_lines_2026.csv"
echo "RUN=${RUN}"
