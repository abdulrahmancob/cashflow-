"""Reusable feature store repository (definition + snapshot)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable

import psycopg

from cashflow_db.repository import client


def upsert_definition(
    conn: psycopg.Connection,
    *,
    feature_key: str,
    description: str | None = None,
    grain: str | None = None,
    owner: str | None = None,
    version: str = "1",
) -> None:
    client.execute(
        conn,
        """
        INSERT INTO analytics.feature_definition (
            feature_key, description, grain, owner, version
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (feature_key) DO UPDATE SET
            description = COALESCE(EXCLUDED.description, analytics.feature_definition.description),
            grain = COALESCE(EXCLUDED.grain, analytics.feature_definition.grain),
            owner = COALESCE(EXCLUDED.owner, analytics.feature_definition.owner),
            version = EXCLUDED.version
        """,
        (feature_key, description, grain, owner, version),
    )


def write_snapshots(
    conn: psycopg.Connection,
    *,
    as_of_date: date,
    dataset_version: str | None,
    rows: Iterable[dict[str, Any]],
) -> int:
    n = 0
    sql = """
        INSERT INTO analytics.feature_snapshot (
            as_of_date, feature_key, entity_key, value_num, payload, dataset_version
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (as_of_date, feature_key, entity_key, dataset_version)
        DO UPDATE SET
            value_num = EXCLUDED.value_num,
            payload = EXCLUDED.payload,
            created_at = now()
    """
    for r in rows:
        client.execute(
            conn,
            sql,
            (
                as_of_date,
                r["feature_key"],
                r.get("entity_key") or "",
                r.get("value_num"),
                json.dumps(r.get("payload") or {}, default=str),
                dataset_version,
            ),
        )
        n += 1
    return n


def get_features(
    conn: psycopg.Connection,
    *,
    as_of_date: date,
    feature_keys: list[str] | None = None,
    dataset_version: str | None = None,
    entity_key: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["as_of_date = %s"]
    params: list[Any] = [as_of_date]
    if dataset_version:
        clauses.append("dataset_version = %s")
        params.append(dataset_version)
    if feature_keys:
        clauses.append("feature_key = ANY(%s)")
        params.append(feature_keys)
    if entity_key is not None:
        clauses.append("entity_key = %s")
        params.append(entity_key)
    where = " AND ".join(clauses)
    return client.fetchall(
        conn,
        f"""
        SELECT * FROM analytics.feature_snapshot
        WHERE {where}
        ORDER BY feature_key, entity_key
        """,
        tuple(params),
    )


def latest_dataset_version(
    conn: psycopg.Connection, as_of_date: date
) -> str | None:
    row = client.fetchone(
        conn,
        """
        SELECT dataset_version
        FROM analytics.feature_snapshot
        WHERE as_of_date = %s AND dataset_version IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (as_of_date,),
    )
    return str(row["dataset_version"]) if row and row.get("dataset_version") else None
