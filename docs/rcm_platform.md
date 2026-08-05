# RCM Processing Platform

Daily workflow engine for PT of the City RCM. **02:00 Africa/Cairo is only a trigger** — orchestration, state, recovery, monitoring, and platform qualities live in `cashflow_ops`.

## Quick start

```bash
python -m cashflow_db migrate

set CASHFLOW_OPS_DRY_RUN=1
python -m cashflow_ops run --trigger manual --dry-run --skip-scrapers

python -m cashflow_ops run --trigger task_scheduler
python -m cashflow_ops resume --run-id <uuid>

# Backfill with day-level resume
python -m cashflow_ops run --from 2026-06-01 --to 2026-06-30
python -m cashflow_ops backfill resume --backfill-id <uuid>
python -m cashflow_ops backfill status --backfill-id <uuid>

# Observability
python -m cashflow_ops metrics --run-id <uuid>
python -m cashflow_ops events --run-id <uuid>
python -m cashflow_ops status
python -m cashflow_ops snapshot --as-of 2026-08-04
```

## Stages (DAG)

```text
Acquire → Validate → Enrich → Warehouse → Reconcile → Analytics
  → FeatureStore → Forecast → Publish
```

| Key | Failure policy | Notes |
|-----|----------------|-------|
| `acquire` | stop | Maintenance skips per `monitoring.system_config` |
| `validate_sources` | stop | Hard gate + `quality_metric` history |
| `enrich_clinical` | continue_with_alert | OCR / retry queue |
| `load_warehouse` | stop | migrate → load-all → validate |
| `reconciliation` | stop | Match + insurance behavior + audit |
| `analytics` | continue_with_alert | KPI facts (not forecast) |
| `feature_store` | continue_with_alert | Writes `analytics.feature_snapshot` |
| `forecast` | stop | `--from-db`; prefers feature store |
| `publish_monitor` | continue_with_alert | Snapshot, accuracy, quality trend, notify |

## Platform qualities (migration 015)

Schema **`monitoring`**:

| Table | Role |
|-------|------|
| `pipeline_metric` | Typed metrics (`metric_type`, `entity_key`) |
| `pipeline_event` | Event log for debug timelines |
| `job_runtime` | Duration + `queue_wait_seconds` + SLA breach |
| `quality_metric` | History with `expected_value` / `threshold` / `status` |
| `quality_rule` | Threshold rules |
| `sla_definition` | DB-managed stage/facility SLAs |
| `system_config` | Maintenance mode (`auto` / `force_up` / `maintenance`) |
| `system_health` | Probes with `response_ms` |
| `backfill_run` / `backfill_day` | Resumable multi-day backfill |

Analytics feature store: `feature_definition` + `feature_snapshot`.

Every `ops.pipeline_run` gets `dataset_version` (`YYYY-MM-DD.N`), stamped onto recon/forecast/snapshot.

## API

```text
GET /api/v1/platform          # version, git_sha, schema_version, last_pipeline, status
GET /api/v1/ops/runs
GET /api/v1/ops/runs/{id}
GET /api/v1/ops/metrics?run_id=
GET /api/v1/ops/events?run_id=
GET /api/v1/ops/quality
GET /api/v1/ops/sla
GET /api/v1/ops/health
```

Legacy `/api/*` forecast routes are also aliased under `/api/v1/*`. Platform routes are mounted on both `/api` and `/api/v1`.

## SLA

Success-but-slow is a failure. Limits live in `monitoring.sla_definition` (seeded; editable without redeploy). Facility overrides: `scope_type=facility`, `scope_key=facility:30874`.

## Maintenance

```sql
UPDATE monitoring.system_config SET mode = 'maintenance' WHERE system_key = 'webpt';
```

Acquire skips that system; Validate Sources treats it as explicit skip (no hard-fail).

## Design rules

- Warehouse + `ops` + `monitoring` are platform SoT — not CSV folders.
- Snowflake is KPI-only.
- WebPT session is single-session.
- Forecast requires Feature Store stage (then Analytics → FeatureStore → Forecast).
