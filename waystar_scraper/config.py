import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
PDFS_DIR = OUTPUT_DIR / "pdfs"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"

LOGIN_URL = "https://login.zirmed.com/UI/Login"
CLAIMS_LISTING_URL = "https://claims.zirmed.com/Claims/Listing/Index?appid=1"
PERFORM_SEARCH_URL = (
    "https://claims.zirmed.com/Claims/Listing/PerformSearch?explicitSearch=True"
)
GET_CHILD_CUSTS_URL = "https://claims.zirmed.com/Dashboard/Overview/GetChildCusts"
VIEW_CLAIM_PDF_URL = "https://claims.zirmed.com/Claims/History/ViewClaimPDF"

CLAIMS_PER_PAGE = 30
DEFAULT_TRANSACTION_DAYS = 30
DEFAULT_PAGE_DELAY_SEC = 2.0
DEFAULT_ACTION_DELAY_MIN = 1.0
DEFAULT_ACTION_DELAY_MAX = 2.5
DEFAULT_MFA_TIMEOUT_SEC = 600
DEFAULT_SLOW_MO_MS = 100
DEFAULT_CLAIM_FORM = "CMS1500_0212"
DEFAULT_PDF_DELAY_SEC = 1.5
DEFAULT_PDF_TIMEOUT_SEC = 60
PDF_EXTEND_SESSION_EVERY = 10
SESSION_EXTEND_URL = "https://general.zirmed.com/Session/Extend"
SESSION_EXTEND_EVERY_PAGES = 10
DEFAULT_BATCH_CLAIMS = 10_000
REJECTED_STATUS_CODE = "12345"
DEFAULT_REJECTED_TRANS_FROM = "01/01/2025"
DEFAULT_REJECTED_TRANS_TO = "12/31/2026"
DEFAULT_NETWORK_RETRY_ATTEMPTS = 3
DEFAULT_NETWORK_RETRY_DELAY_SEC = 10.0

DEFAULT_SECURITY_ANSWER_MAP: dict[str, str] = {
    "mother's maiden name": "ahmed",
    "first pet": "katy",
}


def parse_security_answers_env(raw: str) -> dict[str, str]:
    answers: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            answers[key] = value
    return answers


def merge_security_answers(env_raw: str | None) -> dict[str, str]:
    merged = dict(DEFAULT_SECURITY_ANSWER_MAP)
    if env_raw:
        merged.update(parse_security_answers_env(env_raw))
    return merged


def resolve_security_answer(
    question: str | None,
    config: "WaystarConfig",
) -> tuple[str | None, str | None]:
    """Return (answer, matched_key). matched_key is None when using fallback."""
    if question:
        question_lower = question.lower()
        for key, answer in (config.security_answers or {}).items():
            if key in question_lower:
                return answer, key
    if config.security_answer:
        return config.security_answer, None
    return None, None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass
class WaystarConfig:
    username: str
    password: str
    app_id: str = "1"
    cust_id: str | None = None
    transaction_days: int = DEFAULT_TRANSACTION_DAYS
    headless: bool = False
    security_answer: str | None = None
    security_answers: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SECURITY_ANSWER_MAP)
    )
    action_delay_min: float = DEFAULT_ACTION_DELAY_MIN
    action_delay_max: float = DEFAULT_ACTION_DELAY_MAX
    mfa_timeout_sec: int = DEFAULT_MFA_TIMEOUT_SEC
    slow_mo_ms: int = DEFAULT_SLOW_MO_MS
    claim_form: str = DEFAULT_CLAIM_FORM
    pdf_delay_sec: float = DEFAULT_PDF_DELAY_SEC
    network_retry_attempts: int = DEFAULT_NETWORK_RETRY_ATTEMPTS
    network_retry_delay_sec: float = DEFAULT_NETWORK_RETRY_DELAY_SEC

    @classmethod
    def from_env(cls, headless: bool = False) -> "WaystarConfig":
        username = os.getenv("WAYSTAR_USER", "").strip()
        password = os.getenv("WAYSTAR_PASS", "").strip()
        if not username or not password:
            raise ValueError(
                "WAYSTAR_USER and WAYSTAR_PASS must be set in .env or environment"
            )

        cust_id = os.getenv("WAYSTAR_CUST_ID", "").strip() or None
        app_id = os.getenv("WAYSTAR_APP_ID", "1").strip()
        transaction_days = _int_env("WAYSTAR_TRANSACTION_DAYS", DEFAULT_TRANSACTION_DAYS)
        security_answer = os.getenv("WAYSTAR_SECURITY_ANSWER", "").strip() or None
        security_answers_raw = os.getenv("WAYSTAR_SECURITY_ANSWERS", "").strip() or None
        security_answers = merge_security_answers(security_answers_raw)
        claim_form = os.getenv("WAYSTAR_CLAIM_FORM", DEFAULT_CLAIM_FORM).strip()

        return cls(
            username=username,
            password=password,
            app_id=app_id,
            cust_id=cust_id,
            transaction_days=transaction_days,
            headless=headless,
            security_answer=security_answer,
            security_answers=security_answers,
            action_delay_min=_float_env("WAYSTAR_ACTION_DELAY_MIN", DEFAULT_ACTION_DELAY_MIN),
            action_delay_max=_float_env("WAYSTAR_ACTION_DELAY_MAX", DEFAULT_ACTION_DELAY_MAX),
            mfa_timeout_sec=_int_env("WAYSTAR_MFA_TIMEOUT_SEC", DEFAULT_MFA_TIMEOUT_SEC),
            slow_mo_ms=_int_env("WAYSTAR_SLOW_MO_MS", DEFAULT_SLOW_MO_MS),
            claim_form=claim_form,
            pdf_delay_sec=_float_env("WAYSTAR_PDF_DELAY_SEC", DEFAULT_PDF_DELAY_SEC),
            network_retry_attempts=_int_env(
                "WAYSTAR_NETWORK_RETRIES", DEFAULT_NETWORK_RETRY_ATTEMPTS
            ),
            network_retry_delay_sec=_float_env(
                "WAYSTAR_NETWORK_RETRY_DELAY_SEC", DEFAULT_NETWORK_RETRY_DELAY_SEC
            ),
        )
