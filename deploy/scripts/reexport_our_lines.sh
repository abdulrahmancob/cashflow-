#!/usr/bin/env bash
set -euo pipefail
OUT_DIR="${OUT_DIR:-/home/abdu/sf_eval}"
RUN="${RUN:-aae72074-5b88-438e-b238-960dd08208a3}"
mkdir -p "$OUT_DIR"
echo "[export] recon lines with modifier"
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
wc -l "${OUT_DIR}/our_lines_2026.csv"
head -1 "${OUT_DIR}/our_lines_2026.csv"
