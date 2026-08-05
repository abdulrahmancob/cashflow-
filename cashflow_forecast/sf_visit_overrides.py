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
