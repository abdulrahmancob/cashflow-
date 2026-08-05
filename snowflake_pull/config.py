"""Load Snowflake connection settings from environment / .env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
KEYS_DIR = BASE_DIR / "keys"
DEFAULT_PRIVATE_KEY_PATH = KEYS_DIR / "snowflake_key.p8"
DEFAULT_PASSPHRASE_PATH = KEYS_DIR / ".passphrase"
ENV_PATH = BASE_DIR / ".env"

DEFAULT_BILLING_SQL = (
    "SELECT * FROM BILLING_DATA.PUBLIC.ALL_BILLING_DATA "
    "WHERE DATE_OF_SERVICE >= '2026-01-01' "
    "AND DATE_OF_SERVICE < '2027-01-01'"
)

load_dotenv(ENV_PATH)


@dataclass(frozen=True)
class SnowflakeConfig:
    account: str
    user: str
    auth: str  # "password" | "keypair" | "externalbrowser"
    password: str
    passcode: str  # MFA TOTP when auth=password
    warehouse: str
    database: str
    schema: str
    role: str
    private_key_path: Path
    private_key_passphrase: str
    default_sql: str


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(
            f"Missing required env var {name}. "
            f"Copy .env.example to .env and fill in values."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _resolve_passphrase() -> str:
    env_pass = _optional("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    if env_pass:
        return env_pass
    if DEFAULT_PASSPHRASE_PATH.is_file():
        return DEFAULT_PASSPHRASE_PATH.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "Missing private key passphrase. Set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE "
        f"in .env or create {DEFAULT_PASSPHRASE_PATH}."
    )


def load_config() -> SnowflakeConfig:
    auth = _optional("SNOWFLAKE_AUTH", "password").lower()
    if auth not in {"password", "keypair", "externalbrowser"}:
        raise SystemExit(
            "SNOWFLAKE_AUTH must be 'password', 'keypair', or 'externalbrowser'."
        )

    key_raw = _optional(
        "SNOWFLAKE_PRIVATE_KEY_PATH",
        str(DEFAULT_PRIVATE_KEY_PATH),
    )
    key_path = Path(key_raw)
    if not key_path.is_absolute():
        key_path = (BASE_DIR / key_path).resolve()

    password = ""
    passphrase = ""
    if auth == "password":
        password = _require("SNOWFLAKE_PASSWORD")
    elif auth == "keypair":
        passphrase = _resolve_passphrase()
    # externalbrowser: SSO/MFA via browser; password optional

    return SnowflakeConfig(
        account=_require("SNOWFLAKE_ACCOUNT"),
        user=_require("SNOWFLAKE_USER"),
        auth=auth,
        password=password or _optional("SNOWFLAKE_PASSWORD"),
        passcode=_optional("SNOWFLAKE_PASSCODE"),
        warehouse=_optional("SNOWFLAKE_WAREHOUSE"),
        database=_optional("SNOWFLAKE_DATABASE", "BILLING_DATA"),
        schema=_optional("SNOWFLAKE_SCHEMA", "PUBLIC"),
        role=_optional("SNOWFLAKE_ROLE"),
        private_key_path=key_path,
        private_key_passphrase=passphrase,
        default_sql=_optional("SNOWFLAKE_SQL", DEFAULT_BILLING_SQL),
    )


def update_env_warehouse(warehouse: str) -> None:
    """Persist discovered warehouse into .env (create or replace key)."""
    warehouse = warehouse.strip()
    if not warehouse:
        return
    line = f"SNOWFLAKE_WAREHOUSE={warehouse}"
    if ENV_PATH.is_file():
        text = ENV_PATH.read_text(encoding="utf-8")
        if re.search(r"(?m)^SNOWFLAKE_WAREHOUSE=", text):
            text = re.sub(
                r"(?m)^SNOWFLAKE_WAREHOUSE=.*$",
                line,
                text,
            )
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
        ENV_PATH.write_text(text, encoding="utf-8")
    else:
        ENV_PATH.write_text(line + "\n", encoding="utf-8")
    os.environ["SNOWFLAKE_WAREHOUSE"] = warehouse
