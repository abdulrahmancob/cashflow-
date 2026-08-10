#!/usr/bin/env python3
from cashflow_forecast.api import _cached_outcomes, _outcomes_cache_key, _land_date_col
from cashflow_forecast.db_source import load_outcome_stages_latest_df

_cached_outcomes.cache_clear()
df = load_outcome_stages_latest_df()
print("cols_has", {
    c: c in df.columns
    for c in (
        "forecast_date",
        "original_forecast_date",
        "expected_pay_date",
        "facility_name",
        "ins_name",
        "outcome_stage",
    )
})
land = _land_date_col(df)
print("land_col", land, "in_cols", land in df.columns)
if land in df.columns:
    print("land_nonnull", int(df[land].notna().sum()), "of", len(df))
    print("land_sample", df[land].dropna().head(3).tolist())
if "facility_name" in df.columns:
    print("fac_nonnull", int(df["facility_name"].notna().sum()))
    print("fac_sample", df["facility_name"].dropna().astype(str).head(5).tolist())
# payload keys sample
p0 = df.iloc[0]["payload"] if "payload" in df.columns else {}
print("payload_keys", sorted(p0.keys()) if isinstance(p0, dict) else type(p0))
