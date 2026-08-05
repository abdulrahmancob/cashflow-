"""Generate / refresh eligibility work items from reconciliation visits."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from cashflow_db.db import finish_etl_run, start_etl_run
from cashflow_db.repository import client, connection, eligibility, reconciliation

DEFAULT_RECON_DIR = Path(
    os.environ.get(
        "RECONCILIATION_DIR",
        str(
            Path(__file__).resolve().parents[2]
            / "webpt_edco_scraper/output/jun_jul_2026/reconciliation"
        ),
    )
)


def _load_insurance_map_from_csv(recon_dir: Path) -> dict[tuple[str, str], str]:
    """Map (emr_patient_id, facility_name) -> primary insurance from patients CSV."""
    path = recon_dir / "reconciliation_patients.csv"
    out: dict[tuple[str, str], str] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            emr = str(row.get("webpt_patient_id") or "").strip()
            fac = str(row.get("facility_name") or "").strip()
            ins = (
                row.get("primary_payor")
                or row.get("ins_name")
                or ""
            ).strip()
            if emr and fac and ins:
                out[(emr, fac)] = ins
    return out


def _visits_from_csv(recon_dir: Path) -> list[dict[str, Any]]:
    path = recon_dir / "reconciliation_visits.csv"
    if not path.exists():
        return []
    ins_map = _load_insurance_map_from_csv(recon_dir)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            emr = str(row.get("webpt_patient_id") or "").strip()
            fac = str(row.get("facility_name") or "").strip()
            row = dict(row)
            row["insurance_name"] = ins_map.get((emr, fac))
            rows.append(row)
    return rows


def _visits_from_db(conn: Any, run_id: str | None = None) -> list[dict[str, Any]]:
    visits = reconciliation.get_visit_aggs(conn, run_id=run_id)
    if not visits:
        return []
    # Enrich insurance from latest lines when available
    lines = reconciliation.get_lines(conn, run_id=run_id)
    ins_map: dict[tuple[str, str, str], str] = {}
    for line in lines:
        emr = str(line.get("webpt_patient_id") or "")
        fac = str(line.get("facility_name") or "")
        dos = str(line.get("date_of_service") or "")[:10]
        ins = (line.get("ins_name") or line.get("insurance_revflow") or "").strip()
        if emr and fac and dos and ins and (emr, fac, dos) not in ins_map:
            ins_map[(emr, fac, dos)] = ins
    for v in visits:
        emr = str(v.get("webpt_patient_id") or "")
        fac = str(v.get("facility_name") or "")
        dos = str(v.get("date_of_service") or "")[:10]
        v["insurance_name"] = ins_map.get((emr, fac, dos))
    return visits


def generate_eligibility_work_items(
    *,
    recon_dir: Path | None = None,
    recon_run_id: str | None = None,
    from_db: bool = True,
    dry_run: bool = False,
    attach_docs: bool = True,
) -> dict[str, Any]:
    recon_dir = recon_dir or DEFAULT_RECON_DIR
    created = 0
    refreshed = 0
    attached = 0
    errors: list[str] = []

    with connection() as conn:
        etl_id = None
        if not dry_run:
            etl_id = start_etl_run(
                conn,
                "manual",
                source_uri=str(recon_dir),
                notes="eligibility_work_item generation",
            )

        visits: list[dict[str, Any]] = []
        source = "none"
        if from_db:
            try:
                visits = _visits_from_db(conn, run_id=recon_run_id)
                source = "db"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"db_load: {exc}")
        if not visits:
            visits = _visits_from_csv(recon_dir)
            source = "csv" if visits else source

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "source": source,
                "visit_count": len(visits),
            }

        before = client.fetchone(
            conn, "SELECT count(*)::int AS n FROM ops.eligibility_work_item"
        )
        before_n = int(before["n"]) if before else 0

        for visit in visits:
            try:
                wid = eligibility.upsert_from_visit(
                    conn,
                    visit,
                    recon_run_id=recon_run_id,
                    etl_run_id=etl_id,
                )
                if attach_docs:
                    emr = str(visit.get("webpt_patient_id") or "").strip()
                    docs = eligibility.find_eligibility_docs_for_emr(conn, emr)
                    for doc in docs[:1]:
                        eligibility.link_attachment(
                            conn,
                            work_item_id=wid,
                            document_id=str(doc["document_id"]),
                            storage_path=doc.get("storage_path"),
                            filename=doc.get("filename"),
                        )
                        attached += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:200])
                if len(errors) > 20:
                    break

        after = client.fetchone(
            conn, "SELECT count(*)::int AS n FROM ops.eligibility_work_item"
        )
        after_n = int(after["n"]) if after else 0
        created = max(0, after_n - before_n)
        refreshed = max(0, len(visits) - created)

        if etl_id:
            finish_etl_run(
                conn,
                etl_id,
                status="success" if not errors else "partial",
                row_count=len(visits),
                notes=f"source={source}; created~{created}; attached={attached}",
            )

    return {
        "ok": len(errors) == 0,
        "source": source,
        "visit_count": len(visits),
        "created_approx": created,
        "refreshed_approx": refreshed,
        "attachments_linked": attached,
        "errors": errors[:20],
    }
