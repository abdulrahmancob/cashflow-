"""Load Snowflake connection settings from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
KEYS_DIR = BASE_DIR / "keys"
DEFAULT_PRIVATE_KEY_PATH = KEYS_DIR / "snowflake_key.p8"
DEFAULT_PASSPHRASE_PATH = KEYS_DIR / ".passphrase"

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class SnowflakeConfig:
    account: str
    user: str
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
    key_raw = _optional(
        "SNOWFLAKE_PRIVATE_KEY_PATH",
        str(DEFAULT_PRIVATE_KEY_PATH),
    )
    key_path = Path(key_raw)
    if not key_path.is_absolute():
        key_path = (BASE_DIR / key_path).resolve()

    return SnowflakeConfig(
        account=_require("SNOWFLAKE_ACCOUNT"),
        user=_require("SNOWFLAKE_USER"),
        warehouse=_optional("SNOWFLAKE_WAREHOUSE"),
        database=_optional("SNOWFLAKE_DATABASE"),
        schema=_optional("SNOWFLAKE_SCHEMA", "PUBLIC"),
        role=_optional("SNOWFLAKE_ROLE"),
        private_key_path=key_path,
        private_key_passphrase=_resolve_passphrase(),
        default_sql=_optional(
            "SNOWFLAKE_SQL",
            "SELECT CURRENT_TIMESTAMP() AS pulled_at, "
            "CURRENT_USER() AS snowflake_user, "
            "CURRENT_ACCOUNT() AS snowflake_account",
        ),
    )
