#!/usr/bin/env bash
# Run ON the server as: sudo bash /opt/cashflow/deploy/scripts/server_bootstrap_and_drain.sh
# Or from abdu home staging: sudo bash ~/cashflow/deploy/scripts/server_bootstrap_and_drain.sh
set -euo pipefail

REPO_SRC="${REPO_SRC:-/home/abdu/cashflow}"
DATA_STAGING="${DATA_STAGING:-/home/abdu/data-staging}"
OPT_ROOT="${OPT_ROOT:-/opt/cashflow}"
DATA_ROOT="${DATA_ROOT:-/data}"

echo "==> Bootstrap host"
bash "${REPO_SRC}/deploy/bootstrap_host.sh"

echo "==> Place code at ${OPT_ROOT}"
mkdir -p "${OPT_ROOT}"
rsync -a --delete \
  --exclude '.venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
  --exclude '.git/' --exclude '*.pyc' \
  "${REPO_SRC}/" "${OPT_ROOT}/"

echo "==> Place staged data"
mkdir -p "${DATA_ROOT}/exports/side_by_side_case" "${DATA_ROOT}/webpt"
if [[ -d "${DATA_STAGING}/side_by_side_case" ]]; then
  rsync -a "${DATA_STAGING}/side_by_side_case/" "${DATA_ROOT}/exports/side_by_side_case/"
fi
if [[ -d "${DATA_STAGING}/webpt" ]]; then
  rsync -a "${DATA_STAGING}/webpt/" "${DATA_ROOT}/webpt/"
fi
if [[ -f "${REPO_SRC}/webpt_edco_scraper/storage_state.json" ]]; then
  cp -a "${REPO_SRC}/webpt_edco_scraper/storage_state.json" "${DATA_ROOT}/webpt/storage_state.json"
fi

chown -R 10001:10001 \
  "${DATA_ROOT}/webpt" \
  "${DATA_ROOT}/exports" \
  "${DATA_ROOT}/ocr" \
  "${DATA_ROOT}/logs" \
  "${DATA_ROOT}/backups" || true

if [[ ! -f "${OPT_ROOT}/deploy/.env" ]]; then
  echo "ERROR: missing ${OPT_ROOT}/deploy/.env — copy from staging first" >&2
  exit 1
fi
if [[ -f "${REPO_SRC}/deploy/.env" && ! -f "${OPT_ROOT}/deploy/.env" ]]; then
  cp -a "${REPO_SRC}/deploy/.env" "${OPT_ROOT}/deploy/.env"
fi
# Prefer staged env if present
if [[ -f "${REPO_SRC}/deploy/.env" ]]; then
  cp -a "${REPO_SRC}/deploy/.env" "${OPT_ROOT}/deploy/.env"
fi
chmod 600 "${OPT_ROOT}/deploy/.env"

chmod +x "${OPT_ROOT}/deploy/scripts/"*.sh || true

cd "${OPT_ROOT}/deploy"
echo "==> Build + start postgres/api/nginx"
docker compose --env-file .env up -d --build postgres api nginx

echo "==> Migrate"
docker compose --env-file .env --profile tools run --rm worker \
  python -m cashflow_db migrate

echo "==> Health"
curl -fsS http://127.0.0.1/alive
echo
curl -fsS http://127.0.0.1/ready
echo

echo "==> Start single case_drain + case_ocr"
docker compose --env-file .env --profile drain up -d --build case_drain case_ocr

echo "==> Status"
docker compose --env-file .env --profile drain ps
echo "DONE"
