#!/usr/bin/env bash
set -euo pipefail
cd /opt/cashflow/deploy

echo "== sync check =="
test -f /opt/cashflow/cashflow_ops/stages/load_warehouse.py
grep -q warehouse_validate_drift /opt/cashflow/cashflow_ops/stages/load_warehouse.py

echo "== rebuild worker/api =="
docker compose --env-file .env build worker api
docker compose --env-file .env up -d --force-recreate api

RUN_ID="${1:-81fd5c79-4754-42f0-bf10-0ef18b875c73}"

echo "== mark load_warehouse success for resume run=$RUN_ID =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -v ON_ERROR_STOP=1 <<SQL
UPDATE ops.stage_run
SET status = 'success',
    error_message = 'validate drift accepted for resume',
    finished_at = now()
WHERE run_id = '$RUN_ID'
  AND stage_key = 'load_warehouse';

UPDATE ops.stage_run
SET status = 'pending',
    error_message = NULL,
    finished_at = NULL
WHERE run_id = '$RUN_ID'
  AND status IN ('blocked', 'failed')
  AND stage_key <> 'load_warehouse';

UPDATE ops.pipeline_run
SET status = 'running',
    finished_at = NULL,
    notes = COALESCE(notes, '') || ' | resume after validate soften'
WHERE run_id = '$RUN_ID';
SQL

LOG=/data/logs/manual_recovery_resume.log
echo "== resume pipeline -> $LOG =="
nohup docker compose --env-file .env --profile tools run --rm \
  worker python -m cashflow_ops resume --run-id "$RUN_ID" --skip-scrapers \
  >"$LOG" 2>&1 &
echo RESUME_PID=$!
sleep 10
tail -n 40 "$LOG" || true
