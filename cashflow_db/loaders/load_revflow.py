"""RevFlow EOB catalog + exports → eob_* and bootstrap claims."""

from __future__ import annotations

import json
from pathlib import Path

from cashflow_db.config import REVFLOW_OUTPUT
from cashflow_db.db import connect, finish_etl_run, start_etl_run
from cashflow_db.loaders.base import ensure_cpt, upsert_patient
from cashflow_db.util import parse_date, parse_money, safe_str

# Reuse existing RevFlow parser when available
try:
    from cashflow_reconcile.parse_revflow_eob import parse_revflow_csv
except ImportError:  # pragma: no cover
    parse_revflow_csv = None


def _manifest_index(root: Path) -> dict[str, dict]:
    path = root / "manifest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    exports = data.get("exports") or []
    out: dict[str, dict] = {}
    for item in exports:
        if not isinstance(item, dict):
            continue
        for key in (
            item.get("path"),
            item.get("filename"),
            item.get("export_path"),
        ):
            if key:
                out[Path(str(key)).name] = item
        # also by eob_key
        if item.get("eob_key"):
            out[str(item["eob_key"])] = item
    return out


def _catalog_entries(root: Path) -> list[dict]:
    path = root / "eob_catalog.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    return [e for e in entries if isinstance(e, dict)]


