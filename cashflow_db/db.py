"""PostgreSQL connection helpers and schema migration runner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL, SQL_DIR

MIGRATIONS = [
    "001_schemas.sql",
    "002_ref.sql",
    "003_etl_gov.sql",
    "004_core.sql",
    "005_billing.sql",
    "006_docs.sql",
    "007_ops.sql",
    "008_analytics.sql",
    "009_finance.sql",
    "010_views.sql",
    "011_seed_ref.sql",
    "012_case_centric.sql",
    "013_operational_spine.sql",
    "014_pipeline_control.sql",
    "015_monitoring.sql",
    "016_auth.sql",
    "017_eligibility_ops.sql",
    "018_transaction_tracker.sql",
]


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(url or DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    conn.execute(sql)


def migrate(url: str | None = None) -> list[str]:
    applied: list[str] = []
    with connect(url) as conn:
        for name in MIGRATIONS:
            path = SQL_DIR / name
            if not path.exists():
                raise FileNotFoundError(path)
            run_sql_file(conn, path)
            applied.append(name)
    return applied


def start_etl_run(
    conn: psycopg.Connection,
    source_system: str,
    source_uri: str | None = None,
    notes: str | None = None,
) -> str:
    row = conn.execute(
        """
        INSERT INTO etl.etl_run (source_system, source_uri, status, notes)
        VALUES (%s, %s, 'running', %s)
        RETURNING etl_run_id
        """,
        (source_system, source_uri, notes),
    ).fetchone()
    return str(row["etl_run_id"])


def finish_etl_run(
    conn: psycopg.Connection,
    etl_run_id: str,
    *,
    status: str = "success",
    row_count: int | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE etl.etl_run
        SET finished_at = now(),
            status = %s,
            row_count = %s,
            notes = COALESCE(%s, notes)
        WHERE etl_run_id = %s::uuid
        """,
        (status, row_count, notes, etl_run_id),
    )
