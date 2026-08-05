#!/usr/bin/env bash
# Offline OCR loop for case drain artifacts (safe alongside WebPT drain).
set -euo pipefail

ARTIFACTS="${CASE_PIPELINE_DIR:-/data/exports/side_by_side_case}"
BATCH_ID="${OCR_BATCH_ID:-case_schedule_202601_202608}"
WORKERS="${OCR_WORKERS:-1}"
SLEEP_SEC="${OCR_LOOP_SLEEP_SEC:-300}"
LOG_DIR="${ARTIFACTS}/reports"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/ocr_batch_loop.log"

cd /app
while true; do
  ts="$(date -Iseconds)"
  echo "${ts} OCR_BATCH_TICK start" | tee -a "${LOG}"
  set +e
  python -u snowflake_pull/scripts/run_case_ocr_batch.py \
    --artifacts "${ARTIFACTS}" \
    --workers "${WORKERS}" \
    --batch-id "${BATCH_ID}" >>"${LOG}" 2>&1
  rc=$?
  set -e
  echo "$(date -Iseconds) OCR_BATCH_TICK done exit=${rc}" | tee -a "${LOG}"
  sleep "${SLEEP_SEC}"
done
