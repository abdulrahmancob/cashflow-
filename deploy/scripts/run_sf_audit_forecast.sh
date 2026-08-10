#!/usr/bin/env bash
# Park WebPT gap path: load SF KPI -> reconcile -> audit -> forecast --from-db
# Uses host-mounted cashflow_forecast overrides for SF paid/denied in from-db build.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
cd "${ROOT}"

BILLING_CSV="${SNOWFLAKE_BILLING_CSV:-/data/exports/side_by_side_case/reports/billing_2026-01-01_to_now.csv}"
AS_OF="${AS_OF:-$(date +%F)}"
LOG="${DATA_ROOT:-/data}/logs/sf_audit_forecast_${AS_OF}.log"
mkdir -p "$(dirname "$LOG")"

echo "[sf_audit_forecast] $(date -Is) start as_of=${AS_OF} billing=${BILLING_CSV}" | tee -a "$LOG"

run_worker() {
  docker compose --env-file "${ROOT}/.env" --profile tools run --rm -T \
    -e "SNOWFLAKE_BILLING_CSV=${BILLING_CSV}" \
    -v "${REPO}/cashflow_forecast:/app/cashflow_forecast:ro" \
    worker "$@"
}

echo "[sf_audit_forecast] load-snowflake-kpi" | tee -a "$LOG"
run_worker python -m cashflow_db load-snowflake-kpi 2>&1 | tee -a "$LOG"

echo "[sf_audit_forecast] reconcile_from_db" | tee -a "$LOG"
run_worker python - <<'PY' 2>&1 | tee -a "$LOG"
from cashflow_ops.adapters import reconcile
summary = reconcile.reconcile_from_db(dry_run=False)
print(summary)
PY

echo "[sf_audit_forecast] audit_billing" | tee -a "$LOG"
run_worker python - <<'PY' 2>&1 | tee -a "$LOG"
from cashflow_ops.adapters import reconcile
r = reconcile.audit_billing(dry_run=False)
print({"ok": r.ok, "returncode": r.returncode, "skipped": getattr(r, "skipped", False)})
print((r.stdout or "")[-2000:])
if not r.ok and not getattr(r, "skipped", False):
    print((r.stderr or "")[-2000:])
    raise SystemExit(r.returncode or 1)
PY

echo "[sf_audit_forecast] insurance_behavior --from-db" | tee -a "$LOG"
run_worker python -m cashflow_reconcile.insurance_behavior --from-db 2>&1 | tee -a "$LOG"

echo "[sf_audit_forecast] forecast build --from-db as_of=${AS_OF}" | tee -a "$LOG"
run_worker python -m cashflow_forecast build --from-db --as-of "${AS_OF}" 2>&1 | tee -a "$LOG"

echo "[sf_audit_forecast] $(date -Is) done" | tee -a "$LOG"
