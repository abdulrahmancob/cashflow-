"""Paths and runtime settings for cashflow_ops."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_PKG = Path(__file__).resolve().parent
_REPO = _PKG.parent

load_dotenv(_PKG / ".env")
load_dotenv(_REPO / ".env")
load_dotenv(_REPO / "cashflow_db" / ".env")

REPO_ROOT = _REPO
CAIRO_TZ = ZoneInfo("Africa/Cairo")

DATABASE_URL = os.getenv(
    "CASHFLOW_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/cashflow"),
)

DRY_RUN = os.getenv("CASHFLOW_OPS_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
SKIP_SCRAPERS = os.getenv("CASHFLOW_OPS_SKIP_SCRAPERS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEFAULT_LOOKBACK_DAYS = int(os.getenv("CASHFLOW_OPS_LOOKBACK_DAYS", "14"))
STAGE_STALE_SECONDS = int(os.getenv("CASHFLOW_OPS_STAGE_STALE_SEC", "1800"))
RETRY_DELAY_HOURS = float(os.getenv("CASHFLOW_OPS_RETRY_DELAY_HOURS", "1"))

WEBPT_DIR = Path(
    os.getenv("WEBPT_SCRAPER_DIR", str(_REPO / "webpt_edco_scraper"))
)
WEBPT_OUTPUT = Path(
    os.getenv(
        "WEBPT_OUTPUT_DIR",
        str(_REPO / "webpt_edco_scraper" / "output" / "jan_aug_2026"),
    )
)
WEBPT_LEGACY_OUTPUT = Path(
    os.getenv(
        "WEBPT_LEGACY_OUTPUT_DIR",
        str(_REPO / "webpt_edco_scraper" / "output" / "jun_jul_2026"),
    )
)
CASE_PIPELINE_DIR = Path(
    os.getenv(
        "CASE_PIPELINE_DIR",
        str(_REPO / "snowflake_pull" / "artifacts" / "side_by_side_case"),
    )
)
REVFLOW_DIR = Path(os.getenv("REVFLOW_SCRAPER_DIR", str(_REPO / "revflow_scraper")))
REVFLOW_OUTPUT = Path(
    os.getenv(
        "REVFLOW_OUTPUT_DIR",
        str(_REPO / "revflow_scraper" / "output" / "jan_jul_2026"),
    )
)
WAYSTAR_DIR = Path(os.getenv("WAYSTAR_SCRAPER_DIR", str(_REPO / "waystar_scraper")))
WAYSTAR_OUTPUT = Path(
    os.getenv("WAYSTAR_OUTPUT_DIR", str(_REPO / "waystar_scraper" / "output"))
)
SNOWFLAKE_DIR = Path(os.getenv("SNOWFLAKE_PULL_DIR", str(_REPO / "snowflake_pull")))
SNOWFLAKE_OUTPUT = Path(
    os.getenv("SNOWFLAKE_OUTPUT_DIR", str(_REPO / "snowflake_pull" / "output"))
)
TRACKER_XLSX = Path(
    os.getenv(
        "TRACKER_XLSX",
        str(_REPO / "webpt_edco_scraper" / "Transaction Tracker 2026.xlsx"),
    )
)
MAIL_CHECKS_CSV = Path(
    os.getenv(
        "MAIL_CHECKS_CSV",
        str(WEBPT_LEGACY_OUTPUT / "Copy of Mail - Checks$EOBS 22 - 25.csv"),
    )
)

OPS_ARTIFACTS_DIR = Path(
    os.getenv("CASHFLOW_OPS_ARTIFACTS_DIR", str(_PKG / "artifacts"))
)
NOTIFY_WEBHOOK_URL = os.getenv("CASHFLOW_OPS_NOTIFY_WEBHOOK", "").strip()
PYTHON = os.getenv("CASHFLOW_OPS_PYTHON", sys.executable)

# Validate Sources thresholds
SCHEDULE_DROP_STOP_PCT = float(os.getenv("CASHFLOW_OPS_SCHEDULE_DROP_STOP_PCT", "0.15"))
SCHEDULE_DROP_ALERT_PCT = float(os.getenv("CASHFLOW_OPS_SCHEDULE_DROP_ALERT_PCT", "0.05"))
PAYMENTS_DROP_ALERT_PCT = float(os.getenv("CASHFLOW_OPS_PAYMENTS_DROP_ALERT_PCT", "0.30"))
FORECAST_CASH_DROP_ALERT_PCT = float(
    os.getenv("CASHFLOW_OPS_FORECAST_CASH_DROP_ALERT_PCT", "0.40")
)
ENRICH_CRITICAL_FAIL_PCT = float(os.getenv("CASHFLOW_OPS_ENRICH_CRITICAL_FAIL_PCT", "0.50"))


def cairo_today() -> date:
    return datetime.now(CAIRO_TZ).date()


def window_dates(as_of: date, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> tuple[date, date]:
    """Inclusive scrape window: (as_of - lookback) .. as_of (yesterday end typical)."""
    end = as_of - timedelta(days=1)  # "yesterday" relative to run day
    start = end - timedelta(days=max(lookback_days - 1, 0))
    return start, end
