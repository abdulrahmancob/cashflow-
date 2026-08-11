#!/usr/bin/env bash
set -euo pipefail
echo "== worker container processes =="
docker top cashflow-worker-run-729a4ace9126 -eo pid,pcpu,pmem,etime,cmd 2>/dev/null || \
  docker ps -a --filter name=worker --format '{{.Names}} {{.Status}}'
echo "== last 80 resume log =="
tail -n 80 /data/logs/manual_recovery_resume.log || true
echo "== docker logs worker (tail) =="
docker logs --tail 40 cashflow-worker-run-729a4ace9126 2>&1 || true
