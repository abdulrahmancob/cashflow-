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

# Payer-plan payment model / capacity pack
MIN_PLAN_SAMPLES = 30
MIN_CLASS_SAMPLES = 20
MIN_ORG_SAMPLES = 15
MIN_CAP_SAMPLES = 3
ROLLING_DEPOSIT_EVENTS = 8
CAPACITY_SCALE_ALPHA = 0.5
CAPACITY_SCALE_MIN = 0.75
CAPACITY_SCALE_MAX = 2.0
PACK_HORIZON_WEEKS = 16
# Normal open-AR proxy ≈ this many days of Cap_base deposits
BASELINE_DAYS_OF_DEPOSITS = 25
# Weekday landing target = median of last N total deposit days for that weekday
WEEKDAY_TARGET_EVENTS = 8
# When Cap_base missing: small slice of the day's Target
FALLBACK_TARGET_FRAC = 0.01
FALLBACK_TARGET_FLOOR = 200.0
# Layer2 soft penalty when sum(raw caps) > Target: norm = (Target/raw)^α
# α=1 is hard clamp to Target; α<1 pulls toward Target but allows exceptional days.
CAPACITY_SOFT_PENALTY_ALPHA = 0.5
CAPACITY_DRIFT_NORM_WARN = 0.6
FLAT_VISIT_TOLERANCE = 1.0
FLAT_VISIT_MODE_SHARE = 0.70

# Default as-of date for pilot (override via CLI)
DEFAULT_AS_OF = date(2026, 7, 17)
