#!/usr/bin/env bash
# Phase D — extend WebPT schedule/payments/cases through 2026-09-30.
# ONLY run when case_drain queue is near-empty (or drain paused) — one WebPT session.
#
# Usage (on server):
#   sudo bash /opt/cashflow/deploy/scripts/extend_schedule_sep.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
# Prefer compose --env-file; avoid bash-sourcing .env (paths may contain spaces).

DATA_ROOT="${DATA_ROOT:-/data}"
BATCH_ID="${SEP_BATCH_ID:-case_schedule_202601_202609}"
FORCE="${FORCE_SEP_EXTEND:-0}"

remaining="$(python3 - <<'PY'
import json
from pathlib import Path
p=Path("/data/exports/side_by_side_case/reports/health.json")
print(json.load(p.open()).get("cases_remaining", 999999) if p.exists() else 999999)
PY
)"

if [[ "${FORCE}" != "1" && "${remaining}" -gt 500 ]]; then
  echo "[sep-extend] ABORT: cases_remaining=${remaining} > 500 (WebPT owned by case_drain)."
  echo "[sep-extend] Re-run with FORCE_SEP_EXTEND=1 only during a controlled drain pause."
  exit 3
fi

echo "[sep-extend] $(date -Is) remaining=${remaining} batch=${BATCH_ID}"

# 1) WebPT schedule append Aug 31 – Sep 30
docker compose --env-file .env --profile tools run --rm scraper \
  python -m webpt_edco_scraper export-schedule \
  --start 2026-08-31 --end 2026-09-30 \
  --output /data/webpt/jan_aug_2026

# 2) Rebuild case schedule Jan 1 – Sep 30
docker compose --env-file .env --profile tools run --rm scraper \
  python -m snowflake_pull.scripts.build_case_schedule \
  --start 2026-01-01 --end 2026-09-30 \
  --output /data/exports/side_by_side_case

# 3) Enqueue new batch id (prefer new id to avoid FSM confusion)
docker compose --env-file .env --profile tools run --rm scraper \
  python -m snowflake_pull.scripts.enqueue_case_batch \
  --batch-id "${BATCH_ID}" \
  --schedule /data/exports/side_by_side_case/schedule/schedule_cases.csv \
  || echo "[sep-extend] enqueue helper missing — update case_drain --batch-id manually"

# 4) Point env at new CSVs when present
if ls /data/webpt/jan_aug_2026/schedule_visits_*2026-09-30*.csv >/dev/null 2>&1; then
  latest="$(ls -1 /data/webpt/jan_aug_2026/schedule_visits_*2026-09-30*.csv | tail -n1)"
  echo "[sep-extend] set SCHEDULE_VISITS_CSV=${latest} in deploy/.env"
fi

# 5) Flip drain/OCR batch ids and recreate drain services
COMPOSE="${ROOT}/docker-compose.yml"
OCR_LOOP="${ROOT}/scripts/ocr_loop.sh"
if [[ -f "${COMPOSE}" ]]; then
  sudo -n sed -i 's/case_schedule_202601_202608/'"${BATCH_ID}"'/g' "${COMPOSE}" || true
fi
if [[ -f "${OCR_LOOP}" ]]; then
  sudo -n sed -i 's/case_schedule_202601_202608/'"${BATCH_ID}"'/g' "${OCR_LOOP}" || true
fi
# Point warehouse loaders at Sep artifacts when present
if ls /data/webpt/jan_aug_2026/schedule_visits_*2026-09-30*.csv >/dev/null 2>&1; then
  latest="$(ls -1 /data/webpt/jan_aug_2026/schedule_visits_*2026-09-30*.csv | tail -n1)"
  python3 - "${latest}" <<'PY'
import sys
from pathlib import Path
latest = sys.argv[1]
p = Path('/opt/cashflow/deploy/.env')
lines = []
seen = False
for line in p.read_text().splitlines():
    if line.startswith('SCHEDULE_VISITS_CSV='):
        lines.append(f'SCHEDULE_VISITS_CSV={latest}')
        seen = True
        continue
    lines.append(line)
if not seen:
    lines.append(f'SCHEDULE_VISITS_CSV={latest}')
p.write_text('\n'.join(lines) + '\n')
print('updated SCHEDULE_VISITS_CSV', latest)
PY
fi

echo "[sep-extend] recreating case_drain/case_ocr on batch ${BATCH_ID}"
docker compose --env-file .env --profile drain up -d case_drain case_ocr || true
echo "[sep-extend] $(date -Is) done"
