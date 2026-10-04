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
PT_CITY_PATIENT_CSV = Path(
    os.getenv(
        "PT_CITY_PATIENT_CSV",
        str(
            _first_existing(
                Path("/data/exports/snowflake/pt_city/patient_account_emr.csv"),
                _REPO / "snowflake_pull" / "output" / "pt_city" / "patient_account_emr.csv",
            )
        ),
    )
)
PT_CITY_VISIT_CSV = Path(
    os.getenv(
        "PT_CITY_VISIT_CSV",
        str(
            _first_existing(
                Path("/data/exports/snowflake/pt_city/visit_2026.csv"),
                _REPO / "snowflake_pull" / "output" / "pt_city" / "visit_2026.csv",
            )
        ),
    )
)
PT_CITY_CHARGES_DIR = Path(
    os.getenv(
        "PT_CITY_CHARGES_DIR",
        str(
            Path("/data/exports/snowflake/pt_city/charges")
            if Path("/data/exports/snowflake/pt_city/charges").is_dir()
            else _REPO / "snowflake_pull" / "output" / "pt_city" / "charges"
        ),
    )
)
PT_CITY_CHARGES_COMPARE_JSON = Path(
    os.getenv(
        "PT_CITY_CHARGES_COMPARE_JSON",
        str(PT_CITY_CHARGES_DIR / "compare.json"),
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


def _default_checks_deposits_csv() -> Path:
    candidates = [
        Path("/data/webpt/checks_and_deposits_ptoc.csv"),
        _REPO / "webpt_edco_scraper" / "checks_and_deposits_ptoc.csv",
    ]
    scraper_dir = _REPO / "webpt_edco_scraper"
    if scraper_dir.is_dir():
        candidates.extend(sorted(scraper_dir.glob("*Checks*Deposits*.csv")))
        candidates.extend(sorted(scraper_dir.glob("*checks*deposits*.csv")))
    return _first_existing(*candidates)


CHECKS_DEPOSITS_CSV = Path(
    os.getenv("CHECKS_DEPOSITS_CSV", str(_default_checks_deposits_csv()))
)


def _default_checks_deposits_xlsx() -> Path:
    scraper_dir = _REPO / "webpt_edco_scraper"
    named = (
        "Checks and Deposits - PT of The Cihttps___drive.google.com_open_id="
        "1uWqh1uJfRtYYHGO9ywJZO1GpthIBXsse&usp=drive_copy (1).xlsx"
    )
    candidates = [
        Path("/data/webpt") / named,
        scraper_dir / named,
    ]
    if scraper_dir.is_dir():
        matches = list(scraper_dir.glob("*Checks*Deposits*.xlsx"))
        matches.sort(key=lambda path: (0 if "(1)" in path.name else 1, path.name))
        candidates.extend(matches)
    return _first_existing(*candidates)


CHECKS_DEPOSITS_XLSX = Path(
    os.getenv("CHECKS_DEPOSITS_XLSX", str(_default_checks_deposits_xlsx()))
)
ICD_DENIAL_XLSX = Path(
    os.getenv(
        "ICD_DENIAL_XLSX",
        str(_REPO / "webpt_edco_scraper" / "ICD10_Denial_Management.xlsx"),
    )
)
ICD_DENIAL_RULES_YAML = Path(
    os.getenv(
        "ICD_DENIAL_RULES_YAML",
        str(_REPO / "webpt_edco_scraper" / "audit" / "icd_denial_rules.yaml"),
    )
)
ICD10_CATALOG_CSV = Path(
    os.getenv(
        "ICD10_CATALOG_CSV",
        str(_REPO / "webpt_edco_scraper" / "audit" / "data" / "icd10cm_catalog.csv"),
    )
)
PAYABLE_CPT_CSV = Path(
    os.getenv(
        "PAYABLE_CPT_CSV",
        str(_REPO / "webpt_edco_scraper" / "Payble CPT Codes - Shared PTOC - Payble CPT Codes.csv"),
    )
)
CPT_GUIDE_CSV = Path(
    os.getenv(
        "CPT_GUIDE_CSV",
        str(_REPO / "webpt_edco_scraper" / "CPT codes guid  - CPT codes.csv"),
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
WAYSTAR_CLAIMS_CSV = Path(
    os.getenv(
        "WAYSTAR_CLAIMS_CSV",
        str(
            _first_existing(
                Path("/data/waystar/claims_recent/claims_recent.json"),
                Path("/data/waystar/claims_listing_2026/claims_merged.json"),
                _REPO / "waystar_scraper" / "output" / "claims_recent" / "claims_recent.json",
                _REPO / "waystar_scraper" / "output" / "claims_listing_2026" / "claims_merged.json",
            )
        ),
    )
)
WEBPT_VISIT_RECON_XLSX = Path(
    os.getenv(
        "WEBPT_VISIT_RECON_XLSX",
        str(_REPO / "waystar_scraper" / "output" / "webpt_visit_recon" / "Table_8071.xlsx"),
    )
)

SQL_DIR = _PKG / "sql"
RULES_YAML = _PKG / "rules" / "business_rules.yaml"
