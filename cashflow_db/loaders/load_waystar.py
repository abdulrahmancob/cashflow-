"""Waystar rejections / denials → billing.denial_record."""

from __future__ import annotations

import csv
from pathlib import Path

from cashflow_db.config import WAYSTAR_DENIALS_DIR, WAYSTAR_REJECTIONS_CSV
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.util import parse_date, parse_money, safe_str


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh))


def _denial_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    merged = root / "denials_merged.csv"
    if merged.exists():
        return [merged]
    return sorted(root.glob("batch_*.csv")) + sorted(root.glob("*denial*.csv"))


def load_waystar(
    *,
    rejections_csv: Path | None = None,
    denials_dir: Path | None = None,
    database_url: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    rejections_csv = rejections_csv or WAYSTAR_REJECTIONS_CSV
    denials_dir = denials_dir or WAYSTAR_DENIALS_DIR
    counts = {"rejections": 0, "denials": 0, "skipped": 0}

    with connect(database_url) as conn:
        etl_id = start_etl_run(
            conn,
            "waystar",
            str(rejections_csv if rejections_csv.exists() else denials_dir),
        )
        try:
            if rejections_csv.exists():
                rows = _read_csv(rejections_csv)
                if limit:
                    rows = rows[:limit]
                for row in rows:
                    natural = (
                        safe_str(row.get("claim_id"))
                        or safe_str(row.get("patient_control_number"))
                        or safe_str(row.get("control_number"))
                    )
                    if not natural:
                        natural = "|".join(
                            safe_str(row.get(k)) or ""
                            for k in ("patient_name", "dos", "cpt", "error_code")
                        )
                    if not natural.strip("|"):
                        counts["skipped"] += 1
                        continue
                    conn.execute(
                        """
                        INSERT INTO billing.denial_record (
                            reason_code, denied_amount, denial_date, source,
                            is_partial_denial, source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s, %s, %s, 'rejection', false, 'waystar', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            reason_code = EXCLUDED.reason_code,
                            denial_date = EXCLUDED.denial_date,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            safe_str(
                                row.get("error_code")
                                or row.get("reason_code")
                                or row.get("rejection_reason")
                            ),
                            parse_money(row.get("denied_amount") or row.get("billed_amount")),
                            parse_date(
                                row.get("denial_date")
                                or row.get("rejection_date")
                                or row.get("dos")
                            ),
                            f"rej:{natural}",
                            etl_id,
                        ),
                    )
                    counts["rejections"] += 1

            for path in _denial_files(denials_dir):
                rows = _read_csv(path)
                if limit:
                    rows = rows[:limit]
                for row in rows:
                    natural = (
                        safe_str(row.get("claim_id"))
                        or safe_str(row.get("patient_control_number"))
                        or "|".join(
                            safe_str(row.get(k)) or ""
                            for k in ("patient_name", "service_date", "cpt", "carc")
                        )
                    )
                    if not natural.strip("|"):
                        counts["skipped"] += 1
                        continue
                    conn.execute(
                        """
                        INSERT INTO billing.denial_record (
                            reason_code, denied_amount, denial_date, source,
                            is_partial_denial, source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s, %s, %s, 'denial', %s, 'waystar', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            reason_code = EXCLUDED.reason_code,
                            denied_amount = EXCLUDED.denied_amount,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            safe_str(
                                row.get("carc")
                                or row.get("reason_code")
                                or row.get("denial_reason")
                            ),
                            parse_money(row.get("denied_amount") or row.get("paid_amount")),
                            parse_date(
                                row.get("denial_date")
                                or row.get("service_date")
                                or row.get("dos")
                            ),
                            str(row.get("is_partial") or "").lower() in {"1", "true", "yes"},
                            f"den:{path.name}:{natural}",
                            etl_id,
                        ),
                    )
                    counts["denials"] += 1

            finish_etl_run(
                conn,
                etl_id,
                status="success",
                row_count=counts["rejections"] + counts["denials"],
            )
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
