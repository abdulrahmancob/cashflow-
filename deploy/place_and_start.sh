#!/usr/bin/env bash
# Place staged code/data and start compose stack + drain.
set -euo pipefail

REPO_SRC="${REPO_SRC:-/home/abdu/cashflow}"
DATA_STAGING="${DATA_STAGING:-/home/abdu/data-staging}"
OPT_ROOT="${OPT_ROOT:-/opt/cashflow}"
DATA_ROOT="${DATA_ROOT:-/data}"

echo "==> Refresh code at ${OPT_ROOT}"
mkdir -p "${OPT_ROOT}"
rsync -a \
  --exclude '.venv/' --exclude 'venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
  --exclude '.git/' --exclude 'waystar_scraper/output/' --exclude 'webpt_edco_scraper/output/' \
  "${REPO_SRC}/" "${OPT_ROOT}/"
find "${OPT_ROOT}/deploy" -name '*.sh' -print0 | xargs -0 sed -i 's/\r$//'
chmod +x "${OPT_ROOT}/deploy/scripts/"*.sh || true

echo "==> Place staged case/webpt data"
mkdir -p "${DATA_ROOT}/exports/side_by_side_case" "${DATA_ROOT}/webpt"
if [[ -d "${DATA_STAGING}/side_by_side_case" ]]; then
  rsync -a "${DATA_STAGING}/side_by_side_case/" "${DATA_ROOT}/exports/side_by_side_case/"
fi
if [[ -d "${DATA_STAGING}/webpt" ]]; then
  rsync -a "${DATA_STAGING}/webpt/" "${DATA_ROOT}/webpt/" || true
fi
if [[ -f "${OPT_ROOT}/webpt_edco_scraper/storage_state.json" ]]; then
  cp -a "${OPT_ROOT}/webpt_edco_scraper/storage_state.json" "${DATA_ROOT}/webpt/storage_state.json"
fi

if [[ -f "${REPO_SRC}/deploy/.env" ]]; then
  cp -a "${REPO_SRC}/deploy/.env" "${OPT_ROOT}/deploy/.env"
fi
chmod 600 "${OPT_ROOT}/deploy/.env"

chown -R 10001:10001 \
  "${DATA_ROOT}/webpt" \
  "${DATA_ROOT}/exports" \
  "${DATA_ROOT}/ocr" \
  "${DATA_ROOT}/logs" \
  "${DATA_ROOT}/backups" || true

echo "==> Build + start postgres/api/nginx"
cd "${OPT_ROOT}/deploy"
docker compose --env-file .env up -d --build postgres api nginx

echo "==> Migrate"
docker compose --env-file .env --profile tools run --rm worker \
  python -m cashflow_db migrate

echo "==> Health"
curl -fsS http://127.0.0.1/alive; echo
curl -fsS http://127.0.0.1/ready; echo

echo "==> Start case_drain + case_ocr (single session)"
docker compose --env-file .env --profile drain up -d --build case_drain case_ocr

docker compose --env-file .env --profile drain ps
echo "DONE"
