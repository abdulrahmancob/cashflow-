#!/usr/bin/env bash
set -euo pipefail
sed -i 's/\r$//' /tmp/run_sf_audit_forecast_tail.sh
sudo cp /tmp/run_sf_audit_forecast_tail.sh /opt/cashflow/deploy/scripts/
sudo chmod +x /opt/cashflow/deploy/scripts/run_sf_audit_forecast_tail.sh

# Stop redundant full resume (re-reconcile)
pkill -f 'run_sf_audit_forecast_resume.sh' 2>/dev/null || true
# Stop any leftover worker one-shots from resume reconcile
docker ps --format '{{.Names}}' | grep 'cashflow-worker-run-' | while read -r n; do
  docker stop "$n" >/dev/null 2>&1 || true
done

sleep 2
nohup bash /opt/cashflow/deploy/scripts/run_sf_audit_forecast_tail.sh \
  >/data/logs/sf_audit_forecast_tail_nohup.out 2>&1 &
echo "STARTED_PID=$!"
sleep 3
tail -30 /data/logs/sf_audit_forecast_2026-08-10_tail.log 2>/dev/null || \
  cat /data/logs/sf_audit_forecast_tail_nohup.out
