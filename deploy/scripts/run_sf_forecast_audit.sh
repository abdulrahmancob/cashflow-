#!/usr/bin/env bash
# Forecast --from-db (SF overrides) then audit_billing with mounts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"
AS_OF="${AS_OF:-$(date +%F)}"
LOG="${DATA_ROOT:-/data}/logs/sf_forecast_audit_${AS_OF}.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "[sf_forecast_audit] $(date -Is) start as_of=${AS_OF}"

run_worker() {
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
    -e PYTHONPATH=/app \
    -v "${REPO}/cashflow_forecast:/app/cashflow_forecast:ro" \
    -v "${REPO}/cashflow_db:/app/cashflow_db:ro" \
    -v "${REPO}/webpt_edco_scraper:/app/webpt_edco_scraper:ro" \
    -w /app \
    worker "$@"
}

echo "[sf_forecast_audit] forecast build --from-db"
run_worker python -m cashflow_forecast build --from-db --as-of "${AS_OF}"

echo "[sf_forecast_audit] audit_billing"
SRC=/data/exports/side_by_side_case/extracted
OUT=/data/exports/side_by_side_case/reports/audit
sudo mkdir -p "${OUT}"
sudo chmod a+rwx "${OUT}" || true
run_worker python /app/webpt_edco_scraper/scripts/audit_billing.py \
  --extracted "${SRC}" \
  --out "${OUT}"

echo "[sf_forecast_audit] verify"
run_worker python -c 'from cashflow_db.repository import connection
with connection() as conn:
  for r in conn.execute("SELECT forecast_run_id::text AS id, status, created_at, as_of_date FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("fc", dict(r))
  rid=conn.execute("SELECT forecast_run_id FROM analytics.forecast_run WHERE status=%s ORDER BY created_at DESC LIMIT 1", ("success",)).fetchone()
  if rid:
    n=conn.execute("SELECT COUNT(*) AS n FROM analytics.forecast_prediction WHERE forecast_run_id=%s", (rid["forecast_run_id"],)).fetchone()
    print("pred_rows", n["n"])
'

echo "[sf_forecast_audit] $(date -Is) done"
ls -la /data/exports/side_by_side_case/reports/audit 2>/dev/null | head -20 || true
