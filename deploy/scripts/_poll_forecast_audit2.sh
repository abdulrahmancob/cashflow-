#!/usr/bin/env bash
set -euo pipefail
LOG=/data/logs/sf_forecast_audit_2026-08-10.log
for i in $(seq 1 120); do
  if grep -q '\[sf_forecast_audit\].* done' "$LOG" 2>/dev/null; then
    echo FINISHED_OK
    tail -100 "$LOG"
    exit 0
  fi
  if ! pgrep -f run_sf_forecast_audit.sh >/dev/null; then
    echo FINISHED_EARLY
    tail -120 "$LOG" || true
    exit 1
  fi
  echo "wait_$i $(date +%H:%M:%S)"
  docker top cashflow-worker-run-934e5f735823 -eo pcpu,etime,cmd 2>/dev/null | head -3 || \
    docker ps --format '{{.Names}} {{.Status}}' | grep worker || true
  tail -2 "$LOG" || true
  sleep 30
done
echo TIMEOUT
tail -50 "$LOG" || true
exit 1
