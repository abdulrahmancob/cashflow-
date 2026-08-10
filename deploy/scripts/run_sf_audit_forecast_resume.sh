#!/usr/bin/env bash
# Resume after KPI load: reconcile -> audit -> insurance_behavior -> forecast
# Detach-safe: run under nohup. Mounts host cashflow_forecast for SF from-db overrides.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"

BILLING_CSV="${SNOWFLAKE_BILLING_CSV:-/data/exports/side_by_side_case/reports/billing_2026-01-01_to_now.csv}"
AS_OF="${AS_OF:-$(date +%F)}"
LOG="${DATA_ROOT:-/data}/logs/sf_audit_forecast_${AS_OF}_resume.log"
mkdir -p "$(dirname "$LOG")"

exec >>"$LOG" 2>&1
echo "[sf_audit_forecast_resume] $(date -Is) start as_of=${AS_OF}"

run_worker() {
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
    -e "SNOWFLAKE_BILLING_CSV=${BILLING_CSV}" \
    -e "PYTHONPATH=/app" \
    -v "${REPO}/cashflow_forecast:/app/cashflow_forecast:ro" \
    -w /app \
    worker "$@"
}

echo "[sf_audit_forecast_resume] db snapshot before"
run_worker python -c 'from cashflow_db.repository import connection
with connection() as conn:
  print("kpi", conn.execute("SELECT COUNT(*) AS n FROM analytics.snowflake_visit_kpi").fetchone()["n"])
  for r in conn.execute("SELECT reconciliation_run_id::text AS id, status, created_at, finished_at, row_count FROM billing.reconciliation_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("recon", dict(r))
  for r in conn.execute("SELECT forecast_run_id::text AS id, status, created_at, as_of_date FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("fc", dict(r))
' || true

echo "[sf_audit_forecast_resume] reconcile_from_db"
run_worker python -c 'from cashflow_ops.adapters import reconcile; print(reconcile.reconcile_from_db(dry_run=False))'

echo "[sf_audit_forecast_resume] audit_billing"
run_worker python -c 'from cashflow_ops.adapters import reconcile; import sys; r=reconcile.audit_billing(dry_run=False); print({"ok": r.ok, "returncode": r.returncode, "skipped": getattr(r, "skipped", False)}); print((r.stdout or "")[-2000:]); sys.exit(0 if r.ok or getattr(r, "skipped", False) else (r.returncode or 1))'

echo "[sf_audit_forecast_resume] insurance_behavior --from-db"
run_worker python -m cashflow_reconcile.insurance_behavior --from-db

echo "[sf_audit_forecast_resume] forecast build --from-db as_of=${AS_OF}"
run_worker python -m cashflow_forecast build --from-db --as-of "${AS_OF}"

echo "[sf_audit_forecast_resume] db snapshot after"
run_worker python -c 'from cashflow_db.repository import connection
with connection() as conn:
  print("kpi", conn.execute("SELECT COUNT(*) AS n FROM analytics.snowflake_visit_kpi").fetchone()["n"])
  for r in conn.execute("SELECT reconciliation_run_id::text AS id, status, created_at, finished_at, row_count FROM billing.reconciliation_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("recon", dict(r))
  for r in conn.execute("SELECT forecast_run_id::text AS id, status, created_at, as_of_date FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("fc", dict(r))
' || true

echo "[sf_audit_forecast_resume] $(date -Is) done"
