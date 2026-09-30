#!/usr/bin/env bash
# Host-triggered 15:00 Africa/Cairo daily-note catch-up.
# Download + OCR missing last-7-day notes so CPT/ICD audit is ready for submission.
#
# Example crontab (host TZ must be Africa/Cairo):
#   0 15 * * * /opt/cashflow/deploy/scripts/note_catchup.sh >> /data/logs/note_catchup.log 2>&1
#
# Does not stop postgres / nginx / case_ocr / case_drain / api / intake census.
# Three WebPT accounts each take whole clinics. Sessions stay under /data/webpt/sessions.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export GIT_SHA="${GIT_SHA:-$(git -C "${ROOT}/.." rev-parse --short HEAD 2>/dev/null || echo unknown)}"

LOG_DIR="${DATA_ROOT:-/data}/logs"
mkdir -p "${LOG_DIR}"

echo "[note-catchup] $(date -Is) start GIT_SHA=${GIT_SHA}"

if [[ "${NOTE_CATCHUP_SKIP_SCRAPE:-}" == "1" ]]; then
  echo "[note-catchup] skip scrape (NOTE_CATCHUP_SKIP_SCRAPE=1)"
  echo "[note-catchup] $(date -Is) done"
  exit 0
fi

echo "[note-catchup] stop leftover note catch-up containers"
docker ps -a --format '{{.Names}}' | grep -E '^week_note_stream$|^note_catchup_hamdy[123]$' | while read -r n; do
  echo "[note-catchup] removing ${n}"
  docker rm -f "${n}" || true
done || true

BIND_MOUNTS=(
  -v /opt/cashflow/cashflow_ops:/app/cashflow_ops
  -v /opt/cashflow/cashflow_db:/app/cashflow_db
  -v /opt/cashflow/webpt_edco_scraper:/app/webpt_edco_scraper
  -v /opt/cashflow/snowflake_pull:/app/snowflake_pull
)

DAYS="${NOTE_CATCHUP_DAYS:-7}"

start_one() {
  local folder="$1"
  local user="$2"
  local index="$3"
  echo "[note-catchup] start note_catchup_${folder} shard ${index}/3 --days ${DAYS}"
  docker compose --env-file "${ROOT}/.env" --profile tools run -d --no-deps \
    --name "note_catchup_${folder}" \
    "${BIND_MOUNTS[@]}" \
    -e "WEBPT_STORAGE_STATE=/data/webpt/sessions/${folder}/storage_state.json" \
    -e WEBPT_HEADLESS=true \
    -e PYTHONUNBUFFERED=1 \
    scraper \
    sh -c "set -a; . /data/webpt/sessions/${folder}.env; set +a; case \"\$WEBPT_STORAGE_STATE\" in */sessions/*) ;; *) echo REFUSE_STORAGE; exit 2;; esac; case \"\$WEBPT_USERNAME\" in ${user}) ;; *) echo REFUSE_USER; exit 2;; esac; exec python -u /app/snowflake_pull/scripts/run_note_catchup.py --days ${DAYS} --shard-index ${index} --shard-count 3"
}

start_one hamdy1 Hamdy1 0
start_one hamdy2 Hamdy2 1
start_one hamdy3 Hamdy3 2

sleep 12
failed=0
for folder in hamdy1 hamdy2 hamdy3; do
  name="note_catchup_${folder}"
  if docker ps --format '{{.Names}}' | grep -qx "${name}"; then
    echo "[note-catchup] ${name} is up"
    docker logs "${name}" 2>&1 | grep -E 'missing_visits|only_facilities|USER_OK|REFUSE|login succeeded|Session invalid' | tail -n 4 || true
  else
    echo "[note-catchup] ${name} failed to stay up" >&2
    docker logs "${name}" 2>&1 | tail -n 30 || true
    failed=1
  fi
done

if [[ "${failed}" -ne 0 ]]; then
  exit 1
fi

echo "[note-catchup] $(date -Is) done"
