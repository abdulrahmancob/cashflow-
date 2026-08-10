#!/usr/bin/env bash
# Phase B: rebuild CPT/daily_notes aggregates from ~75k case daily_note PDFs.
# Long-running (hours). Logs to /data/logs/phase_b_pdf_extract.log
set -euo pipefail

DEPLOY="${DEPLOY:-/opt/cashflow/deploy}"
CASE_ROOT="${CASE_PIPELINE_DIR:-/data/exports/side_by_side_case}"
EXTRACTED="${CASE_ROOT}/extracted"
LOG="${LOG:-/data/logs/phase_b_pdf_extract.log}"

mkdir -p /data/logs "${CASE_ROOT}/reports"
exec >>"${LOG}" 2>&1

echo "============================================================"
echo "[phase-b] $(date -Is) start"
echo "[phase-b] case_root=${CASE_ROOT}"

if [[ -f "${EXTRACTED}/cpt_codes.csv" ]]; then
  echo "[phase-b] baseline extracted cpt:"
  wc -l "${EXTRACTED}/cpt_codes.csv" "${EXTRACTED}/daily_notes.csv" || true
  md5sum "${EXTRACTED}/cpt_codes.csv" || true
  ts=$(date +%Y%m%d_%H%M%S)
  # Host user may not write under /data/exports (uid 10001); prefer home backup.
  BK="${HOME}/logs/extracted_backup_${ts}"
  mkdir -p "${BK}" 2>/dev/null || BK="/tmp/extracted_backup_${ts}"
  mkdir -p "${BK}"
  cp -a "${EXTRACTED}/cpt_codes.csv" "${EXTRACTED}/daily_notes.csv" "${BK}/" 2>/dev/null || true
  echo "[phase-b] backed up to ${BK}"
fi

cd "${DEPLOY}"
docker compose --env-file .env --profile tools run --rm \
  -e PYTHONUNBUFFERED=1 \
  -e CASE_PIPELINE_DIR="${CASE_ROOT}" \
  scraper python /app/deploy/scripts/phase_b_pdf_extract.py
rc=$?

if [[ "${rc}" -ne 0 ]]; then
  echo "[phase-b] $(date -Is) FAILED rc=${rc}"
  exit "${rc}"
fi
echo "[phase-b] $(date -Is) SUCCESS"
exit 0
