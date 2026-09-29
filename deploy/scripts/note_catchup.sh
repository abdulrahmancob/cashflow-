#!/usr/bin/env bash
# Host-triggered 18:00 Africa/Cairo daily-note catch-up.
# Download + OCR missing last-7-day notes so CPT/ICD audit is ready for submission.
#
# Example crontab (host TZ must be Africa/Cairo):
#   0 18 * * * /opt/cashflow/deploy/scripts/note_catchup.sh >> /data/logs/note_catchup.log 2>&1
#
# Does not stop postgres / nginx / case_ocr / case_drain / api.
# Replaces leftover week_note_stream so a hung job cannot skip scrape forever.
# Skips WebPT only while case_drain still owns the session (cases_remaining > 500).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export GIT_SHA="${GIT_SHA:-$(git -C "${ROOT}/.." rev-parse --short HEAD 2>/dev/null || echo unknown)}"

LOG_DIR="${DATA_ROOT:-/data}/logs"
mkdir -p "${LOG_DIR}"

echo "[note-catchup] $(date -Is) start GIT_SHA=${GIT_SHA}"

HEALTH_JSON="${DATA_ROOT:-/data}/exports/side_by_side_case/reports/health.json"
remaining="$(python3 - <<PY
import json
from pathlib import Path
p = Path("${HEALTH_JSON}")
print(json.load(p.open()).get("cases_remaining", 0) if p.exists() else 0)
PY
)"

if [[ "${NOTE_CATCHUP_SKIP_SCRAPE:-}" == "1" ]] || [[ "${remaining}" -gt 500 ]]; then
  echo "[note-catchup] skip scrape (remaining=${remaining}; NOTE_CATCHUP_SKIP_SCRAPE=${NOTE_CATCHUP_SKIP_SCRAPE:-})"
  echo "[note-catchup] $(date -Is) done"
  exit 0
fi

echo "[note-catchup] stop leftover WebPT scrapers"
docker ps -a --format '{{.Names}}' | grep -E '^week_note_stream$|^cashflow-scraper-run-' | while read -r n; do
  echo "[note-catchup] removing ${n}"
  docker rm -f "${n}" || true
done || true

BIND_MOUNTS=(
  -v /opt/cashflow/cashflow_ops:/app/cashflow_ops
  -v /opt/cashflow/cashflow_db:/app/cashflow_db
  -v /opt/cashflow/webpt_edco_scraper:/app/webpt_edco_scraper
  -v /opt/cashflow/snowflake_pull:/app/snowflake_pull
)

echo "[note-catchup] start week_note_stream --days ${NOTE_CATCHUP_DAYS:-7} NO_TIMEOUT"
docker compose --env-file "${ROOT}/.env" --profile tools run -d --name week_note_stream \
  -e NOTE_CATCHUP_NO_TIMEOUT=1 \
  "${BIND_MOUNTS[@]}" \
  scraper python -m cashflow_ops note-catchup --days "${NOTE_CATCHUP_DAYS:-7}"

sleep 8
if docker ps --format '{{.Names}}' | grep -qx week_note_stream; then
  echo "[note-catchup] week_note_stream is up"
  docker logs week_note_stream 2>&1 | grep -E 'missing_visits|login succeeded|Session invalid' | tail -n 6 || true
else
  echo "[note-catchup] week_note_stream failed to stay up" >&2
  docker logs week_note_stream 2>&1 | tail -n 30 || true
  exit 1
fi

echo "[note-catchup] $(date -Is) done"
