#!/usr/bin/env bash
set -euo pipefail
docker exec cashflow-postgres-1 psql -U cashflow -d cashflow -c \
  "SELECT reconciliation_run_id::text, status, row_count, created_at, finished_at
   FROM billing.reconciliation_run
   ORDER BY created_at DESC
   LIMIT 5;"
