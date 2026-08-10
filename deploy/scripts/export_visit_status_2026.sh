#!/usr/bin/env bash
# Export EMR+DOS visit status (+ note/cpt flags) for coverage schedule-status probe.
set -euo pipefail
OUT_DIR="${OUT_DIR:-/home/abdu/sf_eval}"
mkdir -p "$OUT_DIR"

echo "[export] visit status 2026 pack"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c "\copy (
  SELECT
    p.webpt_patient_id AS emr_id,
    v.service_date::text AS dos,
    COALESCE(v.status, '') AS visit_status,
    CASE WHEN v.check_out_at IS NOT NULL THEN '1' ELSE '0' END AS has_check_out,
    CASE WHEN EXISTS (
      SELECT 1 FROM core.clinical_note cn WHERE cn.visit_id = v.visit_id
    ) THEN '1' ELSE '0' END AS has_note,
    CASE WHEN EXISTS (
      SELECT 1 FROM core.visit_service_line sl WHERE sl.visit_id = v.visit_id
    ) THEN '1' ELSE '0' END AS has_cpt,
    COALESCE((
      SELECT sa.status FROM core.schedule_appointment sa
      WHERE sa.patient_id = v.patient_id AND sa.service_date = v.service_date
      ORDER BY
        CASE sa.status
          WHEN 'completed' THEN 0
          WHEN 'unchecked_out' THEN 1
          WHEN 'scheduled' THEN 2
          WHEN 'no_show' THEN 3
          WHEN 'cancelled' THEN 4
          ELSE 5
        END
      LIMIT 1
    ), '') AS schedule_status
  FROM core.visit v
  JOIN core.patient p ON p.patient_id = v.patient_id
  WHERE v.service_date BETWEEN DATE '2026-01-01' AND DATE '2026-08-30'
    AND p.webpt_patient_id IS NOT NULL
    AND btrim(p.webpt_patient_id) <> ''
) TO STDOUT WITH CSV HEADER" > "${OUT_DIR}/visit_status_2026.csv"

wc -l "${OUT_DIR}/visit_status_2026.csv"
head -2 "${OUT_DIR}/visit_status_2026.csv"
