#!/usr/bin/env bash
# Resume RevFlow discover/export + warehouse reload. Never touches WebPT.
set -uo pipefail
cd /opt/cashflow/deploy
mkdir -p /data/logs /data/revflow

# Clear prior done flag so a fresh attempt is allowed if invoked from followup
# (this script is the intentional resume — do not re-trigger hung waystar)

RF_ENV=(
  -e REVFLOW_HEADLESS=true
  -e REVFLOW_OUTPUT_DIR=/data/revflow
  -e REVFLOW_STORAGE_STATE_PATH=/data/revflow/storage_state.json
  -e GMAIL_CREDENTIALS_PATH=/data/revflow/credentials.json
  -e GMAIL_TOKEN_PATH=/data/revflow/gmail_token.json
  -e GMAIL_POLL_TIMEOUT_SEC=300
)

echo "[revflow] $(date -Is) login smoke"
docker compose --env-file .env --profile tools run --rm --no-deps \
  "${RF_ENV[@]}" \
  scraper python /app/revflow_scraper/scraper.py --headless login \
  || { echo "[revflow] login failed"; exit 2; }

echo "[revflow] $(date -Is) discover-eobs"
docker compose --env-file .env --profile tools run --rm --no-deps \
  "${RF_ENV[@]}" \
  scraper python /app/revflow_scraper/scraper.py --headless discover-eobs \
  --from-date 2026-01-01 --to-date 2026-09-30 --output /data/revflow \
  || echo "[revflow] discover non-zero"

echo "[revflow] $(date -Is) export-all"
docker compose --env-file .env --profile tools run --rm --no-deps \
  "${RF_ENV[@]}" \
  scraper python /app/revflow_scraper/scraper.py --headless export-all --output /data/revflow \
  || echo "[revflow] export non-zero"

echo "[revflow] artifacts:"
find /data/revflow -type f | head -n 40 || true

echo "[revflow] $(date -Is) load-all"
docker compose --env-file .env --profile tools run --rm \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e SCHEDULE_VISITS_CSV=/data/webpt/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv \
  -e PATIENT_PAYMENTS_CSV=/data/webpt/jan_aug_2026/patient_payments_202601_202608.csv \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker python -m cashflow_db load-all \
  || echo "[revflow] load-all non-zero"

echo "[revflow] $(date -Is) done"
python3 - <<'PY'
import json
from pathlib import Path
# quick counts
import subprocess
for label, sql in [
    ('eob', 'SELECT count(*) FROM billing.eob_check'),
    ('patient', 'SELECT count(*) FROM core.patient'),
]:
    r=subprocess.run(['sudo','-n','docker','exec','cashflow-postgres-1','psql','-U','cashflow','-d','cashflow','-tAc',sql],capture_output=True,text=True)
    print(label, (r.stdout or '').strip())
d=json.load(open('/data/exports/side_by_side_case/reports/health.json'))
print('drain', d.get('cases_remaining'), d.get('auth_status'))
PY
