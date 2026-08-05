"""Paths and connection settings for cashflow_db."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PKG = Path(__file__).resolve().parent
_REPO = _PKG.parent

load_dotenv(_PKG / ".env")
load_dotenv(_REPO / ".env")

DATABASE_URL = os.getenv(
    "CASHFLOW_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/cashflow"),
)

WEBPT_OUTPUT = Path(
    os.getenv(
        "WEBPT_OUTPUT_DIR",
        str(_REPO / "webpt_edco_scraper" / "output" / "jan_aug_2026"),
    )
)
# Legacy edocs / audit / extracted (patient-collapsed) live under jun_jul.
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
def _first_existing(*candidates: Path) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


# Prefer Jan–Sep artifacts when Phase D extend lands; fall back to Jan–Aug.
SCHEDULE_VISITS_CSV = Path(
    os.getenv(
        "SCHEDULE_VISITS_CSV",
        str(
            _first_existing(
                WEBPT_OUTPUT / "schedule_visits_2026-01-01_2026-09-30.csv",
                WEBPT_OUTPUT / "schedule_visits_2026-01-01_2026-08-30.csv",
            )
        ),
    )
)
PATIENT_PAYMENTS_CSV = Path(
    os.getenv(
        "PATIENT_PAYMENTS_CSV",
        str(
            _first_existing(
                WEBPT_OUTPUT / "patient_payments_202601_202609.csv",
                WEBPT_OUTPUT / "patient_payments_202601_202608.csv",
            )
        ),
    )
)
SNOWFLAKE_BILLING_CSV = Path(
    os.getenv(
        "SNOWFLAKE_BILLING_CSV",
        str(_REPO / "snowflake_pull" / "output" / "billing_2026-01-01_to_2026-07-30.csv"),
    )
)
REVFLOW_OUTPUT = Path(
    os.getenv(
        "REVFLOW_OUTPUT_DIR",
        str(_REPO / "revflow_scraper" / "output" / "jan_jul_2026"),
    )
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
ICD_DENIAL_XLSX = Path(
    os.getenv(
        "ICD_DENIAL_XLSX",
        str(_REPO / "webpt_edco_scraper" / "ICD10_Denial_Management.xlsx"),
    )
)
PAYABLE_CPT_CSV = Path(
    os.getenv(
        "PAYABLE_CPT_CSV",
        str(_REPO / "webpt_edco_scraper" / "Payble CPT Codes - Shared PTOC - Payble CPT Codes.csv"),
    )
)
WAYSTAR_REJECTIONS_CSV = Path(
    os.getenv(
        "WAYSTAR_REJECTIONS_CSV",
        str(
            _REPO
            / "waystar_scraper"
            / "output"
            / "claims_rejected_all"
            / "claims_rejected_all_merged.csv"
        ),
    )
)
WAYSTAR_DENIALS_DIR = Path(
    os.getenv(
        "WAYSTAR_DENIALS_DIR",
        str(_REPO / "waystar_scraper" / "output" / "denials_2026_all"),
    )
)

SQL_DIR = _PKG / "sql"
RULES_YAML = _PKG / "rules" / "business_rules.yaml"
