#!/usr/bin/env bash
set -euo pipefail
LOG=/data/logs/sf_audit_forecast_2026-08-10.log
while pgrep -f run_sf_audit_forecast.sh >/dev/null; do
  echo "$(date -Is) still running"
  docker ps --format '{{.Names}} {{.Status}}' | grep worker || true
  tail -5 "$LOG" || true
  sleep 60
done
echo DONE
tail -120 "$LOG" || true
