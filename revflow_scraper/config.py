import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"

LOGIN_URL = "https://billing.revflow.com/login"
BILLING_BASE_URL = "https://billing.revflow.com"
API_BASE_URL = "https://r6prodgoldna.revflow.com"

OPEN_835_REPORT_ID = 68
COMPANY_EOB_LOG_REPORT_ID = 66
EOB_DETAIL_REPORT_ID = 67

IP_REGISTRATION_LINK_RE = (
    r"https://billing\.revflow\.com/ipRegistration\?\?[a-f0-9]+"
)

DEFAULT_GMAIL_POLL_TIMEOUT_SEC = 300
DEFAULT_GMAIL_POLL_INTERVAL_SEC = 5
DEFAULT_EXPORT_DELAY_SEC = 3.5
DEFAULT_EXPORT_DELAY_JITTER_SEC = 1.5
DEFAULT_EXPORT_TIMEOUT_SEC = 120
DEFAULT_EXPORT_BUTTON_WAIT_SEC = 10.0
DEFAULT_ACTION_DELAY_SEC = 1.0
DEFAULT_EXPORT_RETRY_MAX = 2
DEFAULT_REAUTH_COOLDOWN_SEC = 15.0
DEFAULT_SESSION_REFRESH_EVERY_N = 25


@dataclass
class RevFlowConfig:
    username: str
    password: str
    company_id: str = ""
    user_id: str = ""
    clinic_code: str = "PV4"
    headless: bool = False
    gmail_credentials_path: Path = BASE_DIR / "credentials.json"
    gmail_token_path: Path = BASE_DIR / "gmail_token.json"
    gmail_poll_timeout_sec: int = DEFAULT_GMAIL_POLL_TIMEOUT_SEC
    gmail_poll_interval_sec: int = DEFAULT_GMAIL_POLL_INTERVAL_SEC
    export_delay_sec: float = DEFAULT_EXPORT_DELAY_SEC
    export_delay_jitter_sec: float = DEFAULT_EXPORT_DELAY_JITTER_SEC
    export_timeout_sec: float = DEFAULT_EXPORT_TIMEOUT_SEC
    export_button_wait_sec: float = DEFAULT_EXPORT_BUTTON_WAIT_SEC
    action_delay_sec: float = DEFAULT_ACTION_DELAY_SEC
    export_retry_max: int = DEFAULT_EXPORT_RETRY_MAX
    reauth_cooldown_sec: float = DEFAULT_REAUTH_COOLDOWN_SEC
    session_refresh_every_n: int = DEFAULT_SESSION_REFRESH_EVERY_N
    storage_state_path: Path = STORAGE_STATE_PATH

    @classmethod
    def from_env(cls) -> "RevFlowConfig":
        username = os.getenv("REVFLOW_USERNAME", "").strip()
        password = os.getenv("REVFLOW_PASSWORD", "").strip()
        if not username or not password:
            raise ValueError("REVFLOW_USERNAME and REVFLOW_PASSWORD must be set in .env")

        headless_raw = os.getenv("REVFLOW_HEADLESS", "false").strip().lower()
        return cls(
            username=username,
            password=password,
            company_id=os.getenv("REVFLOW_COMPANY_ID", "").strip(),
            user_id=os.getenv("REVFLOW_USER_ID", "").strip(),
            clinic_code=os.getenv("REVFLOW_CLINIC_CODE", "PV4").strip(),
            headless=headless_raw in {"1", "true", "yes"},
            gmail_credentials_path=Path(
                os.getenv("GMAIL_CREDENTIALS_PATH", str(BASE_DIR / "credentials.json"))
            ),
            gmail_token_path=Path(
                os.getenv("GMAIL_TOKEN_PATH", str(BASE_DIR / "gmail_token.json"))
            ),
            gmail_poll_timeout_sec=int(
                os.getenv("GMAIL_POLL_TIMEOUT_SEC", str(DEFAULT_GMAIL_POLL_TIMEOUT_SEC))
            ),
            gmail_poll_interval_sec=int(
                os.getenv("GMAIL_POLL_INTERVAL_SEC", str(DEFAULT_GMAIL_POLL_INTERVAL_SEC))
            ),
            export_delay_sec=float(
                os.getenv("REVFLOW_EXPORT_DELAY_SEC", str(DEFAULT_EXPORT_DELAY_SEC))
            ),
            export_delay_jitter_sec=float(
                os.getenv(
                    "REVFLOW_EXPORT_DELAY_JITTER_SEC",
                    str(DEFAULT_EXPORT_DELAY_JITTER_SEC),
                )
            ),
            export_timeout_sec=float(
                os.getenv("REVFLOW_EXPORT_TIMEOUT_SEC", str(DEFAULT_EXPORT_TIMEOUT_SEC))
            ),
            export_button_wait_sec=float(
                os.getenv(
                    "REVFLOW_EXPORT_BUTTON_WAIT_SEC",
                    str(DEFAULT_EXPORT_BUTTON_WAIT_SEC),
                )
            ),
            action_delay_sec=float(
                os.getenv("REVFLOW_ACTION_DELAY_SEC", str(DEFAULT_ACTION_DELAY_SEC))
            ),
            export_retry_max=int(
                os.getenv("REVFLOW_EXPORT_RETRY_MAX", str(DEFAULT_EXPORT_RETRY_MAX))
            ),
            reauth_cooldown_sec=float(
                os.getenv("REVFLOW_REAUTH_COOLDOWN_SEC", str(DEFAULT_REAUTH_COOLDOWN_SEC))
            ),
            session_refresh_every_n=int(
                os.getenv(
                    "REVFLOW_SESSION_REFRESH_EVERY_N",
                    str(DEFAULT_SESSION_REFRESH_EVERY_N),
                )
            ),
            storage_state_path=Path(
                os.getenv("REVFLOW_STORAGE_STATE_PATH", str(STORAGE_STATE_PATH))
            ),
        )
