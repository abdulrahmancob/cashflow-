#!/usr/bin/env bash
# Phase C — RevFlow + Waystar only. Never touches WebPT (case_drain owns the session).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
mkdir -p /data/logs /data/revflow /data/waystar

START="${WINDOW_START:-2026-01-01}"
END="${WINDOW_END:-2026-09-30}"

RF_ENV=(
  -e REVFLOW_OUTPUT_DIR=/data/revflow
  -e REVFLOW_HEADLESS=true
  -e REVFLOW_STORAGE_STATE_PATH=/data/revflow/storage_state.json
  -e GMAIL_CREDENTIALS_PATH=/data/revflow/credentials.json
  -e GMAIL_TOKEN_PATH=/data/revflow/gmail_token.json
  -e GMAIL_POLL_TIMEOUT_SEC="${GMAIL_POLL_TIMEOUT_SEC:-300}"
)

echo "[non-webpt] $(date -Is) RevFlow discover+export ${START}..${END}"
docker compose --env-file .env --profile tools run --rm \
  "${RF_ENV[@]}" \
  scraper \
  python /app/revflow_scraper/scraper.py --headless discover-eobs \
    --from-date "${START}" --to-date "${END}" --output /data/revflow \
  || echo "[non-webpt] revflow discover non-zero"

docker compose --env-file .env --profile tools run --rm \
  "${RF_ENV[@]}" \
  scraper \
  python /app/revflow_scraper/scraper.py --headless export-all --output /data/revflow \
  || echo "[non-webpt] revflow export non-zero"

echo "[non-webpt] $(date -Is) Waystar rejected"
docker compose --env-file .env --profile tools run --rm \
  -e WAYSTAR_OUTPUT_DIR=/data/waystar \
  scraper \
  python /app/waystar_scraper/scraper.py \
    --rejected \
    --trans-from "01/01/2026" \
    --trans-to "09/30/2026" \
    --run-id "ops_${START}_${END}" \
  || echo "[non-webpt] waystar rejected non-zero"

echo "[non-webpt] Snowflake skipped (creds commented in .env)"

echo "[non-webpt] reload warehouse"
docker compose --env-file .env --profile tools run --rm \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e SCHEDULE_VISITS_CSV=/data/webpt/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv \
  -e PATIENT_PAYMENTS_CSV=/data/webpt/jan_aug_2026/patient_payments_202601_202608.csv \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker \
  python -m cashflow_db load-all \
  || echo "[non-webpt] load-all non-zero"

echo "[non-webpt] $(date -Is) done"
