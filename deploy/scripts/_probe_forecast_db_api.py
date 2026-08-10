#!/usr/bin/env python3
from cashflow_forecast.db_source import load_feature_df, load_outcome_stages_latest_df
from cashflow_forecast.api import _cached_outcomes, _outcomes_cache_key, _use_db, kpi

print("use_db", _use_db())
o = load_outcome_stages_latest_df()
print("outcomes_rows", len(o))
print("outcomes_cols", list(o.columns)[:25] if not o.empty else [])
if not o.empty:
    print("expected_sum", float(o["expected_amount"].sum()) if "expected_amount" in o.columns else None)
    print("stages", o["outcome_stage"].value_counts().head(10).to_dict() if "outcome_stage" in o.columns else None)
    print("has_facility", "facility_name" in o.columns)
    if "payload" in o.columns:
        sample = o.iloc[0]["payload"]
        print("payload_type", type(sample), str(sample)[:200])

k = load_feature_df("kpi_summary")
print("kpi_feature_rows", len(k))
if not k.empty:
    print("kpi_feature_cols", list(k.columns))
    print("kpi_feature_head", k.head(1).to_dict())

# clear cache and call kpi endpoint function
_cached_outcomes.cache_clear()
print("cache_key", _outcomes_cache_key())
print("kpi_api", kpi())
