#!/usr/bin/env python3
import os
from pathlib import Path

from cashflow_forecast.api import _audit_dir, _forecast_dir

fd = _forecast_dir()
ad = _audit_dir()
print("FORECAST_ENV", os.getenv("CASHFLOW_FORECAST_DIR") or os.getenv("FORECAST_DIR"))
print("forecast_dir", fd)
print("forecast_exists", fd.is_dir())
print("kpi_summary", (fd / "kpi_summary.json").exists())
if fd.is_dir():
    print("files", sorted(p.name for p in fd.iterdir())[:30])
print("audit_dir", ad)
print("audit_exists", ad.is_dir())