def load_revflow(
    *,
    root: Path | None = None,
    database_url: str | None = None,
    limit_files: int | None = None,
    bootstrap_claims: bool = True,
) -> dict[str, int]:
    if parse_revflow_csv is None:
        raise RuntimeError("cashflow_reconcile.parse_revflow_eob is required")

    root = root or REVFLOW_OUTPUT
    exports_dir = root / "exports"
    counts = {"eob_checks": 0, "eob_lines": 0, "claims": 0, "claim_lines": 0, "events": 0}

    with connect(database_url) as conn:
        etl_id = start_etl_run(conn, "revflow", str(root))
        try:
            route = conn.execute(
                "SELECT submission_route_id FROM ref.submission_route WHERE code = 'waystar'"
            ).fetchone()
            route_id = str(route["submission_route_id"]) if route else None

            # Prefer catalog for check headers
            catalog_by_key = {
                str(e.get("eob_key")): e for e in _catalog_entries(root) if e.get("eob_key")
            }
            manifest = _manifest_index(root)

            files = sorted(exports_dir.glob("*.csv"))
            if limit_files:
                files = files[:limit_files]

            for path in files:
                meta = manifest.get(path.name) or {}
                eob_key = safe_str(meta.get("eob_key"))
                if eob_key and eob_key in catalog_by_key:
                    cat = catalog_by_key[eob_key]
                    meta = {**cat, **meta}

                payments = parse_revflow_csv(path, manifest_meta=meta or None)
                if not payments:
                    continue

                first = payments[0]
                check_natural = (
                    f"{first.check_eft_num}:{first.eob_date}:{path.name}"
                )
                check = conn.execute(
                    """
                    INSERT INTO billing.eob_check (
                        eob_key, company_id, check_eft_num, payor_raw,
                        check_date, eob_date, report_from, report_to,
                        paid_amount_sum, source_file, source_system,
                        source_natural_key, etl_run_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'revflow', %s, %s::uuid
                    )
                    ON CONFLICT (source_system, source_natural_key)
                        WHERE source_natural_key IS NOT NULL
                    DO UPDATE SET
                        paid_amount_sum = EXCLUDED.paid_amount_sum,
                        payor_raw = COALESCE(EXCLUDED.payor_raw, billing.eob_check.payor_raw),
                        etl_run_id = EXCLUDED.etl_run_id
                    RETURNING eob_check_id
                    """,
                    (
                        safe_str(meta.get("eob_key")) or first.eob_key or None,
                        safe_str(meta.get("company_id")) or first.company_id or None,
                        safe_str(first.check_eft_num) or safe_str(meta.get("check_eft_num")),
                        safe_str(first.payor) or safe_str(meta.get("payor")),
                        parse_date(first.eob_date) or parse_date(meta.get("eob_date")),
                        parse_date(first.eob_date) or parse_date(meta.get("eob_date")),
                        parse_date(first.report_from),
                        parse_date(first.report_to),
                        sum((p.paid_amount or 0) for p in payments),
                        path.name,
                        check_natural,
                        etl_id,
                    ),
                ).fetchone()
                eob_check_id = str(check["eob_check_id"])
                counts["eob_checks"] += 1

                for p in payments:
                    patient_id = upsert_patient(
                        conn,
                        webpt_patient_id=None,
                        patient_name=f"{p.last_name}, {p.first_name}",
                        revflow_patient_id=p.revflow_patient_id or None,
                        etl_run_id=etl_id,
                        source_system="revflow",
                    )
                    ensure_cpt(conn, p.cpt_code or None)
                    pr_codes = []
                    for token in (p.carcs or "").replace(",", " ").split():
                        t = token.strip().upper()
                        if t.startswith("PR-") or t in {"PR1", "PR2", "PR3", "OA23"}:
                            pr_codes.append(t)

                    line_natural = (
                        f"{path.name}:{p.revflow_patient_id}:{p.date_of_service}:"
                        f"{p.cpt_code}:{p.modifier}"
                    )
                    conn.execute(
                        """
                        INSERT INTO billing.eob_line (
                            eob_check_id, revflow_patient_id, patient_id,
                            date_of_service, cpt_code, modifiers, units,
                            billed_amount, allowed_amount, paid_amount,
                            adjustment_amount, deductible_amount, carcs, pr_oa_codes,
                            source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s, %s::uuid, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s,
                            'revflow', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            paid_amount = EXCLUDED.paid_amount,
                            allowed_amount = EXCLUDED.allowed_amount,
                            adjustment_amount = EXCLUDED.adjustment_amount,
                            eob_check_id = EXCLUDED.eob_check_id,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            eob_check_id,
                            safe_str(p.revflow_patient_id),
                            patient_id,
                            parse_date(p.date_of_service),
                            safe_str(p.cpt_code),
                            safe_str(p.modifier),
                            p.units,
                            p.billed_amount,
                            p.allowed_amount,
                            p.paid_amount,
                            p.adjustment_amount,
                            p.deductible_amount,
                            safe_str(p.carcs),
                            ";".join(pr_codes) or None,
                            line_natural,
                            etl_id,
                        ),
                    )
                    counts["eob_lines"] += 1

            if bootstrap_claims:
                # Create claim per (patient_id, DOS) from visit_service_line when present,
                # else from eob_line without claim_line.
                visits = conn.execute(
                    """
                    SELECT v.visit_id, v.patient_id, v.service_date, v.case_pk,
                           sl.service_line_id, sl.cpt_code, sl.modifiers, sl.units
                    FROM core.visit v
                    JOIN core.visit_service_line sl ON sl.visit_id = v.visit_id
                    ORDER BY v.patient_id, v.service_date
                    """
                ).fetchall()
                current_key = None
                claim_id = None
                line_no = 0
                for row in visits:
                    key = (str(row["patient_id"]), row["service_date"])
                    if key != current_key:
                        current_key = key
                        line_no = 0
                        claim = conn.execute(
                            """
                            INSERT INTO billing.claim (
                                case_pk, patient_id, submission_route_id,
                                payer_sequence, service_date_from, service_date_to,
                                status_current, source_system, source_natural_key, etl_run_id
                            )
                            VALUES (
                                %s::uuid, %s::uuid, %s::uuid, 'primary', %s, %s,
                                'created', 'webpt', %s, %s::uuid
                            )
                            ON CONFLICT (source_system, source_natural_key)
                                WHERE source_natural_key IS NOT NULL
                            DO UPDATE SET
                                etl_run_id = EXCLUDED.etl_run_id
                            RETURNING claim_id
                            """,
                            (
                                str(row["case_pk"]) if row["case_pk"] else None,
                                str(row["patient_id"]),
                                route_id,
                                row["service_date"],
                                row["service_date"],
                                f"{row['patient_id']}:{row['service_date']}",
                                etl_id,
                            ),
                        ).fetchone()
                        claim_id = str(claim["claim_id"])
                        counts["claims"] += 1
                        counts["events"] += 1

                    line_no += 1
                    conn.execute(
                        """
                        INSERT INTO billing.claim_line (
                            claim_id, visit_id, service_line_id, line_no,
                            cpt_code, modifiers, units,
                            source_system, source_natural_key, etl_run_id
                        )
                        VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s,
                            'webpt', %s, %s::uuid
                        )
                        ON CONFLICT (source_system, source_natural_key)
                            WHERE source_natural_key IS NOT NULL
                        DO UPDATE SET
                            claim_id = EXCLUDED.claim_id,
                            etl_run_id = EXCLUDED.etl_run_id
                        """,
                        (
                            claim_id,
                            str(row["visit_id"]),
                            str(row["service_line_id"]),
                            line_no,
                            row["cpt_code"],
                            row["modifiers"],
                            row["units"],
                            f"{row['service_line_id']}",
                            etl_id,
                        ),
                    )
                    counts["claim_lines"] += 1

                    # try link eob_line by patient+dos+cpt
                    conn.execute(
                        """
                        UPDATE billing.eob_line el
                        SET claim_line_id = %s::uuid
                        WHERE el.claim_line_id IS NULL
                          AND el.patient_id = %s::uuid
                          AND el.date_of_service = %s
                          AND el.cpt_code = %s
                          AND COALESCE(el.modifiers, '') = COALESCE(%s, '')
                        """,
                        (
                            str(cl["claim_line_id"]),
                            str(row["patient_id"]),
                            row["service_date"],
                            row["cpt_code"],
                            row["modifiers"],
                        ),
                    )

                # Mark era_received when eob linked
                linked = conn.execute(
                    """
                    SELECT DISTINCT cl.claim_id
                    FROM billing.claim_line cl
                    JOIN billing.eob_line el ON el.claim_line_id = cl.claim_line_id
                    """
                ).fetchall()
                for row in linked:
                    conn.execute(
                        """
                        INSERT INTO billing.claim_event (
                            claim_id, event_type, payload, source_system, etl_run_id
                        )
                        VALUES (%s::uuid, 'era_received', '{}'::jsonb, 'revflow', %s::uuid)
                        """,
                        (str(row["claim_id"]), etl_id),
                    )
                    conn.execute(
                        """
                        UPDATE billing.claim
                        SET status_current = 'era_received'
                        WHERE claim_id = %s::uuid
                        """,
                        (str(row["claim_id"]),),
                    )
                    counts["events"] += 1

            finish_etl_run(conn, etl_id, status="success", row_count=sum(counts.values()))
        except Exception as exc:
            finish_etl_run(conn, etl_id, status="failed", notes=str(exc)[:2000])
            raise
    return counts
