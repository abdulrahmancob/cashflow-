#!/usr/bin/env bash
# Export visit_service_line keys + tracker rows for coverage/tracker audits.
set -euo pipefail
OUT_DIR="${OUT_DIR:-/home/abdu/sf_eval}"
mkdir -p "$OUT_DIR"

echo "[export] visit_service_line 2026+"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT DISTINCT p.webpt_patient_id AS emr_id, v.service_date::text AS dos
  FROM core.visit_service_line sl
  JOIN core.visit v ON v.visit_id = sl.visit_id
  JOIN core.patient p ON p.patient_id = v.patient_id
  WHERE v.service_date >= DATE '2026-01-01'
    AND p.webpt_patient_id IS NOT NULL
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/service_lines_2026.csv"

echo "[export] tracker rows (rich refs)"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT
    COALESCE(eft_1,'') AS eft_1,
    COALESCE(eft_2,'') AS eft_2,
    COALESCE(check_reference,'') AS check_reference,
    COALESCE(description,'') AS description
  FROM billing.transaction_tracker_row
  WHERE deleted_at IS NULL
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/tracker_rows_rich.csv"

wc -l "${OUT_DIR}/service_lines_2026.csv" "${OUT_DIR}/tracker_rows_rich.csv"
