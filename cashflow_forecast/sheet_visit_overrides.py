"""Apply Eligibility Sheet paid/denied onto reconciliation lines.

Waystar paid lines are left alone. The sheet decides every other visit:
paid and deduct close the visit, denied zeros it, and anything else stays open.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime

import pandas as pd

from cashflow_db.services.waystar_recon import split_paid_across_lines
from cashflow_forecast.utils import parse_date

log = logging.getLogger("cashflow_forecast.sheet_visit_overrides")

SHEET_PAID_STATUSES = frozenset({"paid", "deduct"})
SHEET_DENIED_STATUSES = frozenset({"denied"})
_EOB_FIELDS = ("insurance_check_date", "tracker_date", "posting_date_1")

SheetOverride = tuple[str, float, date | None]


def sheet_overrides_enabled() -> bool:
    """False when the forecast must ignore the Eligibility Sheet."""
    raw = (os.getenv("CASHFLOW_FORECAST_DISABLE_SHEET_OVERRIDES") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return parse_date(str(value))


def _sheet_eob_date(row: dict) -> date | None:
    for key in _EOB_FIELDS:
        found = _as_date(row.get(key))
        if found is not None:
            return found
    return None


def sheet_override_from_row(
    row: dict,
    *,
    as_of: date,
    backtest: bool,
) -> tuple[tuple[str, date], SheetOverride] | None:
    """One visit override, or None when the sheet leaves the visit open.

    A backtest keeps a paid row only when its check date is on or before
    ``as_of``. A paid row with no date is skipped in a backtest and applied
    in a live run.
    """
    emr = str(row.get("emr_patient_id") or "").strip()
    dos = _as_date(row.get("dos"))
    if not emr or dos is None:
        return None
    status = str(row.get("source_visit_status") or "").strip().lower()
    eob = _sheet_eob_date(row)
    if status in SHEET_PAID_STATUSES:
        if backtest and (eob is None or eob > as_of):
            return None
        try:
            paid = float(row.get("total_amount") or 0)
        except (TypeError, ValueError):
            paid = 0.0
        return (emr, dos), ("paid", paid, eob)
    if status in SHEET_DENIED_STATUSES:
        return (emr, dos), ("denied", 0.0, eob)
    return None


def remember_sheet_override(
    out: dict[tuple[str, date], SheetOverride],
    key: tuple[str, date],
    value: SheetOverride,
) -> None:
    """Paid wins over a second row for the same visit."""
    prev = out.get(key)
    if prev is None or (value[0] == "paid" and prev[0] != "paid"):
        out[key] = value


def sheet_override_counts(
    overrides: dict[tuple[str, date], SheetOverride],
) -> tuple[int, int, int]:
    """(visits, paid, denied)."""
    paid = sum(1 for status, _amount, _eob in overrides.values() if status == "paid")
    denied = sum(1 for status, _amount, _eob in overrides.values() if status == "denied")
    return len(overrides), paid, denied


def sheet_paid_visit_keys(
    overrides: dict[tuple[str, date], SheetOverride],
) -> set[tuple[str, date]]:
    return {key for key, (status, _amount, _eob) in overrides.items() if status == "paid"}


_CACHED_SHEET_ROWS: list[dict] | None = None


def prime_forecast_sheet_rows(rows: list[dict] | None) -> None:
    """Reuse one sheet read across a replay process. None clears the cache."""
    global _CACHED_SHEET_ROWS
    _CACHED_SHEET_ROWS = rows


def load_forecast_sheet_rows(*, database_url: str | None = None) -> list[dict]:
    """Effective sheet rows, read once. Does not write the sheet."""
    from cashflow_db.repository import connection
    from cashflow_db.repository.eligibility import list_forecast_sheet_visits

    with connection(database_url) as conn:
        return list_forecast_sheet_visits(conn)


def load_sheet_visit_overrides(
    *,
    as_of: date,
    backtest: bool,
    database_url: str | None = None,
    sheet_rows: list[dict] | None = None,
) -> dict[tuple[str, date], SheetOverride]:
    """(emr, DOS) -> (status, paid_total, eob_date) from the effective sheet."""
    if not sheet_overrides_enabled():
        log.info(
            "Eligibility sheet overrides disabled (CASHFLOW_FORECAST_DISABLE_SHEET_OVERRIDES)"
        )
        return {}
    rows = sheet_rows if sheet_rows is not None else _CACHED_SHEET_ROWS
    if rows is None:
        rows = load_forecast_sheet_rows(database_url=database_url)
    out: dict[tuple[str, date], SheetOverride] = {}
    for row in rows:
        built = sheet_override_from_row(row, as_of=as_of, backtest=backtest)
        if built is None:
            continue
        key, value = built
        remember_sheet_override(out, key, value)
    n, paid, denied = sheet_override_counts(out)
    log.info(
        "Eligibility sheet overrides: visits=%d paid=%d denied=%d (sheet_rows=%d)",
        n,
        paid,
        denied,
        len(rows),
    )
    return out


def _line_units(value: object) -> int:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 1
        return max(int(float(value)), 1)
    except (TypeError, ValueError):
        return 1


def _line_dos(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return parse_date(str(value))


def apply_sheet_visit_overrides_reference(
    lines: pd.DataFrame,
    overrides: dict[tuple[str, date], SheetOverride],
) -> pd.DataFrame:
    """Row-wise sheet apply. Kept so tests can prove the fast path matches it."""
    if lines is None or lines.empty or not overrides:
        return lines
    if "webpt_patient_id" not in lines.columns or "date_of_service" not in lines.columns:
        return lines

    out = lines.copy()
    if "status" not in out.columns:
        out["status"] = ""
    if "paid_amount" not in out.columns:
        out["paid_amount"] = 0.0
    if "source" not in out.columns:
        out["source"] = "reconciliation"
    if "units" not in out.columns:
        out["units"] = 1.0
    out["paid_amount"] = pd.to_numeric(out["paid_amount"], errors="coerce").fillna(0.0)

    groups: dict[tuple[str, date], list] = {}
    for idx, row in out.iterrows():
        if str(row.get("status") or "").strip().lower() == "paid":
            continue
        emr = str(row.get("webpt_patient_id") or "").strip()
        dos = _line_dos(row.get("date_of_service"))
        if not emr or dos is None:
            continue
        if (emr, dos) not in overrides:
            continue
        groups.setdefault((emr, dos), []).append(idx)

    touched_lines = 0
    for key, idxs in groups.items():
        status, visit_paid, eob = overrides[key]
        if status == "denied":
            for idx in idxs:
                out.at[idx, "status"] = "denied"
                out.at[idx, "paid_amount"] = 0.0
                out.at[idx, "source"] = "eligibility_sheet"
            touched_lines += len(idxs)
            continue
        shares = split_paid_across_lines(
            float(visit_paid or 0),
            [_line_units(out.at[idx, "units"]) for idx in idxs],
        )
        for idx, share in zip(idxs, shares):
            out.at[idx, "status"] = "paid"
            out.at[idx, "paid_amount"] = float(share)
            out.at[idx, "source"] = "eligibility_sheet"
            if eob is not None:
                out.at[idx, "eob_date"] = eob
        touched_lines += len(idxs)

    log.info(
        "Applied eligibility sheet overrides to %d lines across %d visits",
        touched_lines,
        len(groups),
    )
    return out


def apply_sheet_visit_overrides(
    lines: pd.DataFrame,
    overrides: dict[tuple[str, date], SheetOverride],
) -> pd.DataFrame:
    """Mark non-Waystar lines paid or denied from the sheet.

    A line whose status is already ``paid`` is a Waystar paid line and is
    not changed. Paid dollars are split across the visit's remaining CPT
    lines by units, in dataframe order, same as ``split_paid_across_lines``.
    """
    if lines is None or lines.empty or not overrides:
        return lines
    if "webpt_patient_id" not in lines.columns or "date_of_service" not in lines.columns:
        return lines

    out = lines.copy()
    if "status" not in out.columns:
        out["status"] = ""
    if "paid_amount" not in out.columns:
        out["paid_amount"] = 0.0
    if "source" not in out.columns:
        out["source"] = "reconciliation"
    if "units" not in out.columns:
        out["units"] = 1.0
    out["paid_amount"] = pd.to_numeric(out["paid_amount"], errors="coerce").fillna(0.0)

    emr = out["webpt_patient_id"].map(lambda value: "" if pd.isna(value) else str(value).strip())
    dos = pd.to_datetime(out["date_of_service"], errors="coerce")
    status = out["status"].astype(str).str.strip().str.lower()
    eligible = (status != "paid") & emr.ne("") & dos.notna()
    if not bool(eligible.any()):
        log.info("Applied eligibility sheet overrides to 0 lines across 0 visits")
        return out

    left = pd.DataFrame(
        {
            "emr": emr.to_numpy(),
            "dos": dos.dt.date.to_numpy(),
            "_ix": out.index.to_numpy(),
            "_units": out["units"].map(_line_units).to_numpy(),
            "_pos": range(len(out)),
        }
    ).loc[eligible.to_numpy()]
    ov = pd.DataFrame(
        {
            "emr": [key[0] for key in overrides],
            "dos": [key[1] for key in overrides],
            "ov_status": [value[0] for value in overrides.values()],
            "ov_paid": [value[1] for value in overrides.values()],
            "ov_eob": [value[2] for value in overrides.values()],
        }
    )
    hit = left.merge(ov, on=["emr", "dos"], how="inner")
    if hit.empty:
        log.info("Applied eligibility sheet overrides to 0 lines across 0 visits")
        return out
    hit = hit.sort_values("_pos", kind="mergesort")

    denied = hit["ov_status"].eq("denied")
    if bool(denied.any()):
        denied_ix = hit.loc[denied, "_ix"]
        out.loc[denied_ix, "status"] = "denied"
        out.loc[denied_ix, "paid_amount"] = 0.0
        out.loc[denied_ix, "source"] = "eligibility_sheet"

    paid = hit.loc[~denied]
    visits = 0
    if not paid.empty:
        if "eob_date" not in out.columns:
            out["eob_date"] = None
        for (_emr, _dos), group in paid.groupby(["emr", "dos"], sort=False):
            visits += 1
            shares = split_paid_across_lines(
                float(group["ov_paid"].iloc[0] or 0),
                [int(unit) for unit in group["_units"].tolist()],
            )
            eob = group["ov_eob"].iloc[0]
            set_eob = eob is not None and not (isinstance(eob, float) and pd.isna(eob))
            for ix, share in zip(group["_ix"].tolist(), shares):
                out.at[ix, "status"] = "paid"
                out.at[ix, "paid_amount"] = float(share)
                out.at[ix, "source"] = "eligibility_sheet"
                if set_eob:
                    out.at[ix, "eob_date"] = eob
    denied_visits = int(hit.loc[denied, ["emr", "dos"]].drop_duplicates().shape[0]) if bool(denied.any()) else 0
    log.info(
        "Applied eligibility sheet overrides to %d lines across %d visits",
        len(hit),
        visits + denied_visits,
    )
    return out
