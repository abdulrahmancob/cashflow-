"""Shared DB client / transaction helpers for the repository layer."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from cashflow_db.config import DATABASE_URL
from cashflow_db.db import connect as _connect


@contextmanager
def connection(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Yield a dict-row connection; commit on success."""
    with _connect(url) as conn:
        yield conn


@contextmanager
def transaction(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Alias for connection (commit/rollback handled by connect)."""
    with connection(url) as conn:
        yield conn


def fetchall(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params or ())
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetchone(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    cur = conn.execute(sql, params or ())
    row = cur.fetchone()
    return dict(row) if row else None


def execute(
    conn: psycopg.Connection,
    sql: str,
    params: Sequence[Any] | None = None,
) -> None:
    conn.execute(sql, params or ())


def executemany(
    conn: psycopg.Connection,
    sql: str,
    params_seq: Sequence[Sequence[Any]],
) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql, params_seq)


def connect_raw(url: str | None = None) -> psycopg.Connection:
    """Open a connection the caller must close (rare; prefer context managers)."""
    return psycopg.connect(url or DATABASE_URL, row_factory=dict_row)
