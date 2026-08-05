#!/usr/bin/env bash
# Idempotent warehouse reload from on-disk artifacts (safe alongside case_drain).
set -uo pipefail
cd /opt/cashflow/deploy
docker compose --env-file .env --profile tools run --rm \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e SCHEDULE_VISITS_CSV=/data/webpt/jan_aug_2026/schedule_visits_2026-01-01_2026-08-30.csv \
  -e PATIENT_PAYMENTS_CSV=/data/webpt/jan_aug_2026/patient_payments_202601_202608.csv \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker python -m cashflow_db load-all
echo "reload exit=$?"
