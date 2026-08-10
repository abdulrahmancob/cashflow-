#!/usr/bin/env bash
# After Waystar one-shot exits, run RevFlow headless + warehouse reload once.
set -uo pipefail
FLAG=/data/logs/phase_c_revflow.done
[[ -f "${FLAG}" ]] && exit 0

if sudo -n docker ps --format '{{.Names}}' | grep -q scraper-run; then
  exit 0
fi
if pgrep -f 'revflow_scraper/scraper.py' >/dev/null 2>&1; then
  exit 0
fi

exec 9>/data/logs/phase_c_revflow.lock
flock -n 9 || exit 0

echo "[phase-c] $(date -Is) starting revflow follow-up"
cd /opt/cashflow/deploy
RF_ENV=(
  -e REVFLOW_HEADLESS=true
  -e REVFLOW_OUTPUT_DIR=/data/revflow
  -e REVFLOW_STORAGE_STATE_PATH=/data/revflow/storage_state.json
  -e GMAIL_CREDENTIALS_PATH=/data/revflow/credentials.json
  -e GMAIL_TOKEN_PATH=/data/revflow/gmail_token.json
)
docker compose --env-file .env --profile tools run --rm \
  "${RF_ENV[@]}" \
  scraper python /app/revflow_scraper/scraper.py --headless discover-eobs \
  --from-date 2026-01-01 --to-date 2026-09-30 --output /data/revflow \
  || echo "[phase-c] revflow discover non-zero"

docker compose --env-file .env --profile tools run --rm \
  "${RF_ENV[@]}" \
  scraper python /app/revflow_scraper/scraper.py --headless export-all --output /data/revflow \
  || echo "[phase-c] revflow export non-zero"

docker compose --env-file .env --profile tools run --rm \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e SCHEDULE_VISITS_CSV=/data/webpt/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv \
  -e PATIENT_PAYMENTS_CSV=/data/webpt/jan_aug_2026/patient_payments_202601_202608.csv \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker python -m cashflow_db load-all \
  || echo "[phase-c] load-all non-zero"

date -Is > "${FLAG}"
echo "[phase-c] $(date -Is) done"
