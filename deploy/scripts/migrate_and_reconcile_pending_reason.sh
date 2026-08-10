#!/usr/bin/env bash
set -euo pipefail
cd /opt/cashflow/deploy
echo "[migrate] apply SQL migrations"
docker compose --env-file .env run --rm --no-deps -e PYTHONPATH=/app api python -m cashflow_db migrate
echo "[build] rebuild api image with pending_reason"
docker compose --env-file .env build api
echo "[reconcile] softmatch + pending_reason"
docker compose --env-file .env run --rm --no-deps -e PYTHONPATH=/app \
  -v /opt/cashflow/deploy/scripts/run_reconcile_softmatch.py:/tmp/run_reconcile_softmatch.py \
  api python /tmp/run_reconcile_softmatch.py
