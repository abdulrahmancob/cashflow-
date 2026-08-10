#!/usr/bin/env bash
# Run audit_billing with host webpt_edco_scraper + case extracted mounted.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"
LOG="${DATA_ROOT:-/data}/logs/audit_billing_mounted.log"
exec >>"$LOG" 2>&1
echo "[audit_mounted] $(date -Is) start"

# Prefer case pipeline extract on /data if present
CASE_EXT=/data/exports/side_by_side_case/extracted
LEGACY_EXT=/data/webpt/output/jun_jul_2026/extracted
if [[ -f "${CASE_EXT}/daily_notes.csv" ]]; then
  SRC="${CASE_EXT}"
elif [[ -f "${LEGACY_EXT}/daily_notes.csv" ]]; then
  SRC="${LEGACY_EXT}"
else
  SRC="${REPO}/webpt_edco_scraper/output/jun_jul_2026/extracted"
fi
OUT=/data/exports/side_by_side_case/reports/audit
mkdir -p "${OUT}" || sudo mkdir -p "${OUT}"

echo "[audit_mounted] extracted=${SRC} out=${OUT}"

docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
  -e PYTHONPATH=/app \
  -v "${REPO}/webpt_edco_scraper:/app/webpt_edco_scraper:ro" \
  -v "${SRC}:${SRC}:ro" \
  -v "${OUT}:${OUT}" \
  -w /app \
  worker python /app/webpt_edco_scraper/scripts/audit_billing.py \
    --extracted "${SRC}" \
    --out "${OUT}"

echo "[audit_mounted] $(date -Is) done"
ls -la "${OUT}" | head -30
