# cashflow_db — Operational Data Platform

Normalized PostgreSQL schema + ETL + **repository contracts** for PT of the City RCM.

**Principle:** CSV/XLSX are ingest-only (scrapers → loaders). Product paths (`cashflow_reconcile`, `cashflow_forecast`, API) read/write via `cashflow_db.repository` — not ad-hoc SQL or intermediate CSV SoT.

## Identity rules (case-centric)

- **Clinical visit** `core.visit`: one row per `(case_pk, service_date)`.
- **Scheduler slots** `core.schedule_appointment`: first-class grain `(case_pk, appointment_at)`.
- Same-day multiple appointments → one clinical visit (Checked Out → note → CPT → longest chair → earliest).
- Blank `case_id` is **fail-closed**.
- Notes/CPT load from **case-aware** extracts; use `enrich-case-extracts` to expand volume from legacy+schedule.
- Snowflake is **KPI staging only** (`analytics.snowflake_visit_kpi`).

## Quick start

```bash
pip install -r cashflow_db/requirements.txt
# set CASHFLOW_DATABASE_URL in cashflow_db/.env
createdb cashflow
python -m cashflow_db migrate
python -m cashflow_db enrich-case-extracts   # optional: grow case notes/CPT
python -m cashflow_db load-all --limit 50    # smoke
python -m cashflow_db load-all
python -m cashflow_db validate               # warehouse assertions gate
python -m cashflow_reconcile --from-db
python -m cashflow_forecast build --from-db
```

## Layout

| Path | Purpose |
|------|---------|
| `sql/` | Migrations through `017_eligibility_ops.sql` (auth + eligibility work queue) |
| `loaders/` | Ingest-only ETL from scraper files |
| `repository/` | Stable read/write contracts for all consumers |
| `validate_warehouse.py` | Source↔DB data assertions |
| `scripts/enrich_case_extracts.py` | Attach case_id when uniquely determined |
| `../docs/erd.md` | ERD |
| `../docs/data_coverage_audit.md` | Architecture certification |
| `../docs/deprecation_plan.md` | Intermediate CSV retirement |

## Repository

```python
from cashflow_db.repository import connection, reconciliation, forecast, payments

with connection() as conn:
    lines = reconciliation.get_lines(conn)
```

Modules: `visits`, `payments`, `claims`, `reconciliation`, `forecast`, `insurance`.

## Run lineage

- `etl.etl_run` — each loader
- `billing.reconciliation_run` — match run + `source_etl_run_ids`
- `analytics.forecast_run` — predictions + `reconciliation_run_id`, `rules_version`, `source_etl_run_ids`

## Default source paths

| Env | Default |
|-----|---------|
| `WEBPT_OUTPUT_DIR` | `webpt_edco_scraper/output/jan_aug_2026` |
| `WEBPT_LEGACY_OUTPUT_DIR` | `.../jun_jul_2026` |
| `CASE_PIPELINE_DIR` | `snowflake_pull/artifacts/side_by_side_case` |
| `SCHEDULE_VISITS_CSV` / `PATIENT_PAYMENTS_CSV` | under WEBPT_OUTPUT |
| `REVFLOW_OUTPUT_DIR` | `revflow_scraper/output/jan_jul_2026` |
| `TRACKER_XLSX` | `webpt_edco_scraper/Transaction Tracker 2026.xlsx` |
| `WAYSTAR_REJECTIONS_CSV` / `WAYSTAR_DENIALS_DIR` | under `waystar_scraper/output` |

## CLI

```text
python -m cashflow_db migrate
python -m cashflow_db load-all [--limit N] [--with-forecast-csv]
python -m cashflow_db load-waystar
python -m cashflow_db validate
python -m cashflow_db enrich-case-extracts
```

`load-all` order: rules → schedule → webpt → patient_payments → revflow → tracker → mail → snowflake_kpi → waystar.  
Forecast CSV import is **not** in the default load-all (use `forecast build --from-db` or `--with-forecast-csv`).
