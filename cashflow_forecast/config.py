"""Paths, thresholds, and forecast defaults."""

from __future__ import annotations

from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Audit ↔ Waystar match scoring (0–100)
MATCH_NAME_EXACT = 40
MATCH_DOS_EXACT = 30
MATCH_DOS_NEAR = 15  # ±1 day; not stacked with exact
MATCH_PAYER = 20
MATCH_CPT = 10
MATCH_ACCEPT_THRESHOLD = 70
MATCH_REVIEW_THRESHOLD = 50

# Payer SLA
GLOBAL_MEDIAN_LAG_DAYS = 14
MIN_SLA_SAMPLES = 3
MAX_LAG_DAYS = 365
OVERDUE_BUFFER_DAYS = 3

# Forecast adjustments
RESUBMISSION_LAG_DAYS = 30
SUBMISSION_WINDOW_DAYS = 14  # after DOS, still waiting to appear in Waystar

# Default as-of date for pilot (override via CLI)
DEFAULT_AS_OF = date(2026, 7, 9)
