#!/usr/bin/env bash
# Standing ops: after any WebPT case extract refresh → load-webpt → reconcile.
# Do not skip load when "DB looks full"; load-webpt upserts missing case-aware rows.
# Never blind-load CPT without case_id. Keep CASHFLOW_NAME_MATCH_LEVENSHTEIN unset/0.
set -euo pipefail

LOG_DIR="${LOG_DIR:-/data/logs}"
REPORT_DIR="${REPORT_DIR:-/data/exports/side_by_side_case/reports}"
mkdir -p "$LOG_DIR" "$REPORT_DIR"
LOG="$LOG_DIR/ops_extract_load_reconcile.log"
cd /opt/cashflow/deploy

echo "[ops] $(date -Is) start extract→load-webpt→reconcile" | tee -a "$LOG"

# 1) Catch-up / sync missing notes+CPT from CASE_PIPELINE extracted/
docker compose --env-file .env --profile tools run --rm \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/app \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/deploy/scripts:/app/deploy/scripts:ro \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  worker python -m cashflow_db load-webpt 2>&1 | tee -a "$LOG"

# 2) Reconcile (soft-match ON via code; Levenshtein OFF)
docker compose --env-file .env --profile tools run --rm \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/app \
  -e CASHFLOW_NAME_MATCH_LEVENSHTEIN=0 \
  -e WEBPT_OUTPUT_DIR=/data/webpt/jan_aug_2026 \
  -e CASE_PIPELINE_DIR=/data/exports/side_by_side_case \
  -e REVFLOW_OUTPUT_DIR=/data/revflow \
  -v /opt/cashflow/deploy/scripts:/app/deploy/scripts:ro \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db:ro \
  -v /opt/cashflow/cashflow_ops:/app/cashflow_ops:ro \
  -v /opt/cashflow/cashflow_reconcile:/app/cashflow_reconcile:ro \
  worker python /app/deploy/scripts/phase_c2_reconcile_dq.py 2>&1 | tee -a "$LOG"

echo "[ops] $(date -Is) done" | tee -a "$LOG"
