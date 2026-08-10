#!/usr/bin/env python3
from cashflow_forecast.api import (
    _cached_outcomes,
    _kpi_summary_base,
    _latest_forecast_run_stamp,
    kpi,
    projected_monthly,
    projected_by_facility,
)

print("stamp", _latest_forecast_run_stamp())
base = _kpi_summary_base()
print("base_projected", base.get("projected_cash_in"), "base_actual", base.get("actual_cash_received"))
_cached_outcomes.cache_clear()
k = kpi()
print(
    {
        "projected_cash_in": k.get("projected_cash_in"),
        "actual_cash_received": k.get("actual_cash_received"),
        "on_track_amount": k.get("on_track_amount"),
        "overdue_amount": k.get("overdue_amount"),
    }
)
m = projected_monthly()
print("monthly_n", len(m), "monthly_head", m[:3] if m else [])
f = projected_by_facility()
print("facility_n", len(f), "facility_head", f[:3] if f else [])
