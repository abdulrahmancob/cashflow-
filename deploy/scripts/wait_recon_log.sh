#!/usr/bin/env bash
set -euo pipefail
# Wait until /tmp/recon_pending_reason.log contains OK or Traceback, or no api-run container
for i in $(seq 1 80); do
  if grep -q "OK run_id=" /tmp/recon_pending_reason.log 2>/dev/null; then
    echo SUCCESS
    tail -40 /tmp/recon_pending_reason.log
    bash /tmp/check_latest_recon.sh
    docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
      "SELECT pending_reason, COUNT(*) FROM billing.reconciliation_visit_agg
       WHERE reconciliation_run_id = (
         SELECT reconciliation_run_id FROM billing.reconciliation_run
         ORDER BY created_at DESC LIMIT 1)
       GROUP BY 1 ORDER BY 2 DESC;"
    exit 0
  fi
  if grep -q "Traceback" /tmp/recon_pending_reason.log 2>/dev/null; then
    echo FAILED
    tail -60 /tmp/recon_pending_reason.log
    exit 1
  fi
  echo "waiting $i $(date -u +%H:%M:%S)"
  sleep 30
done
echo TIMEOUT
tail -40 /tmp/recon_pending_reason.log || true
exit 2
