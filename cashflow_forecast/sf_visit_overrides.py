"""Apply SF visit-level paid/denied overrides onto reconciliation lines for forecast."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from cashflow_forecast.utils import normalize_name_key, parse_date, parse_money

log = logging.getLogger("cashflow_forecast.sf_visit_overrides")

OVERRIDE_STATUSES = frozenset({"paid", "denied"})


def _has_eob_value(value: object) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "nat", "none"}


def load_sf_override_keys(
    path: Path | str,
    visits_path: Path | str | None = None,
    *,
    require_paid_evidence: bool = False,
) -> dict[tuple[str, date], tuple[str, float]]:
    """
    Load (name_key, DOS) -> (status, paid_total) from audit CSV or visits CSV.

    Prefer sf_status_overrides_applied.csv (only patched keys). Visits fallback
    only keeps paid/denied rows.

    ``visits_path`` / ``require_paid_evidence`` are accepted for call-site
    compatibility; paid-without-EOB softenting is enforced in
    ``apply_sf_visit_overrides`` against line-level ``eob_date``.
    """
    del visits_path, require_paid_evidence  # applied at line-override time
    path = Path(path)
    if not path.exists():
        return {}

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    out: dict[tuple[str, date], tuple[str, float]] = {}

    if "new_status" in df.columns:
        for _, row in df.iterrows():
            status = str(row.get("new_status") or "").strip().lower()
            if status not in OVERRIDE_STATUSES:
                continue
            nk = str(row.get("name_key") or "").strip()
            dos = parse_date(str(row.get("date_of_service") or ""))
            if not nk or not dos:
                continue
            paid = parse_money(str(row.get("new_paid") or "0"))
            out[(nk, dos)] = (status, paid)
        return out

    for _, row in df.iterrows():
        status = str(row.get("visit_status") or "").strip().lower()
        if status not in OVERRIDE_STATUSES:
            continue
        nk = normalize_name_key(str(row.get("patient_name") or ""))
        dos = parse_date(str(row.get("date_of_service") or ""))
        if not nk or not dos:
            continue
        paid_raw = row.get("visit_paid_total") or row.get("total_paid") or "0"
        paid = parse_money(str(paid_raw))
        out[(nk, dos)] = (status, paid)
    return out


def apply_sf_visit_overrides(
    lines: pd.DataFrame,
    overrides: dict[tuple[str, date], tuple[str, float]],
    *,
    require_line_eob_for_paid: bool = True,
) -> pd.DataFrame:
    """
    Mutate a copy of lines:
    - paid: equal-split visit paid across lines; status=paid
      (skipped when require_line_eob_for_paid and no line eob_date — keeps open AR)
    - denied: paid_amount=0; status=denied
    """
    if lines is None or lines.empty or not overrides:
        return lines

    out = lines.copy()
    if "name_key" not in out.columns:
        out["name_key"] = out.get("patient_name", pd.Series([""] * len(out))).map(
            lambda x: normalize_name_key(str(x))
        )
    if "source" not in out.columns:
        out["source"] = "reconciliation"

    out["paid_amount"] = pd.to_numeric(out["paid_amount"], errors="coerce").fillna(0.0)

    touched_keys = 0
    touched_lines = 0
    skipped_paid_keys = 0

    key_to_idxs: dict[tuple[str, date], list[int]] = {}
    for i, row in out.iterrows():
        nk = str(row.get("name_key") or "")
        dos = row.get("date_of_service")
        if isinstance(dos, str):
            dos = parse_date(dos)
        if not nk or not isinstance(dos, date):
            continue
        key = (nk, dos)
        if key not in overrides:
            continue
        key_to_idxs.setdefault(key, []).append(i)

    for key, idxs in key_to_idxs.items():
        status, visit_paid = overrides[key]
        n = len(idxs)
        if n == 0:
            continue

        if status == "denied":
            for i in idxs:
                out.at[i, "status"] = "denied"
                out.at[i, "paid_amount"] = 0.0
                out.at[i, "source"] = "sf_override"
            touched_keys += 1
            touched_lines += n
            continue

        if require_line_eob_for_paid:
            has_eob = False
            if "eob_date" in out.columns:
                has_eob = any(_has_eob_value(out.at[i, "eob_date"]) for i in idxs)
            if not has_eob:
                skipped_paid_keys += 1
                continue

        if n == 1:
            shares = [round(visit_paid, 2)]
        else:
            base = round(visit_paid / n, 2)
            shares = [base] * n
            shares[-1] = round(visit_paid - sum(shares[:-1]), 2)

        for i, share in zip(idxs, shares):
            out.at[i, "status"] = "paid"
            out.at[i, "paid_amount"] = float(share)
            out.at[i, "source"] = "sf_override"
        touched_keys += 1
        touched_lines += n

    if skipped_paid_keys:
        log.info(
            "Skipped %d SF paid visit overrides with no line eob_date (keep open AR)",
            skipped_paid_keys,
        )
    log.info(
        "Applied SF visit overrides to %d lines across %d visits (%d override keys loaded)",
        touched_lines,
        touched_keys,
        len(overrides),
    )
    return out


def resolve_override_path(recon_dir: Path) -> Path | None:
    """Prefer audit CSV; else visits CSV."""
    audit = recon_dir / "sf_status_overrides_applied.csv"
    if audit.exists():
        return audit
    visits = recon_dir / "reconciliation_visits.csv"
    if visits.exists():
        return visits
    return None


def load_sf_emr_override_keys_from_db(
    *,
    database_url: str | None = None,
) -> dict[tuple[str, date], tuple[str, float]]:
    """
    Load (emr_id, DOS) -> (status, paid_total) from analytics.snowflake_visit_kpi.

    paid_total = insurance_payment + co_insurance_payment + client_payment
    (same composition as snowflake_pull.compare_visits).
    """
    try:
        from cashflow_db.repository import connection
    except ImportError:
        log.warning("cashflow_db unavailable — no SF overrides from DB")
        return {}

    sql = """
        SELECT
            emr_id,
            date_of_service,
            LOWER(TRIM(status)) AS status,
            COALESCE(insurance_payment, 0)
              + COALESCE(co_insurance_payment, 0)
              + COALESCE(client_payment, 0) AS total_paid
        FROM analytics.snowflake_visit_kpi
        WHERE LOWER(TRIM(status)) IN ('paid', 'denied')
          AND emr_id IS NOT NULL
          AND date_of_service IS NOT NULL
    """
    out: dict[tuple[str, date], tuple[str, float]] = {}
    with connection(database_url) as conn:
        rows = conn.execute(sql).fetchall()
    for row in rows:
        emr = str(row["emr_id"] or "").strip()
        dos_raw = row["date_of_service"]
        if hasattr(dos_raw, "isoformat"):
            dos = dos_raw if isinstance(dos_raw, date) else dos_raw
            if not isinstance(dos, date):
                dos = parse_date(str(dos_raw))
        else:
            dos = parse_date(str(dos_raw or ""))
        status = str(row["status"] or "").strip().lower()
        if not emr or not dos or status not in OVERRIDE_STATUSES:
            continue
        paid = float(row["total_paid"] or 0)
        out[(emr, dos)] = (status, paid)
    log.info("Loaded %d SF paid/denied override keys from snowflake_visit_kpi", len(out))
    return out


def remap_emr_overrides_to_name_keys(
    lines: pd.DataFrame,
    emr_overrides: dict[tuple[str, date], tuple[str, float]],
) -> dict[tuple[str, date], tuple[str, float]]:
    """
    Map (emr_id, DOS) overrides onto (name_key, DOS) using recon line identity.

    Prefers ``webpt_patient_id`` (EMR) on lines; falls back to normalizing
    ``patient_name`` only when EMR is absent (weaker; skipped if emr overrides
    cannot match).
    """
    if lines is None or lines.empty or not emr_overrides:
        return {}

    out: dict[tuple[str, date], tuple[str, float]] = {}
    has_emr = "webpt_patient_id" in lines.columns
    for _, row in lines.iterrows():
        dos = row.get("date_of_service")
        if isinstance(dos, str):
            dos = parse_date(dos)
        if not isinstance(dos, date):
            continue
        emr = ""
        if has_emr:
            emr = str(row.get("webpt_patient_id") or "").strip()
        if not emr:
            continue
        hit = emr_overrides.get((emr, dos))
        if hit is None:
            continue
        nk = str(row.get("name_key") or "").strip()
        if not nk:
            nk = normalize_name_key(str(row.get("patient_name") or ""))
        if not nk:
            continue
        # Prefer paid over denied if duplicate keys collide
        prev = out.get((nk, dos))
        if prev is None or (hit[0] == "paid" and prev[0] != "paid"):
            out[(nk, dos)] = hit
    log.info(
        "Remapped %d EMR SF overrides onto %d name_key visit keys",
        len(emr_overrides),
        len(out),
    )
    return out


def load_sf_override_keys_from_db(
    lines: pd.DataFrame,
    *,
    database_url: str | None = None,
) -> dict[tuple[str, date], tuple[str, float]]:
    """Load SF paid/denied overrides from KPI and key them for recon lines."""
    emr_keys = load_sf_emr_override_keys_from_db(database_url=database_url)
    return remap_emr_overrides_to_name_keys(lines, emr_keys)
