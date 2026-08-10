#!/usr/bin/env bash
# Skip reconcile (already success today). audit -> insurance_behavior -> forecast.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"

AS_OF="${AS_OF:-$(date +%F)}"
LOG="${DATA_ROOT:-/data}/logs/sf_audit_forecast_${AS_OF}_tail.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "[sf_audit_forecast_tail] $(date -Is) start as_of=${AS_OF}"

run_worker() {
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
    -e "PYTHONPATH=/app" \
    -v "${REPO}/cashflow_forecast:/app/cashflow_forecast:ro" \
    -w /app \
    worker "$@"
}

echo "[sf_audit_forecast_tail] audit_billing"
run_worker python -c 'from cashflow_ops.adapters import reconcile; import sys; r=reconcile.audit_billing(dry_run=False); print({"ok": r.ok, "returncode": r.returncode, "skipped": getattr(r, "skipped", False)}); print((r.stdout or "")[-2000:]); print((r.stderr or "")[-1000:]); sys.exit(0 if r.ok or getattr(r, "skipped", False) else (r.returncode or 1))'

echo "[sf_audit_forecast_tail] insurance_behavior --from-db"
run_worker python -m cashflow_reconcile.insurance_behavior --from-db

echo "[sf_audit_forecast_tail] forecast build --from-db as_of=${AS_OF}"
run_worker python -m cashflow_forecast build --from-db --as-of "${AS_OF}"

echo "[sf_audit_forecast_tail] db snapshot after"
run_worker python -c 'from cashflow_db.repository import connection
with connection() as conn:
  for r in conn.execute("SELECT forecast_run_id::text AS id, status, created_at, as_of_date FROM analytics.forecast_run ORDER BY created_at DESC LIMIT 3").fetchall():
    print("fc", dict(r))
  rid = conn.execute("SELECT forecast_run_id FROM analytics.forecast_run WHERE status=%s ORDER BY created_at DESC LIMIT 1", ("success",)).fetchone()
  if rid:
    n=conn.execute("SELECT COUNT(*) AS n FROM analytics.forecast_prediction WHERE forecast_run_id=%s", (rid["forecast_run_id"],)).fetchone()
    print("pred_rows", n["n"])
  else:
    print("pred_rows", 0)
'

echo "[sf_audit_forecast_tail] $(date -Is) done"
