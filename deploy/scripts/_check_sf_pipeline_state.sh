#!/usr/bin/env bash
set -euo pipefail
echo "=== processes ==="
pgrep -af 'run_sf_audit_forecast|cashflow_reconcile|cashflow_forecast|audit_billing' || echo none
echo "=== docker workers ==="
docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'worker|cashflow' | head -20 || true
echo "=== log tail ==="
tail -40 /data/logs/sf_audit_forecast_2026-08-10.log || true
echo "=== db snapshot ==="
cd /opt/cashflow/deploy
docker compose --env-file .env --profile tools run --rm -T worker python - <<'PY'
from cashflow_db.repository import connection
with connection() as conn:
    kpi = conn.execute("SELECT COUNT(*) AS n FROM analytics.snowflake_visit_kpi").fetchone()
    runs = conn.execute("""
        SELECT reconciliation_run_id::text, status, created_at, line_count, visit_count
        FROM billing.reconciliation_run
        ORDER BY created_at DESC LIMIT 3
    """).fetchall()
    fc = conn.execute("""
        SELECT forecast_run_id::text, status, as_of_date, created_at
        FROM analytics.forecast_run
        ORDER BY created_at DESC LIMIT 3
    """).fetchall()
    print("snowflake_visit_kpi", kpi["n"])
    print("recon_runs")
    for r in runs:
        print(dict(r))
    print("forecast_runs")
    for r in fc:
        print(dict(r))
PY
