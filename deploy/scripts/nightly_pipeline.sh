#!/usr/bin/env bash
# Host-triggered daily RCM pipeline (02:00 Africa/Cairo).
# Orchestration stays inside cashflow_ops; this script only triggers containers.
#
# Example crontab (host TZ must be Africa/Cairo):
#   0 2 * * * /opt/cashflow/deploy/scripts/nightly_pipeline.sh >> /data/logs/nightly.log 2>&1
#
# DO NOT run cron inside containers.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# Do not bash-source .env (values may contain spaces). compose --env-file loads it.

export GIT_SHA="${GIT_SHA:-$(git -C "${ROOT}/.." rev-parse --short HEAD 2>/dev/null || echo unknown)}"

LOG_DIR="${DATA_ROOT:-/data}/logs"
mkdir -p "${LOG_DIR}"

echo "[nightly] $(date -Is) start GIT_SHA=${GIT_SHA}"

HEALTH_JSON="${DATA_ROOT:-/data}/exports/side_by_side_case/reports/health.json"
remaining="$(python3 - <<PY
import json
from pathlib import Path
p = Path("${HEALTH_JSON}")
print(json.load(p.open()).get("cases_remaining", 0) if p.exists() else 0)
PY
)"
# While case_drain owns the sole WebPT session, never start Acquire scrapers.
skip_scrapers=0
if [[ "${NIGHTLY_SKIP_SCRAPERS:-}" == "1" ]] || [[ "${remaining}" -gt 500 ]]; then
  skip_scrapers=1
  echo "[nightly] skip scrapers (remaining=${remaining}; NIGHTLY_SKIP_SCRAPERS=${NIGHTLY_SKIP_SCRAPERS:-})"
fi

if [[ "${skip_scrapers}" -eq 0 ]]; then
  # Full Acquire (WebPT + RevFlow + Waystar + Snowflake) when drain is safe.
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm scraper \
    python -m cashflow_ops run --trigger task_scheduler \
    || echo "[nightly] scraper pass finished with non-zero (check ops status)"
fi

# Worker: warehouse / post-scrape path (always safe alongside drain)
docker compose --env-file "${ROOT}/.env" --profile tools run --rm worker \
  python -m cashflow_ops run --trigger task_scheduler --skip-scrapers \
  || echo "[nightly] worker pass finished with non-zero (check ops status)"

echo "[nightly] $(date -Is) done"
