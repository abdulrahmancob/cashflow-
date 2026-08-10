#!/usr/bin/env bash
# Daily backup: PostgreSQL dump + OCR/raw data snapshot.
# Retain 14 days. Designed to run on the host via cron (Deployment phase).
#
# Example cron (Africa/Cairo):
#   30 3 * * * /opt/cashflow/deploy/scripts/backup.sh >> /data/logs/backup.log 2>&1
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
set -a
[[ -f "${ROOT}/.env" ]] && source "${ROOT}/.env"
set +a

DATA_ROOT="${DATA_ROOT:-/data}"
BACKUP_ROOT="${DATA_ROOT}/backups"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"
DEST="${BACKUP_ROOT}/${STAMP}"

mkdir -p "${DEST}"
cd "${ROOT}"

echo "[backup] starting ${STAMP}"

docker compose --env-file "${ROOT}/.env" exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-cashflow}" -d "${POSTGRES_DB:-cashflow}" \
  | gzip -c > "${DEST}/cashflow.sql.gz"

for dir in webpt revflow waystar ocr exports; do
  if [[ -d "${DATA_ROOT}/${dir}" ]]; then
    tar -C "${DATA_ROOT}" -czf "${DEST}/${dir}.tar.gz" \
      --exclude='**/.cache' \
      --exclude='**/ms-playwright' \
      "${dir}" 2>/dev/null || true
  fi
done

echo "[backup] wrote ${DEST}"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} +
echo "[backup] pruned backups older than ${KEEP_DAYS} days"
echo "[backup] done"
