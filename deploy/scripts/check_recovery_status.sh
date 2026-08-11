#!/usr/bin/env bash
set -euo pipefail
RUN_ID="${1:-81fd5c79-4754-42f0-bf10-0ef18b875c73}"
echo "== pipeline_run =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT run_id, status, trigger_source, started_at, finished_at FROM ops.pipeline_run WHERE run_id='$RUN_ID';"
echo "== stage_run =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT stage_key, status, attempt, LEFT(COALESCE(error_message,''),80) AS err FROM ops.stage_run WHERE run_id='$RUN_ID' ORDER BY stage_key;"
echo "== latest forecast =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT * FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3;"
echo "== recent etl =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT source_system, status, started_at, finished_at FROM etl.etl_run ORDER BY started_at DESC LIMIT 8;"
echo "== resume log tail =="
tail -n 50 /data/logs/manual_recovery_resume.log || true
echo "== containers =="
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'worker|scraper|api|postgres' || true
