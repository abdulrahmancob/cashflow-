"""Portal password hashing + idempotent seed of first portal users."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Any

PBKDF2_ITERATIONS = 260_000

# Defaults match deploy/.env.example (Docker first users).
_DEFAULT_SEEDS: list[dict[str, str]] = [
    {
        "env_email": "CASHFLOW_SEED_ADMIN_EMAIL",
        "env_password": "CASHFLOW_SEED_ADMIN_PASSWORD",
        "env_name": "CASHFLOW_SEED_ADMIN_NAME",
        "email": "abdelrahman.hamdy@cobsolution.com",
        "password": "Rcm@112233",
        "name": "Abdelrahman Hamdy",
        "role": "super_admin",
    },
    {
        "env_email": "CASHFLOW_SEED_FINANCE_EMAIL",
        "env_password": "CASHFLOW_SEED_FINANCE_PASSWORD",
        "env_name": "CASHFLOW_SEED_FINANCE_NAME",
        "email": "mostafa.ezz@cobsolution.com",
        "password": "F@12345",
        "name": "Mostafa Ezz",
        "role": "finance",
    },
    {
        "env_email": "CASHFLOW_SEED_POSTING_EMAIL",
        "env_password": "CASHFLOW_SEED_POSTING_PASSWORD",
        "env_name": "CASHFLOW_SEED_POSTING_NAME",
        "email": "billing7@cobsolution.com",
        "password": "B@12345",
        "name": "Ahmed Daker",
        "role": "posting_team",
    },
]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iters_s, salt_b64, dig_b64 = password_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(dig_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iters
        )
        return hmac.compare_digest(actual, expected)
    except Exception:  # noqa: BLE001
        return False


def seed_portal_users() -> dict[str, Any]:
    """Ensure the three Docker portal users exist (create-if-missing only)."""
    from cashflow_db.repository import auth_users, connection

    created: list[str] = []
    existing: list[str] = []
    with connection() as conn:
        for spec in _DEFAULT_SEEDS:
            email = os.environ.get(spec["env_email"], spec["email"]).strip()
            password = os.environ.get(spec["env_password"], spec["password"])
            name = os.environ.get(spec["env_name"], spec["name"]).strip()
            uid, was_created = auth_users.ensure_user(
                conn,
                username=email,
                email=email,
                password_hash=hash_password(password),
                display_name=name,
                roles=[spec["role"]],
            )
            entry = f"{email}:{spec['role']}:{uid}"
            if was_created:
                created.append(entry)
            else:
                existing.append(entry)
    return {"created": created, "existing": existing}


def bootstrap_admin_if_needed() -> dict[str, Any] | None:
    """Backward-compatible entry: seed portal users (idempotent)."""
    try:
        return seed_portal_users()
    except Exception:  # noqa: BLE001
        return None
