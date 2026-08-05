"""Quality metric persistence with precomputed status."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cashflow_ops.config import DATABASE_URL

log = logging.getLogger(__name__)


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _eval_status(value: float, rule: dict[str, Any]) -> tuple[str, str | None, str | None]:
    expected = rule.get("expected_value")
    warning = rule.get("warning_threshold")
    critical = rule.get("critical_threshold")
    comparison = rule.get("comparison") or "lt"

    def _num(x: Any) -> float | None:
        if x is None:
            return None
        try:
            return float(str(x).lstrip("<>=").strip())
        except ValueError:
            return None

    crit_n = _num(critical)
    warn_n = _num(warning) if warning is not None else _num(rule.get("threshold"))

    def breach(threshold: float | None, op: str) -> bool:
        if threshold is None:
            return False
        if op in ("lt", "lte"):
            # value should be below threshold; breach if value >= threshold
            return value >= threshold if op == "lt" else value > threshold
        if op in ("gt", "gte"):
            return value <= threshold if op == "gt" else value < threshold
        return value != threshold

    # For gt/gte metrics (schedule_rows > 0): critical if not meeting
    if comparison in ("gt", "gte"):
        thresh = _num(rule.get("threshold"))
        if thresh is not None and breach(thresh, comparison):
            return "critical", expected, rule.get("threshold")
        return "ok", expected, rule.get("threshold")

    # For lt/lte (notes_missing < 10): warning/critical when high
    if crit_n is not None and value >= crit_n:
        return "critical", expected, str(crit_n)
    if warn_n is not None and value >= warn_n:
        return "warning", expected, str(warn_n)
    return "ok", expected, rule.get("threshold")


def upsert_quality_metric(
    *,
    as_of_date: date,
    run_id: str,
    metric_key: str,
    value_num: float | int,
    dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        with _connect() as conn:
            rule = conn.execute(
                """
                SELECT * FROM monitoring.quality_rule
                WHERE metric_key = %s AND enabled
                """,
                (metric_key,),
            ).fetchone()
            if rule:
                status, expected, threshold = _eval_status(float(value_num), dict(rule))
            else:
                status, expected, threshold = "ok", None, None

            conn.execute(
                """
                INSERT INTO monitoring.quality_metric (
                    as_of_date, run_id, metric_key, value_num,
                    expected_value, threshold, status, dimensions
                )
                VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, metric_key) WHERE run_id IS NOT NULL
                DO UPDATE SET
                    value_num = EXCLUDED.value_num,
                    expected_value = EXCLUDED.expected_value,
                    threshold = EXCLUDED.threshold,
                    status = EXCLUDED.status,
                    dimensions = EXCLUDED.dimensions,
                    recorded_at = now()
                """,
                (
                    as_of_date,
                    run_id,
                    metric_key,
                    float(value_num),
                    expected,
                    threshold,
                    status,
                    json.dumps(dimensions or {}),
                ),
            )
            conn.commit()
            return {
                "metric_key": metric_key,
                "value_num": float(value_num),
                "expected_value": expected,
                "threshold": threshold,
                "status": status,
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("upsert_quality_metric failed (%s): %s", metric_key, exc)
        return {"metric_key": metric_key, "error": str(exc)}


def persist_from_metrics_dict(
    *,
    as_of_date: date,
    run_id: str,
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    keys = (
        "schedule_rows",
        "notes_missing",
        "cpt_missing",
        "ocr_success_pct",
        "revflow_files",
        "payments_rows",
        "patient_payments_rows",
        "revflow_export_files",
    )
    # normalize aliases
    normalized = dict(metrics)
    if "revflow_export_files" in normalized and "revflow_files" not in normalized:
        normalized["revflow_files"] = normalized["revflow_export_files"]
    if "patient_payments_rows" in normalized and "payments_rows" not in normalized:
        normalized["payments_rows"] = normalized["patient_payments_rows"]

    out: list[dict[str, Any]] = []
    for key in keys:
        if key in ("revflow_export_files", "patient_payments_rows"):
            continue
        if key not in normalized or normalized[key] is None:
            continue
        try:
            out.append(
                upsert_quality_metric(
                    as_of_date=as_of_date,
                    run_id=run_id,
                    metric_key=key,
                    value_num=float(normalized[key]),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def quality_trend(as_of: date, days: int = 30) -> list[dict[str, Any]]:
    start = as_of - timedelta(days=days)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT as_of_date, metric_key, value_num, status, expected_value, threshold
            FROM monitoring.quality_metric
            WHERE as_of_date >= %s AND as_of_date <= %s
            ORDER BY as_of_date, metric_key
            """,
            (start, as_of),
        ).fetchall()
        return [dict(r) for r in rows]
