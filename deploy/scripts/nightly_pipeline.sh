#!/usr/bin/env bash
# Host-triggered daily RCM pipeline (02:00 Africa/Cairo).
# Orchestration stays inside cashflow_ops; this script only triggers containers.
#
# Example crontab (host TZ must be Africa/Cairo):
#   0 2 * * * bash /opt/cashflow/deploy/scripts/nightly_pipeline.sh >> /data/logs/nightly.log 2>&1
#
# DO NOT run cron inside containers.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export GIT_SHA="${GIT_SHA:-$(git -C "${ROOT}/.." rev-parse --short HEAD 2>/dev/null || echo unknown)}"

LOG_DIR="${DATA_ROOT:-/data}/logs"
mkdir -p "${LOG_DIR}"

echo "[nightly] $(date -Is) start GIT_SHA=${GIT_SHA}"

LOCK="${LOG_DIR}/nightly.lock"
exec 9>"${LOCK}"
if ! flock -n 9; then
  echo "[nightly] another nightly_pipeline.sh holds ${LOCK} — skip"
  exit 0
fi

running="$(docker ps --format '{{.Names}}' | grep -E '^(cashflow-nightly-worker|cashflow-worker-run-)' || true)"
if [ -n "${running}" ]; then
  echo "[nightly] worker already running (${running}) — skip"
  exit 0
fi

# compose run --name refuses to start when an exited container still holds the name.
drop_stopped_container() {
  local name="$1"
  local state
  state="$(docker inspect -f '{{.State.Status}}' "${name}" 2>/dev/null || true)"
  if [ -n "${state}" ] && [ "${state}" != "running" ]; then
    echo "[nightly] removing stopped container ${name} (${state})"
    docker rm "${name}"
  fi
}

# Scraper: Waystar recent claims + RevFlow + Snowflake PT_CITY (stop after acquire).
# Acquire does not use the WebPT login, so keep it running while week_note_stream
# is up. Force RevFlow headless: production .env may set REVFLOW_HEADLESS=false.
docker compose --env-file "${ROOT}/.env" --profile tools run --rm \
  -e REVFLOW_HEADLESS=true \
  scraper \
  python -m cashflow_ops run --trigger task_scheduler --stop-after acquire \
  || echo "[nightly] scraper pass finished with non-zero (check ops status)"

# Worker: load-nightly + waystar recon + forecast (no browsers).
drop_stopped_container cashflow-nightly-worker
docker compose --env-file "${ROOT}/.env" --profile tools run --rm \
  --name cashflow-nightly-worker \
  worker \
  python -m cashflow_ops run --trigger task_scheduler --skip-scrapers \
  || echo "[nightly] worker pass finished with non-zero (check ops status)"

# Eligibility sheet sync: copy recon check amounts / pending-tracker tags.
# Full generate-eligibility holds one long transaction and can lock the sheet.
drop_stopped_container cashflow-nightly-elig-sync
docker compose --env-file "${ROOT}/.env" --profile tools run --rm \
  --name cashflow-nightly-elig-sync \
  -e PYTHONPATH=/app \
  -v /opt/cashflow/cashflow_db:/app/cashflow_db \
  -w /app \
  worker \
  python -m cashflow_db waystar-sync-check-context \
  || echo "[nightly] ALERT eligibility context sync failed"

# If recon/forecast failed, resume once then alert — publish_monitor never
# runs after a STOP failure so this is the only guaranteed webhook.
AS_OF="$(TZ=Africa/Cairo date +%F)"
drop_stopped_container cashflow-nightly-forecast-check
docker compose --env-file "${ROOT}/.env" --profile tools run --rm \
  --name cashflow-nightly-forecast-check \
  worker \
  python -m cashflow_ops check-forecast --as-of "${AS_OF}" \
  || echo "[nightly] ALERT missing success forecast for ${AS_OF}"

echo "[nightly] $(date -Is) done"
