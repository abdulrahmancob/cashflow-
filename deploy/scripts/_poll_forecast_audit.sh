#!/usr/bin/env bash
set -euo pipefail
LOG=/data/logs/sf_forecast_audit_2026-08-10.log
# ensure process is running
if ! pgrep -f run_sf_forecast_audit.sh >/dev/null; then
  nohup bash /opt/cashflow/deploy/scripts/run_sf_forecast_audit.sh >/data/logs/sf_forecast_audit_nohup.out 2>&1 &
  echo restarted_pid=$!
  sleep 2
fi
for i in $(seq 1 180); do
  if grep -q '\[sf_forecast_audit\].* done' "$LOG" 2>/dev/null; then
    echo FINISHED_OK
    tail -80 "$LOG"
    exit 0
  fi
  if ! pgrep -f run_sf_forecast_audit.sh >/dev/null; then
    echo FINISHED_EARLY
    tail -100 "$LOG" || true
    exit 1
  fi
  echo "wait_$i $(date +%H:%M:%S)"
  docker ps --format '{{.Names}} {{.Status}}' | grep worker || true
  tail -3 "$LOG" || true
  sleep 30
done
echo TIMEOUT
tail -50 "$LOG" || true
exit 1
