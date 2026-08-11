#!/usr/bin/env bash
set -euo pipefail
cd /opt/cashflow/deploy

echo "== rebuild scraper =="
docker compose --env-file .env build scraper

echo "== mark stuck running pipeline_runs failed =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "UPDATE ops.pipeline_run SET status='failed', finished_at=COALESCE(finished_at, now()), notes=COALESCE(notes,'') || ' | cleaned stuck running' WHERE status='running' AND run_id <> '81fd5c79-4754-42f0-bf10-0ef18b875c73';"

echo "== creds smoke (names only) =="
docker compose --env-file .env run --rm --no-deps scraper python -c "
import os
keys=['REVFLOW_USERNAME','REVFLOW_PASSWORD','WAYSTAR_USERNAME','WAYSTAR_PASSWORD','WEBPT_USERNAME','WEBPT_PASSWORD','SNOWFLAKE_ACCOUNT','SNOWFLAKE_USER','SNOWFLAKE_PASSWORD']
for k in keys:
    v=os.environ.get(k,'')
    print(f'{k}:{\"SET\" if v.strip() else \"EMPTY\"}')
"

echo "== acquire SLA =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT stage_key, max_duration_seconds FROM monitoring.sla_definition WHERE stage_key='acquire';"

echo "== success run check =="
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT run_id, status, finished_at FROM ops.pipeline_run WHERE run_id='81fd5c79-4754-42f0-bf10-0ef18b875c73';"
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT forecast_run_id, as_of_date, status, created_at, dataset_version FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 1;"

echo DONE
