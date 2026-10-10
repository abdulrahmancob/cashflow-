"""Accounts for load tests: active only while a test runs, signed in without any password.

``activate`` creates ``loadtest-NN@internal.invalid`` on first use (random password nobody
knows), turns them on, and prints one short-lived session token per account as JSON for k6.
``deactivate`` turns them off again, which also takes them off the away board.

Run inside the api container:
    python -m cashflow_ops.loadtest_users activate --count 50
    python -m cashflow_ops.loadtest_users deactivate
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from cashflow_db.repository import auth_users, client, connection
from cashflow_db.services.bootstrap_admin import hash_password
from cashflow_ops.security import create_access_token

USERNAME = "loadtest-{:02d}@internal.invalid"
POSTING_SHARE = 0.2
TOKEN_TTL_SECONDS = 3600


def plan_accounts(count: int) -> list[dict[str, str]]:
    """Most accounts open My day only; a fifth also read the eligibility list."""
    posting = max(1, round(count * POSTING_SHARE))
    accounts = []
    for index in range(1, count + 1):
        kind = "posting" if index > count - posting else "desk"
        accounts.append({
            "username": USERNAME.format(index),
            "kind": kind,
            "role": "posting_team" if kind == "posting" else "desk",
        })
    return accounts


def activate(count: int) -> list[dict[str, str]]:
    tokens = []
    with connection() as conn:
        for account in plan_accounts(count):
            user = auth_users.get_user_by_username(conn, account["username"])
            if user is None:
                user_id = auth_users.create_user(
                    conn,
                    username=account["username"],
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                    display_name=f"Load test {account['username'][9:11]}",
                    email=account["username"],
                    roles=[account["role"]],
                )
            else:
                user_id = str(user["user_id"])
                auth_users.update_user(conn, user_id, is_active=True)
                auth_users.set_user_roles(conn, user_id, [account["role"]])
            tokens.append({
                "kind": account["kind"],
                "token": create_access_token(
                    user_id=user_id,
                    username=account["username"],
                    roles=[account["role"]],
                    display_name=account["username"],
                    ttl_seconds=TOKEN_TTL_SECONDS,
                ),
            })
    return tokens


def deactivate() -> int:
    with connection() as conn:
        rows = client.fetchall(
            conn,
            """
            UPDATE auth.app_user
            SET is_active = false, updated_at = now()
            WHERE username LIKE 'loadtest-%%@internal.invalid' AND is_active
            RETURNING user_id
            """,
            (),
        )
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load test accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("activate")
    on.add_argument("--count", type=int, default=50)
    sub.add_parser("deactivate")
    args = parser.parse_args(argv)
    if args.command == "activate":
        json.dump(activate(max(1, min(args.count, 200))), sys.stdout)
        return 0
    print(f"deactivated {deactivate()} load test account(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
