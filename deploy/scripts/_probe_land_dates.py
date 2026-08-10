#!/usr/bin/env python3
import pandas as pd
from cashflow_forecast.db_source import load_outcome_stages_latest_df

df = load_outcome_stages_latest_df()
for col in ("original_forecast_date", "forecast_date", "expected_pay_date"):
    s = pd.to_datetime(df[col], errors="coerce")
    print(col, "min", s.min(), "max", s.max(), "nulls", int(s.isna().sum()))
    print("  year_counts", s.dt.year.value_counts().head(8).to_dict())
