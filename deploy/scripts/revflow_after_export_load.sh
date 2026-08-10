#!/usr/bin/env bash
# When RevFlow export-all finishes, reload warehouse once.
set -uo pipefail
FLAG=/data/logs/revflow_load_after_export.done
[[ -f "${FLAG}" ]] && exit 0
# Export still running?
if sudo -n docker ps --format '{{.Names}}' | grep -q scraper-run; then
  exit 0
fi
# Need catalog + some exports
n=$(find /data/revflow/exports -name '*.csv' 2>/dev/null | wc -l)
[[ "${n}" -gt 0 ]] || exit 0
[[ -f /data/revflow/eob_catalog.json ]] || exit 0

exec 9>/data/logs/revflow_load_after_export.lock
flock -n 9 || exit 0

echo "[revflow-load] $(date -Is) exports=${n} starting load-all"
cd /opt/cashflow/deploy
docker compose --env-file .env --profile tools run --rm \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e SCHEDULE_VISITS_CSV=/data/webpt/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv \
  -e PATIENT_PAYMENTS_CSV=/data/webpt/jan_aug_2026/patient_payments_202601_202608.csv \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker python -m cashflow_db load-all \
  || echo "[revflow-load] load-all non-zero"
date -Is > "${FLAG}"
echo "[revflow-load] $(date -Is) done"
