#!/usr/bin/env bash
set -euo pipefail
sed -i 's/\r$//' /tmp/run_audit_billing_mounted.sh
sudo cp /tmp/run_audit_billing_mounted.sh /opt/cashflow/deploy/scripts/
sudo chmod +x /opt/cashflow/deploy/scripts/run_audit_billing_mounted.sh

LOG=/data/logs/sf_audit_forecast_2026-08-10_tail.log
for i in $(seq 1 120); do
  if grep -q 'sf_audit_forecast_tail].* done' "$LOG" 2>/dev/null; then
    echo "TAIL_DONE"
    break
  fi
  if ! pgrep -f run_sf_audit_forecast_tail.sh >/dev/null; then
    echo "TAIL_EXITED"
    tail -40 "$LOG" || true
    break
  fi
  echo "wait_$i $(date +%H:%M:%S)"
  docker ps --format '{{.Names}} {{.Status}}' | grep worker || true
  tail -3 "$LOG" || true
  sleep 30
done

echo "=== tail log end ==="
tail -80 "$LOG" || true

echo "=== run audit mounted ==="
bash /opt/cashflow/deploy/scripts/run_audit_billing_mounted.sh
tail -40 /data/logs/audit_billing_mounted.log || true
