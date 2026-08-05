# Deprecation Plan — Intermediate CSV Artifacts

Operational Data Platform migration. Scrapers may still emit files for ingest; this table covers **product-path** intermediates only.

| Artifact | Current | Replacement | Remove When | Status |
|----------|---------|-------------|-------------|--------|
| `reconciliation_lines.csv` | Active SoT | `billing.reconciliation_line` + `repo.reconciliation` | After PR C + D1 | Deprecated — use `reconcile --from-db` |
| `reconciliation_visits/patients.csv` | Active | spine aggs / marts | After PR C | Deprecated |
| `payments_unified.csv` | Active | eob/deposit facts + repo | After PR C | Deprecated |
| `unmatched_*.csv` | Active | `mart.v_unmatched_*` | After PR C | Deprecated |
| `insurance_behavior/*.csv` | Active | `analytics.payor_behavior_*` + `repo.insurance` | After PR C | Deprecated |
| Product reads of `extracted/*.csv` | Active | `clinical_note` / `visit_service_line` / `plan_of_care_detail` | After PR D1 | Deprecated — `forecast build --from-db` |
| Waystar CSV in product path | Active | `billing.denial_record` | After PR D1 | Deprecated |
| Tracker XLSX in product path | Active | `billing.bank_deposit` | After PR D1 | Deprecated |
| `payer_sla.csv` | Active | `analytics.forecast_feature` | After PR D2 | Deprecated |
| `risk_flags.csv` | Active | forecast features / prediction | After PR D2 | Deprecated |
| `*_cash_*.csv` | Active | marts + forecast write | After PR D2/D3 | Deprecated |
| `outcome_stages.csv` as SoT | Active | `forecast_prediction` | After PR D2/D3 | Deprecated |
| `load_forecast_from_csv` in load-all | Was default | forecast `--from-db` write | After PR D2 | Removed from default `load-all` (escape hatch: `--with-forecast-csv`) |
| API `FORECAST_DIR` SoT | Active | repository (`CASHFLOW_FORECAST_FROM_DB=1` default) | After PR D3 | DB default; set `=0` for CSV legacy |

Diagnostic `--emit-csv` remains available on reconcile/forecast; it must not be the source of truth.
