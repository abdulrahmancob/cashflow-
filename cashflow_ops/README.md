# cashflow_ops — RCM Processing Platform

Workflow engine + platform qualities (metrics, events, SLA, backfill, maintenance, feature store stage).

See **[docs/rcm_platform.md](../docs/rcm_platform.md)**.

```bash
python -m cashflow_db migrate
python -m cashflow_ops run --dry-run --skip-scrapers
python -m cashflow_ops run --from 2026-06-01 --to 2026-06-30
python -m cashflow_ops backfill resume --backfill-id <uuid>
python -m cashflow_ops metrics --run-id <uuid>
python -m cashflow_ops events --run-id <uuid>
```

After `reconciliation`, stage `eligibility_queue` upserts `ops.eligibility_work_item` rows (ops edits preserved). Manual:

```bash
python -m cashflow_db generate-eligibility --from-csv
python -m cashflow_db bootstrap-admin   # if empty auth.app_user
```

Portal UI: [`rcm_portal/`](../rcm_portal/) (JWT via `/api/auth/login`).
