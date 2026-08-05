"""Connect to Snowflake and export query results to CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import snowflake.connector
from snowflake.connector import SnowflakeConnection

from .config import OUTPUT_DIR, SnowflakeConfig, load_config, update_env_warehouse

FETCH_SIZE = 10_000


def connect(cfg: SnowflakeConfig) -> SnowflakeConnection:
    params: dict[str, Any] = {
        "account": cfg.account,
        "user": cfg.user,
    }
    if cfg.auth == "password":
        params["password"] = cfg.password
        if cfg.passcode:
            # Native Snowflake TOTP MFA
            params["passcode"] = cfg.passcode
            params["authenticator"] = "username_password_mfa"
            params["client_request_mfa_token"] = True
        else:
            # Still request MFA token caching if Duo/push ever applies
            params["client_request_mfa_token"] = True
    elif cfg.auth == "externalbrowser":
        params["authenticator"] = "externalbrowser"
    else:
        if not cfg.private_key_path.is_file():
            raise SystemExit(f"Private key not found: {cfg.private_key_path}")
        params["authenticator"] = "SNOWFLAKE_JWT"
        params["private_key_file"] = str(cfg.private_key_path)
        params["private_key_file_pwd"] = cfg.private_key_passphrase

    if cfg.warehouse:
        params["warehouse"] = cfg.warehouse
    if cfg.database:
        params["database"] = cfg.database
    if cfg.schema:
        params["schema"] = cfg.schema
    if cfg.role:
        params["role"] = cfg.role

    try:
        return snowflake.connector.connect(**params)
    except snowflake.connector.errors.DatabaseError as exc:
        msg = str(exc)
        if "TOTP" in msg or "MFA" in msg:
            raise SystemExit(
                "Snowflake requires MFA TOTP with your password.\n"
                "Re-run with a current authenticator code, e.g.:\n"
                "  python -m snowflake_pull --passcode 123456 --list-warehouses\n"
                "Or set SNOWFLAKE_PASSCODE in .env (6-digit code, expires quickly).\n"
                "Long-term: ask admin to register keys/snowflake_key.pub "
                "(see REGISTER_KEY.md) and use SNOWFLAKE_AUTH=keypair."
            ) from exc
        raise


def list_warehouses(conn: SnowflakeConnection) -> list[str]:
    names: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SHOW WAREHOUSES")
        rows = cur.fetchall()
        # SHOW WAREHOUSES: name is typically column index 0
        for row in rows:
            if row and row[0]:
                names.append(str(row[0]))
    return names


def resolve_warehouse(
    conn: SnowflakeConnection,
    cfg: SnowflakeConfig,
    *,
    persist: bool = True,
) -> str:
    if cfg.warehouse:
        with conn.cursor() as cur:
            cur.execute(f'USE WAREHOUSE "{cfg.warehouse}"')
        return cfg.warehouse

    names = list_warehouses(conn)
    if not names:
        raise SystemExit(
            "No warehouse in env and SHOW WAREHOUSES returned none. "
            "Set SNOWFLAKE_WAREHOUSE in .env."
        )

    chosen = names[0]
    with conn.cursor() as cur:
        cur.execute(f'USE WAREHOUSE "{chosen}"')
    if persist:
        update_env_warehouse(chosen)
        print(f"Discovered warehouse={chosen} (saved to .env)")
    return chosen


def resolve_sql(
    *,
    sql: str | None,
    sql_file: Path | None,
    default_sql: str,
) -> str:
    if sql and sql_file:
        raise SystemExit("Pass only one of --sql or --sql-file, not both.")
    if sql_file is not None:
        if not sql_file.is_file():
            raise SystemExit(f"SQL file not found: {sql_file}")
        text = sql_file.read_text(encoding="utf-8").strip()
        if not text:
            raise SystemExit(f"SQL file is empty: {sql_file}")
        return text
    if sql:
        text = sql.strip()
        if not text:
            raise SystemExit("--sql is empty.")
        return text
    return default_sql.strip()


def write_csv(
    path: Path,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> int:
    """Write an in-memory result set to CSV (used by unit tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(list(row))
    return len(rows)


def write_query_csv(
    conn: SnowflakeConnection,
    sql: str,
    path: Path,
    *,
    fetch_size: int = FETCH_SIZE,
) -> tuple[int, int]:
    """Execute SQL and stream rows to CSV. Returns (row_count, column_count)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [col[0] for col in (cur.description or [])]
        if not columns:
            return 0, 0
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            while True:
                batch = cur.fetchmany(fetch_size)
                if not batch:
                    break
                for row in batch:
                    writer.writerow(list(row))
                    count += 1
                print(f"  ... {count} rows", flush=True)
    return count, len(columns)


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"snowflake_pull_{stamp}.csv"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pull Snowflake query results to CSV (password or key-pair auth).",
    )
    p.add_argument(
        "--sql",
        help="SQL to run (overrides SNOWFLAKE_SQL / default billing query).",
    )
    p.add_argument(
        "--sql-file",
        type=Path,
        help="Path to a .sql file to run.",
    )
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output CSV path (default: output/snowflake_pull_<utc>.csv).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved SQL and connection targets; do not connect.",
    )
    p.add_argument(
        "--list-warehouses",
        action="store_true",
        help="Connect, list warehouses, resolve/persist one if unset, then exit.",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Connect, resolve warehouse, run SELECT 1 smoke test, then exit.",
    )
    p.add_argument(
        "--passcode",
        help="MFA TOTP passcode (overrides SNOWFLAKE_PASSCODE).",
    )
    p.add_argument(
        "--auth",
        choices=("password", "keypair", "externalbrowser"),
        help="Override SNOWFLAKE_AUTH for this run.",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.auth:
        import os

        os.environ["SNOWFLAKE_AUTH"] = args.auth
    if args.passcode:
        import os

        os.environ["SNOWFLAKE_PASSCODE"] = args.passcode
    cfg = load_config()
    sql = resolve_sql(
        sql=args.sql,
        sql_file=args.sql_file,
        default_sql=cfg.default_sql,
    )
    out = args.output or default_output_path()
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()

    if args.dry_run:
        print(f"account={cfg.account}")
        print(f"user={cfg.user}")
        print(f"auth={cfg.auth}")
        print(f"warehouse={cfg.warehouse or '(auto)'}")
        print(f"database={cfg.database or '(none)'}")
        print(f"schema={cfg.schema or '(none)'}")
        print(f"role={cfg.role or '(none)'}")
        if cfg.auth == "keypair":
            print(f"private_key={cfg.private_key_path}")
        print(f"output={out}")
        print("--- SQL ---")
        print(sql)
        return 0

    print(f"Connecting to {cfg.account} as {cfg.user} (auth={cfg.auth}) ...")
    conn = connect(cfg)
    try:
        warehouse = resolve_warehouse(conn, cfg)
        print(f"Using warehouse={warehouse}")

        if args.list_warehouses:
            for name in list_warehouses(conn):
                mark = " *" if name == warehouse else ""
                print(f"{name}{mark}")
            return 0

        if args.smoke:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok, CURRENT_USER() AS u, CURRENT_ROLE() AS r")
                row = cur.fetchone()
            print(f"Smoke OK: {row}")
            return 0

        print(f"Running query -> {out}")
        n, ncols = write_query_csv(conn, sql, out)
        if ncols == 0:
            print("Query returned no columns (e.g. DDL/DML). Nothing written.")
            return 0
        print(f"Wrote {n} row(s), {ncols} column(s) -> {out}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
