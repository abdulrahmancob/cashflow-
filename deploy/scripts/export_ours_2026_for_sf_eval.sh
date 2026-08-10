#!/usr/bin/env bash
# Export our checks + recon visits/lines (DOS>=2026-01-01) for SF paid+check eval.
set -euo pipefail
RUN="${1:-aae72074-5b88-438e-b238-960dd08208a3}"
OUT_DIR="${OUT_DIR:-/data/exports/ops/sf_eval}"
mkdir -p "$OUT_DIR"

docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT btrim(check_eft_num) AS check_eft_num
  FROM billing.eob_check
  WHERE check_eft_num IS NOT NULL AND btrim(check_eft_num) <> ''
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_checks.csv"

docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT
    webpt_patient_id,
    patient_name,
    date_of_service::text,
    facility_name,
    visit_status,
    COALESCE(primary_check_number,'') AS primary_check_number,
    COALESCE(secondary_check_number,'') AS secondary_check_number,
    COALESCE(total_paid::text,'') AS total_paid,
    COALESCE(matched_paid::text,'') AS matched_paid,
    COALESCE(pending_lines::text,'') AS pending_lines,
    COALESCE(paid_lines::text,'') AS paid_lines
  FROM billing.reconciliation_visit_agg
  WHERE reconciliation_run_id = '${RUN}'::uuid
    AND date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_visits_2026.csv"

docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT
    webpt_patient_id,
    patient_name,
    date_of_service::text,
    cpt_code,
    status,
    COALESCE(match_level,'') AS match_level,
    COALESCE(check_eft_num,'') AS check_eft_num,
    COALESCE(paid_amount::text,'') AS paid_amount
  FROM billing.reconciliation_line
  WHERE reconciliation_run_id = '${RUN}'::uuid
    AND date_of_service >= DATE '2026-01-01'
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/our_lines_2026.csv"

wc -l "${OUT_DIR}/our_checks.csv" "${OUT_DIR}/our_visits_2026.csv" "${OUT_DIR}/our_lines_2026.csv"
echo "OUT_DIR=${OUT_DIR}"
