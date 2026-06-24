import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
EDOCS_DIR = OUTPUT_DIR / "edocs"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"

BASE_URL = "https://app.webpt.com"
DASHBOARD_URL = f"{BASE_URL}/dashboard.php"
LOGIN_ENTRY_URL = DASHBOARD_URL
PATIENT_DISPLAY_URL = f"{BASE_URL}/patient/display/"
GATEWAY_GRAPHQL_URL = "https://gateway.webpt.com/graphql"

GET_PATIENTS_URL = f"{BASE_URL}/patient/display/getpatients"
GET_NEW_PATIENTS_URL = f"{BASE_URL}/patient/display/getnewpatients"
GET_DOCUMENTS_PER_CASE_URL = f"{BASE_URL}/edoc/edoc/getdocumentspercase"
GET_ALL_DOCUMENTS_URL = f"{BASE_URL}/edoc/edoc/getalldocuments"
VIEW_EXT_DOC_URL = f"{BASE_URL}/viewExtDoc.php"

SCHEDULER_INDEX_URL = f"{BASE_URL}/scheduler/index"
SCHEDULER_DATA_URL = f"{BASE_URL}/scheduler/index/data/T/e"
PATIENT_CHART_URL = f"{BASE_URL}/patientChart.php"
PATIENT_CHART_NOTE_URL = f"{BASE_URL}/patientChartNote.php"
PRINT_PDF_URL = f"{BASE_URL}/printPDF.php"

DEFAULT_TIMEZONE = "US/Eastern"
DEFAULT_PATIENT_PAGE_SIZE = 50
DEFAULT_PDF_DELAY_SEC = 2.0
DEFAULT_PDF_TIMEOUT_SEC = 60.0
DEFAULT_ACTION_DELAY_SEC = 1.5
DEFAULT_CHART_TIMEOUT_SEC = 90.0
DEFAULT_LOGIN_TIMEOUT_SEC = 120
DEFAULT_COMPANY_ID = "13829"
DEFAULT_OCR_ENABLED = True
DEFAULT_OCR_DPI = 200


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass
class WebPTConfig:
    username: str
    password: str
    company_id: str = DEFAULT_COMPANY_ID
    headless: bool = False
    pdf_delay_sec: float = DEFAULT_PDF_DELAY_SEC
    pdf_timeout_sec: float = DEFAULT_PDF_TIMEOUT_SEC
    action_delay_sec: float = DEFAULT_ACTION_DELAY_SEC
    chart_timeout_sec: float = DEFAULT_CHART_TIMEOUT_SEC
    patient_page_size: int = DEFAULT_PATIENT_PAGE_SIZE
    timezone: str = DEFAULT_TIMEZONE
    ocr_enabled: bool = DEFAULT_OCR_ENABLED
    ocr_dpi: int = DEFAULT_OCR_DPI
    tesseract_cmd: str = ""

    @classmethod
    def from_env(cls) -> "WebPTConfig":
        username = os.getenv("WEBPT_USERNAME", "").strip()
        password = os.getenv("WEBPT_PASSWORD", "").strip()
        if not username or not password:
            raise ValueError(
                "WEBPT_USERNAME and WEBPT_PASSWORD must be set in .env or environment"
            )
        headless_raw = os.getenv("WEBPT_HEADLESS", "false").strip().lower()
        return cls(
            username=username,
            password=password,
            company_id=os.getenv("WEBPT_COMPANY_ID", DEFAULT_COMPANY_ID).strip(),
            headless=headless_raw in ("1", "true", "yes"),
            pdf_delay_sec=_float_env("WEBPT_PDF_DELAY_SEC", DEFAULT_PDF_DELAY_SEC),
            pdf_timeout_sec=_float_env("WEBPT_PDF_TIMEOUT_SEC", DEFAULT_PDF_TIMEOUT_SEC),
            action_delay_sec=_float_env("WEBPT_ACTION_DELAY_SEC", DEFAULT_ACTION_DELAY_SEC),
            chart_timeout_sec=_float_env(
                "WEBPT_CHART_TIMEOUT_SEC", DEFAULT_CHART_TIMEOUT_SEC
            ),
            patient_page_size=_int_env("WEBPT_PATIENT_PAGE_SIZE", DEFAULT_PATIENT_PAGE_SIZE),
            timezone=os.getenv("WEBPT_TIMEZONE", DEFAULT_TIMEZONE).strip(),
            ocr_enabled=os.getenv("WEBPT_OCR_ENABLED", "true").strip().lower()
            in ("1", "true", "yes"),
            ocr_dpi=_int_env("WEBPT_OCR_DPI", DEFAULT_OCR_DPI),
            tesseract_cmd=os.getenv("WEBPT_TESSERACT_CMD", "").strip(),
        )
