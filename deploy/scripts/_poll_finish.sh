#!/usr/bin/env bash
set -euo pipefail
LOG=/data/logs/sf_audit_forecast_2026-08-10_finish.log
for i in $(seq 1 180); do
  if grep -q '\[sf_finish\].* done' "$LOG" 2>/dev/null; then
    echo FINISHED_OK
    tail -100 "$LOG"
    exit 0
  fi
  if ! pgrep -f run_sf_finish.sh >/dev/null; then
    echo FINISHED_EARLY
    tail -100 "$LOG" || true
    exit 1
  fi
  echo "wait_$i $(date +%H:%M:%S)"
  docker ps --format '{{.Names}} {{.Status}}' | grep worker || true
  tail -2 "$LOG" || true
  sleep 30
done
echo TIMEOUT
tail -50 "$LOG" || true
exit 1
