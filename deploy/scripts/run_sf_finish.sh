#!/usr/bin/env bash
# Finish: insurance_behavior (with empty-date fix) -> forecast --from-db -> audit mounted
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"
AS_OF="${AS_OF:-$(date +%F)}"
LOG="${DATA_ROOT:-/data}/logs/sf_audit_forecast_${AS_OF}_finish.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "[sf_finish] $(date -Is) start as_of=${AS_OF}"

run_worker() {
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
    -e PYTHONPATH=/app \
    -v "${REPO}/cashflow_forecast:/app/cashflow_forecast:ro" \
    -v "${REPO}/cashflow_db:/app/cashflow_db:ro" \
    -v "${REPO}/webpt_edco_scraper:/app/webpt_edco_scraper:ro" \
    -w /app \
    worker "$@"
}

echo "[sf_finish] insurance_behavior --from-db"
run_worker python -m cashflow_reconcile.insurance_behavior --from-db

echo "[sf_finish] forecast build --from-db"
run_worker python -m cashflow_forecast build --from-db --as-of "${AS_OF}"

echo "[sf_finish] audit_billing mounted"
SRC=/data/exports/side_by_side_case/extracted
OUT=/data/exports/side_by_side_case/reports/audit
sudo mkdir -p "${OUT}"
sudo chmod a+rwx "${OUT}" || true
run_worker python /app/webpt_edco_scraper/scripts/audit_billing.py \
  --extracted "${SRC}" \
  --out "${OUT}"

echo "[sf_finish] verify"
run_worker python -c 'from cashflow_db.repository import connection
with connection() as conn:
  print("kpi", conn.execute("SELECT COUNT(*) AS n FROM analytics.snowflake_visit_kpi").fetchone()["n"])
  for r in conn.execute("SELECT reconciliation_run_id::text AS id, status, created_at, row_count FROM billing.reconciliation_run ORDER BY created_at DESC LIMIT 2").fetchall():
    print("recon", dict(r))
  for r in conn.execute("SELECT forecast_run_id::text AS id, status, created_at, as_of_date FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("fc", dict(r))
  rid=conn.execute("SELECT forecast_run_id FROM analytics.forecast_run WHERE status=%s ORDER BY created_at DESC LIMIT 1", ("success",)).fetchone()
  if rid:
    n=conn.execute("SELECT COUNT(*) AS n FROM analytics.forecast_prediction WHERE forecast_run_id=%s", (rid["forecast_run_id"],)).fetchone()
    print("pred_rows", n["n"])
'

echo "[sf_finish] $(date -Is) done"
ls -la /data/exports/side_by_side_case/reports/audit 2>/dev/null | head -20 || true
