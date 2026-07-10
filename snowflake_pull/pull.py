"""Connect to Snowflake with key-pair auth and export query results to CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import snowflake.connector

from .config import OUTPUT_DIR, SnowflakeConfig, load_config


def connect(cfg: SnowflakeConfig) -> snowflake.connector.SnowflakeConnection:
    if not cfg.private_key_path.is_file():
        raise SystemExit(f"Private key not found: {cfg.private_key_path}")

    params: dict[str, Any] = {
        "account": cfg.account,
        "user": cfg.user,
        "authenticator": "SNOWFLAKE_JWT",
        "private_key_file": str(cfg.private_key_path),
        "private_key_file_pwd": cfg.private_key_passphrase,
    }
    if cfg.warehouse:
        params["warehouse"] = cfg.warehouse
    if cfg.database:
        params["database"] = cfg.database
    if cfg.schema:
        params["schema"] = cfg.schema
    if cfg.role:
        params["role"] = cfg.role

    return snowflake.connector.connect(**params)


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


def fetch_rows(
    conn: snowflake.connector.SnowflakeConnection,
    sql: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [col[0] for col in (cur.description or [])]
        rows = cur.fetchall()
    return columns, list(rows)


def write_csv(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(list(row))
            count += 1
    return count


def default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"snowflake_pull_{stamp}.csv"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pull Snowflake query results to CSV using key-pair auth.",
    )
    p.add_argument(
        "--sql",
        help="SQL to run (overrides SNOWFLAKE_SQL / default smoke query).",
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
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        print(f"warehouse={cfg.warehouse or '(none)'}")
        print(f"database={cfg.database or '(none)'}")
        print(f"schema={cfg.schema or '(none)'}")
        print(f"role={cfg.role or '(none)'}")
        print(f"private_key={cfg.private_key_path}")
        print(f"output={out}")
        print("--- SQL ---")
        print(sql)
        return 0

    print(f"Connecting to {cfg.account} as {cfg.user} ...")
    conn = connect(cfg)
    try:
        columns, rows = fetch_rows(conn, sql)
    finally:
        conn.close()

    if not columns:
        print("Query returned no columns (e.g. DDL/DML). Nothing written.")
        return 0

    n = write_csv(out, columns, rows)
    print(f"Wrote {n} row(s), {len(columns)} column(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
