#!/usr/bin/env bash
set -euo pipefail
NAME=$(docker ps -a --filter name=cashflow-api-run --format '{{.Names}}' | head -1)
echo "watching $NAME"
while docker inspect "$NAME" --format '{{.State.Running}}' 2>/dev/null | grep -q true; do
  echo "still $(date -u +%H:%M:%S)"
  sleep 40
done
echo DONE
docker logs --tail 50 "$NAME" 2>&1 || true
bash /tmp/check_latest_recon.sh
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT pending_reason, COUNT(*) FROM billing.reconciliation_visit_agg
   WHERE reconciliation_run_id = (
     SELECT reconciliation_run_id FROM billing.reconciliation_run
     ORDER BY created_at DESC LIMIT 1)
   GROUP BY 1 ORDER BY 2 DESC;"
