"""FastAPI backend for the React forecast dashboard.

Run from repo root:
  python -m cashflow_forecast.api
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.dashboard_insights import (  # noqa: E402
    build_insight_cards,
    facility_severity_matrix,
    filter_audit,
    icd_category_breakdown,
    icd_guidance_samples,
    load_audit_bundle,
    risk_audit_exposure,
    top_cpt_rules,
    unmapped_ranked,
)

DEFAULT_FORECAST = _REPO / "webpt_edco_scraper/output/jun_jul_2026/forecast"
DEFAULT_AUDIT = _REPO / "webpt_edco_scraper/output/jun_jul_2026/audit"

app = FastAPI(title="RCM Platform API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Platform ops + auth + eligibility under /api/v1 and /api (alias)
try:
    from cashflow_ops.api import router as _ops_router
    from cashflow_ops.auth_api import router as _auth_router
    from cashflow_ops.eligibility_api import router as _eligibility_router
    from cashflow_ops.tracker_api import router as _tracker_router
    from cashflow_ops.security import seed_portal_users

    app.include_router(_ops_router, prefix="/api/v1")
    app.include_router(_ops_router, prefix="/api")
    app.include_router(_auth_router, prefix="/api/v1")
    app.include_router(_auth_router, prefix="/api")
    app.include_router(_eligibility_router, prefix="/api/v1")
    app.include_router(_eligibility_router, prefix="/api")
    app.include_router(_tracker_router, prefix="/api/v1")
    app.include_router(_tracker_router, prefix="/api")

    @app.on_event("startup")
    def _portal_startup() -> None:
        try:
            seed_portal_users()
        except Exception:  # noqa: BLE001
            pass

except Exception:  # noqa: BLE001 — forecast API still works without ops
    pass


# Protect forecast /api/* routes (finance + super_admin). Auth/login and
# eligibility enforce their own deps. Public: /alive, /ready, /docs, /openapi.
@app.middleware("http")
async def _forecast_rbac(request, call_next):  # type: ignore[no-untyped-def]
    path = request.url.path
    public_prefixes = (
        "/alive",
        "/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/auth/login",
        "/api/v1/auth/login",
    )
    if any(path == p or path.startswith(p + "/") for p in public_prefixes):
        return await call_next(request)
    # Eligibility + auth/users already use Depends — skip double-check noise for OPTIONS
    if request.method == "OPTIONS":
        return await call_next(request)
    # Forecast data endpoints under /api (not auth/eligibility/ops)
    forecast_prefixes = (
        "/api/kpi",
        "/api/projected",
        "/api/actual",
        "/api/outcomes",
        "/api/insights",
        "/api/drill",
        "/api/meta",
        "/api/v1/kpi",
        "/api/v1/projected",
        "/api/v1/actual",
        "/api/v1/outcomes",
        "/api/v1/insights",
        "/api/v1/drill",
        "/api/v1/meta",
    )
    if not any(path.startswith(p) for p in forecast_prefixes):
        return await call_next(request)
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    try:
        from cashflow_ops.security import ROLE_FINANCE, ROLE_SUPER, decode_access_token

        payload = decode_access_token(auth.split(" ", 1)[1].strip())
        roles = list(payload.get("roles") or [])
        if ROLE_SUPER not in roles and ROLE_FINANCE not in roles:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Insufficient permissions"}, status_code=403)
    except Exception as exc:  # noqa: BLE001
        from fastapi.responses import JSONResponse

        detail = getattr(exc, "detail", "Invalid or expired token")
        return JSONResponse({"detail": detail}, status_code=401)
    return await call_next(request)


@app.get("/alive")
def alive() -> dict[str, str]:
    """Liveness: process is up (no dependency checks)."""
    return {"status": "alive"}


@app.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness: PostgreSQL + repository reachable. Returns 503 when not ready."""
    from fastapi import HTTPException

    try:
        from cashflow_db.repository import connection

        with connection() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            if not row or int(row.get("ok", 0)) != 1:
                raise HTTPException(
                    status_code=503,
                    detail={"status": "not_ready", "reason": "db_probe_failed"},
                )
            # Light repository touch when migrations applied
            try:
                conn.execute("SELECT 1 FROM ops.pipeline_run LIMIT 1")
            except Exception:
                pass
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "reason": str(exc)},
        ) from exc


def _forecast_dir() -> Path:
    return Path(os.environ.get("FORECAST_DIR", str(DEFAULT_FORECAST)))


def _audit_dir() -> Path:
    return Path(os.environ.get("AUDIT_DIR", str(DEFAULT_AUDIT)))


def _use_db() -> bool:
    """Product default: DB. Set CASHFLOW_FORECAST_FROM_DB=0 to force CSV legacy."""
    flag = os.environ.get("CASHFLOW_FORECAST_FROM_DB", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _prefer(base: str) -> Path:
    d = _forecast_dir()
    may = d / f"{base}_may_aug.csv"
    if may.exists():
        return may
    return d / f"{base}.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _read_feature(kind: str, csv_base: str | None = None) -> pd.DataFrame:
    if _use_db():
        try:
            from cashflow_forecast.db_source import load_feature_df

            df = load_feature_df(kind)
            if not df.empty:
                return df.astype(str)
        except Exception:
            pass
    if csv_base:
        return _read_csv(_prefer(csv_base))
    return pd.DataFrame()


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df if limit is None else df.head(limit)
    # Convert NaN to None for JSON
    return json.loads(out.to_json(orient="records", date_format="iso"))


def _file_mtime_key(path: Path) -> str:
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return "0"


def _outcomes_cache_key() -> str:
    """Invalidate when outcome_stages rebuild or DB mode toggles."""
    if _use_db():
        return f"db|{os.environ.get('CASHFLOW_DATABASE_URL', 'default')}"
    d = _forecast_dir()
    return f"{d}|{_file_mtime_key(d / 'outcome_stages.csv')}"


def _risk_cache_key() -> str:
    if _use_db():
        return f"db-risk|{os.environ.get('CASHFLOW_DATABASE_URL', 'default')}"
    d = _forecast_dir()
    return f"{d}|{_file_mtime_key(d / 'risk_flags.csv')}"


@lru_cache(maxsize=4)
def _cached_outcomes(cache_key: str) -> pd.DataFrame:
    if cache_key.startswith("db|"):
        try:
            from cashflow_forecast.db_source import load_outcome_stages_latest_df

            df = load_outcome_stages_latest_df()
            if not df.empty and "expected_amount" in df.columns:
                df["expected_amount"] = pd.to_numeric(
                    df["expected_amount"], errors="coerce"
                ).fillna(0.0)
            return df
        except Exception:
            pass
    forecast_dir = Path(cache_key.rsplit("|", 1)[0])
    df = _read_csv(forecast_dir / "outcome_stages.csv")
    if not df.empty and "expected_amount" in df.columns:
        df["expected_amount"] = pd.to_numeric(df["expected_amount"], errors="coerce").fillna(0.0)
    return df


@lru_cache(maxsize=4)
def _cached_risk(cache_key: str) -> pd.DataFrame:
    if cache_key.startswith("db-risk|"):
        df = _read_feature("risk_flags", "risk_flags")
        if not df.empty and "exposure_amount" in df.columns:
            df["exposure_amount"] = pd.to_numeric(
                df["exposure_amount"], errors="coerce"
            ).fillna(0.0)
        return df
    forecast_dir = Path(cache_key.rsplit("|", 1)[0])
    df = _read_csv(forecast_dir / "risk_flags.csv")
    if not df.empty and "exposure_amount" in df.columns:
        df["exposure_amount"] = pd.to_numeric(df["exposure_amount"], errors="coerce").fillna(0.0)
    return df


def _split_multi(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_iso_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _month_bounds(ym: str) -> tuple[date, date] | None:
    """YYYY-MM → (first day, last day)."""
    try:
        start = datetime.strptime(ym.strip() + "-01", "%Y-%m-%d").date()
    except ValueError:
        return None
    if start.month == 12:
        end = date(start.year, 12, 31)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def _resolve_date_bounds(
    date_from: str | None,
    date_to: str | None,
    months: list[str] | None = None,
) -> tuple[date | None, date | None]:
    """date_from/to win; else union of selected months."""
    d0 = _parse_iso_date(date_from)
    d1 = _parse_iso_date(date_to)
    if d0 or d1:
        return d0, d1
    if not months:
        return None, None
    starts: list[date] = []
    ends: list[date] = []
    for ym in months:
        b = _month_bounds(ym)
        if b:
            starts.append(b[0])
            ends.append(b[1])
    if not starts:
        return None, None
    return min(starts), max(ends)


def _series_in_range(series: pd.Series, d0: date | None, d1: date | None) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    mask = dt.notna()
    if d0 is not None:
        mask &= dt.dt.date >= d0
    if d1 is not None:
        mask &= dt.dt.date <= d1
    return mask


def _filter_period_column(
    df: pd.DataFrame, d0: date | None, d1: date | None
) -> pd.DataFrame:
    """Keep rows whose `period` (YYYY-MM-DD) falls in [d0, d1]."""
    if df.empty or (d0 is None and d1 is None) or "period" not in df.columns:
        return df
    return df.loc[_series_in_range(df["period"], d0, d1)].copy()


def _match_facility_names(series: pd.Series, fac: list[str]) -> pd.Series:
    if "" in fac or "(blank)" in fac:
        blank = series.astype(str).str.strip().eq("")
        named = series.isin([f for f in fac if f and f != "(blank)"])
        return blank | named
    return series.isin(fac)


def _actual_from_ledger(
    *,
    facility: str | None = None,
    ins: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    month: str | None = None,
) -> pd.DataFrame:
    """Daily actual cash from payments ledger CSVs (not outcome_stages).

    Returns columns: period, amount, line_count (aggregated by day).
    """
    fac, insurers = _split_multi(facility), _split_multi(ins)
    months = _split_multi(month)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)

    if fac:
        df = _read_feature("actual_cash_daily_by_facility", "actual_cash_daily_by_facility")
        if df.empty:
            df = _read_feature("actual_cash_daily", "actual_cash_daily")
        elif "facility_name" in df.columns:
            df = df.loc[_match_facility_names(df["facility_name"], fac)]
    elif insurers:
        df = _read_feature("actual_cash_daily_by_insurance", "actual_cash_daily_by_insurance")
        if df.empty:
            df = _read_feature("actual_cash_daily", "actual_cash_daily")
        elif "ins_name" in df.columns:
            df = df[df["ins_name"].isin(insurers)]
    else:
        df = _read_feature("actual_cash_daily", "actual_cash_daily")

    if df.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])

    df = df.copy()
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    if "line_count" in df.columns:
        df["line_count"] = pd.to_numeric(df["line_count"], errors="coerce").fillna(0).astype(int)
    else:
        df["line_count"] = 0

    df = _filter_period_column(df, d0, d1)
    if df.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])

    # Dimensional files → roll up to daily period for charts / KPI sum
    if "facility_name" in df.columns or "ins_name" in df.columns:
        g = (
            df.groupby("period", as_index=False)
            .agg(amount=("amount", "sum"), line_count=("line_count", "sum"))
            .sort_values("period")
        )
        g["amount"] = g["amount"].round(2)
        return g

    df = df.sort_values("period")
    df["amount"] = df["amount"].round(2)
    return df[["period", "amount", "line_count"]]


def _filter_outcomes_by_dates(
    df: pd.DataFrame,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    months: list[str] | None = None,
) -> pd.DataFrame:
    """Filter by cash dates: scheduled land day for AR stages, eob_date for paid.

    Non-paid stages use original_forecast_date when present (pre-pack schedule)
    so Mission Control Expected land still shows on days before as_of.
    """
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if df.empty or (d0 is None and d1 is None):
        return df
    land_col = (
        "original_forecast_date"
        if "original_forecast_date" in df.columns
        else "forecast_date"
    )
    if land_col not in df.columns and "eob_date" not in df.columns:
        return df
    if land_col in df.columns:
        land_ok = _series_in_range(df[land_col], d0, d1)
        if land_col == "original_forecast_date" and "forecast_date" in df.columns:
            # Rows missing original fall back to packed forecast_date.
            miss = pd.to_datetime(df[land_col], errors="coerce").isna()
            if miss.any():
                land_ok = land_ok | (miss & _series_in_range(df["forecast_date"], d0, d1))
    else:
        land_ok = pd.Series(False, index=df.index)
    ed_ok = (
        _series_in_range(df["eob_date"], d0, d1)
        if "eob_date" in df.columns
        else pd.Series(False, index=df.index)
    )
    paid = df.get("outcome_stage", pd.Series("", index=df.index)).astype(str).eq("paid")
    # paid → eob_date; others → scheduled land date (fallback eob)
    keep = (~paid & (land_ok | ed_ok)) | (paid & ed_ok) | (paid & ~ed_ok & land_ok)
    return df.loc[keep].copy()


def _land_date_col(outcomes: pd.DataFrame) -> str:
    if "original_forecast_date" in outcomes.columns:
        return "original_forecast_date"
    return "forecast_date"


def _filter_outcomes(
    df: pd.DataFrame,
    *,
    facility: list[str],
    ins: list[str],
    stage: list[str],
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if facility and "facility_name" in out.columns:
        # Support blank facility selection
        if "" in facility or "(blank)" in facility:
            blank = out["facility_name"].astype(str).str.strip().eq("")
            named = out["facility_name"].isin([f for f in facility if f and f != "(blank)"])
            out = out[blank | named]
        else:
            out = out[out["facility_name"].isin(facility)]
    if ins and "ins_name" in out.columns:
        out = out[out["ins_name"].isin(ins)]
    if stage and "outcome_stage" in out.columns:
        out = out[out["outcome_stage"].isin(stage)]
    return out


def _filter_risk(
    df: pd.DataFrame,
    *,
    facility: list[str],
    ins: list[str],
    risk_flags: list[str],
    date_from: str | None = None,
    date_to: str | None = None,
    months: list[str] | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if facility and "facility_name" in out.columns:
        if "" in facility or "(blank)" in facility:
            blank = out["facility_name"].astype(str).str.strip().eq("")
            named = out["facility_name"].isin([f for f in facility if f and f != "(blank)"])
            out = out[blank | named]
        else:
            out = out[out["facility_name"].isin(facility)]
    if ins and "ins_name" in out.columns:
        out = out[out["ins_name"].isin(ins)]
    if risk_flags and "risk_flag" in out.columns:
        out = out[out["risk_flag"].isin(risk_flags)]
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if d0 is not None or d1 is not None:
        date_col = None
        if "date_of_service" in out.columns:
            date_col = "date_of_service"
        elif "forecast_date" in out.columns:
            date_col = "forecast_date"
        if date_col is not None:
            out = out.loc[_series_in_range(out[date_col], d0, d1)]
    return out


_PROJECT_STAGES = ("on_track", "overdue")
_CASH_LAND_STAGES = ("on_track", "overdue")


def _month_from_forecast_date(series: pd.Series) -> pd.Series:
    """Parse forecast_date / eob_date to YYYY-MM."""
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.strftime("%Y-%m")


def _projected_from_outcomes(
    outcomes: pd.DataFrame,
    *,
    months: list[str] | None = None,
) -> pd.DataFrame:
    """Sum expected_amount by forecast month for projectable stages."""
    if outcomes.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    land_col = _land_date_col(outcomes)
    proj = outcomes[
        outcomes["outcome_stage"].isin(_PROJECT_STAGES)
        & outcomes[land_col].notna()
        & (outcomes["expected_amount"] > 0)
    ].copy()
    if proj.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    proj["period"] = _month_from_forecast_date(proj[land_col])
    proj = proj[proj["period"].notna() & proj["period"].ne("NaT")]
    if months:
        proj = proj[proj["period"].isin(months)]
    g = (
        proj.groupby("period", as_index=False)
        .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
        .sort_values("period")
    )
    g["amount"] = g["amount"].round(2)
    return g


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "forecast": str(_forecast_dir()), "audit": str(_audit_dir())}


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, Any]:
    outcomes = _cached_outcomes(_outcomes_cache_key())
    risk = _cached_risk(_risk_cache_key())
    monthly = _read_feature("projected_cash_monthly", "projected_cash_monthly")
    date_min, date_max = "2026-01-01", "2026-08-31"
    if not outcomes.empty:
        dates: list[pd.Timestamp] = []
        for col in ("original_forecast_date", "forecast_date", "eob_date", "date_of_service"):
            if col in outcomes.columns:
                dt = pd.to_datetime(outcomes[col], errors="coerce").dropna()
                if not dt.empty:
                    dates.append(dt.min())
                    dates.append(dt.max())
        if dates:
            date_min = min(dates).strftime("%Y-%m-%d")
            date_max = max(dates).strftime("%Y-%m-%d")
    return {
        "facilities": sorted(outcomes["facility_name"].dropna().unique().tolist())
        if not outcomes.empty and "facility_name" in outcomes.columns
        else [],
        "insurers": sorted(outcomes["ins_name"].dropna().unique().tolist())
        if not outcomes.empty and "ins_name" in outcomes.columns
        else [],
        "stages": sorted(outcomes["outcome_stage"].dropna().unique().tolist())
        if not outcomes.empty and "outcome_stage" in outcomes.columns
        else [],
        "risk_flags": sorted(risk["risk_flag"].dropna().unique().tolist())
        if not risk.empty and "risk_flag" in risk.columns
        else [],
        "months": sorted(monthly["period"].astype(str).unique().tolist())
        if not monthly.empty and "period" in monthly.columns
        else [],
        "date_min": date_min,
        "date_max": date_max,
        "severities": ["error", "warning"],
    }


def _scoped_outcomes(
    *,
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    outcomes = _filter_outcomes(
        _cached_outcomes(_outcomes_cache_key()),
        facility=_split_multi(facility),
        ins=_split_multi(ins),
        stage=_split_multi(stage),
    )
    return _filter_outcomes_by_dates(
        outcomes,
        date_from=date_from,
        date_to=date_to,
        months=_split_multi(month),
    )


@app.get("/api/kpi")
def kpi(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    path = _forecast_dir() / "kpi_summary.json"
    base = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    fac, insurers, stages = _split_multi(facility), _split_multi(ins), _split_multi(stage)
    months = _split_multi(month)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    filtered = bool(fac or insurers or stages or months or d0 or d1)

    outcomes = _scoped_outcomes(
        facility=facility,
        ins=ins,
        stage=stage,
        month=month,
        date_from=date_from,
        date_to=date_to,
    )
    risk = _filter_risk(
        _cached_risk(_risk_cache_key()),
        facility=fac,
        ins=insurers,
        risk_flags=[],
        date_from=date_from,
        date_to=date_to,
        months=months,
    )

    def _sum_stage(s: str) -> tuple[float, int]:
        if outcomes.empty:
            return 0.0, 0
        m = outcomes["outcome_stage"] == s
        return float(outcomes.loc[m, "expected_amount"].sum()), int(m.sum())

    on_amt, on_n = _sum_stage("on_track")
    ov_amt, ov_n = _sum_stage("overdue")
    den_amt, den_n = _sum_stage("denied")
    rej_amt, rej_n = _sum_stage("rejected")
    _, paid_n = _sum_stage("paid")

    actual_global = float(base.get("actual_cash_received") or 0)
    # Actual cash from payments ledger (not outcome paid lines)
    ledger_scoped = bool(fac or insurers or months or d0 or d1)
    if ledger_scoped:
        ledger = _actual_from_ledger(
            facility=facility,
            ins=ins,
            date_from=date_from,
            date_to=date_to,
            month=month,
        )
        actual_filtered = (
            round(float(ledger["amount"].sum()), 2) if not ledger.empty else 0.0
        )
    else:
        actual_filtered = actual_global

    may_aug = _projected_from_outcomes(outcomes)
    if not may_aug.empty:
        may_aug = may_aug[
            may_aug["period"].isin(
                [
                    "2026-01",
                    "2026-02",
                    "2026-03",
                    "2026-04",
                    "2026-05",
                    "2026-06",
                    "2026-07",
                    "2026-08",
                ]
            )
        ]
    # When filtered: sum scoped projection only (0 if empty — never fall back to global).
    # Unfiltered: prefer kpi_summary.json window total (key kept as projected_cash_may_aug).
    if filtered:
        may_aug_total = float(may_aug["amount"].sum()) if not may_aug.empty else 0.0
    else:
        may_aug_total = float(
            base.get("projected_cash_may_aug")
            or (may_aug["amount"].sum() if not may_aug.empty else 0)
        )

    return {
        **base,
        "actual_cash_received": actual_global,
        "actual_cash_received_filtered": actual_filtered,
        "on_track_amount": round(on_amt, 2),
        "on_track_count": on_n,
        "overdue_amount": round(ov_amt, 2),
        "overdue_count": ov_n,
        "denied_amount": round(den_amt + rej_amt, 2),
        "denied_count": den_n + rej_n,
        "paid_count": paid_n,
        "projected_cash_in": round(on_amt + ov_amt, 2),
        "projected_cash_may_aug": round(may_aug_total, 2),
        "risk_exposure_amount": round(float(risk["exposure_amount"].sum()), 2)
        if not risk.empty
        else 0,
        "risk_visit_count": int(risk["webpt_patient_id"].nunique())
        if not risk.empty and "webpt_patient_id" in risk.columns
        else 0,
        "filtered": filtered,
        "date_from": d0.isoformat() if d0 else None,
        "date_to": d1.isoformat() if d1 else None,
    }


@app.get("/api/projected/monthly")
def projected_monthly(
    month: str | None = None,
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    months = _split_multi(month)
    fac, insurers, stages = _split_multi(facility), _split_multi(ins), _split_multi(stage)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)

    if fac or insurers or stages or d0 or d1:
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            stage=stage,
            month=None if (date_from or date_to) else month,
            date_from=date_from,
            date_to=date_to,
        )
        # Already date-filtered; roll up by month (no second month filter if day range set)
        return _records(
            _projected_from_outcomes(
                outcomes, months=None if (date_from or date_to) else (months or None)
            )
        )

    df = _read_csv(_prefer("projected_cash_monthly"))
    if df.empty:
        return []
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    if months:
        df = df[df["period"].astype(str).isin(months)]
    return _records(df)


@app.get("/api/projected/daily")
def projected_daily(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    fac, insurers, stages = _split_multi(facility), _split_multi(ins), _split_multi(stage)
    d0, d1 = _resolve_date_bounds(date_from, date_to, _split_multi(month))
    if fac or insurers or stages or d0 or d1:
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            stage=stage,
            month=None if (date_from or date_to) else month,
            date_from=date_from,
            date_to=date_to,
        )
        proj = outcomes[
            outcomes["outcome_stage"].isin(_PROJECT_STAGES)
            & outcomes[_land_date_col(outcomes)].notna()
            & (outcomes["expected_amount"] > 0)
        ].copy()
        if proj.empty:
            return []
        land_col = _land_date_col(proj)
        proj["period"] = pd.to_datetime(proj[land_col], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )
        g = (
            proj.groupby("period", as_index=False)
            .agg(amount=("expected_amount", "sum"), line_count=("expected_amount", "count"))
            .sort_values("period")
        )
        g["amount"] = g["amount"].round(2)
        return _records(g)

    df = _read_csv(_prefer("projected_cash_daily"))
    if not df.empty:
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
        if d0 or d1:
            dt = pd.to_datetime(df["period"], errors="coerce")
            mask = dt.notna()
            if d0:
                mask &= dt.dt.date >= d0
            if d1:
                mask &= dt.dt.date <= d1
            df = df.loc[mask]
    return _records(df)


@app.get("/api/projected/by-facility")
def projected_by_facility(
    month: str | None = None,
    facility: str | None = None,
    ins: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    months, fac, insurers = _split_multi(month), _split_multi(facility), _split_multi(ins)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if insurers or d0 or d1:
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            month=None if (date_from or date_to) else month,
            date_from=date_from,
            date_to=date_to,
        )
        land_col = _land_date_col(outcomes)
        proj = outcomes[
            outcomes["outcome_stage"].isin(_PROJECT_STAGES)
            & outcomes[land_col].notna()
            & (outcomes["expected_amount"] > 0)
        ].copy()
        if proj.empty:
            return []
        if not (date_from or date_to) and months:
            proj["period"] = _month_from_forecast_date(proj[land_col])
            proj = proj[proj["period"].isin(months)]
        agg = (
            proj.groupby("facility_name", as_index=False)["expected_amount"]
            .sum()
            .rename(columns={"expected_amount": "amount"})
            .sort_values("amount", ascending=False)
            .head(25)
        )
        agg["amount"] = agg["amount"].round(2)
        return _records(agg)

    df = _read_csv(_prefer("projected_cash_monthly_by_facility"))
    if df.empty:
        return []
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    if months:
        df = df[df["period"].astype(str).isin(months)]
    if fac:
        if "" in fac or "(blank)" in fac:
            blank = df["facility_name"].astype(str).str.strip().eq("")
            named = df["facility_name"].isin([f for f in fac if f and f != "(blank)"])
            df = df[blank | named]
        else:
            df = df[df["facility_name"].isin(fac)]
    agg = (
        df.groupby("facility_name", as_index=False)["amount"]
        .sum()
        .sort_values("amount", ascending=False)
        .head(25)
    )
    return _records(agg)


@app.get("/api/projected/by-insurance")
def projected_by_insurance(
    month: str | None = None,
    ins: str | None = None,
    facility: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    months, insurers, fac = _split_multi(month), _split_multi(ins), _split_multi(facility)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if fac or d0 or d1:
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            month=None if (date_from or date_to) else month,
            date_from=date_from,
            date_to=date_to,
        )
        land_col = _land_date_col(outcomes)
        proj = outcomes[
            outcomes["outcome_stage"].isin(_PROJECT_STAGES)
            & outcomes[land_col].notna()
            & (outcomes["expected_amount"] > 0)
        ].copy()
        if proj.empty:
            return []
        if not (date_from or date_to) and months:
            proj["period"] = _month_from_forecast_date(proj[land_col])
            proj = proj[proj["period"].isin(months)]
        agg = (
            proj.groupby("ins_name", as_index=False)["expected_amount"]
            .sum()
            .rename(columns={"expected_amount": "amount"})
            .sort_values("amount", ascending=False)
            .head(25)
        )
        agg["amount"] = agg["amount"].round(2)
        return _records(agg)

    df = _read_csv(_prefer("projected_cash_monthly_by_insurance"))
    if df.empty:
        return []
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    if months:
        df = df[df["period"].astype(str).isin(months)]
    if insurers:
        df = df[df["ins_name"].isin(insurers)]
    agg = (
        df.groupby("ins_name", as_index=False)["amount"]
        .sum()
        .sort_values("amount", ascending=False)
        .head(25)
    )
    return _records(agg)


@app.get("/api/actual/daily")
def actual_daily(
    facility: str | None = None,
    ins: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Actual cash by check date from payments ledger CSVs."""
    return _records(
        _actual_from_ledger(
            facility=facility,
            ins=ins,
            date_from=date_from,
            date_to=date_to,
            month=month,
        )
    )


@app.get("/api/outcomes/summary")
def outcomes_summary(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    outcomes = _scoped_outcomes(
        facility=facility,
        ins=ins,
        stage=stage,
        month=month,
        date_from=date_from,
        date_to=date_to,
    )
    risk = _filter_risk(
        _cached_risk(_risk_cache_key()),
        facility=_split_multi(facility),
        ins=_split_multi(ins),
        risk_flags=[],
        date_from=date_from,
        date_to=date_to,
        months=_split_multi(month),
    )
    stages = (
        outcomes.groupby("outcome_stage", as_index=False)
        .agg(line_count=("outcome_stage", "size"), amount=("expected_amount", "sum"))
        .sort_values("line_count", ascending=False)
        if not outcomes.empty
        else pd.DataFrame()
    )
    by_flag = (
        risk.groupby("risk_flag", as_index=False)["exposure_amount"]
        .sum()
        .sort_values("exposure_amount", ascending=False)
        if not risk.empty
        else pd.DataFrame()
    )
    overdue = pd.DataFrame()
    if not outcomes.empty:
        ov = outcomes[outcomes["outcome_stage"] == "overdue"].copy()
        if not ov.empty:
            if "overdue_days" in ov.columns:
                ov["overdue_days"] = pd.to_numeric(ov["overdue_days"], errors="coerce")
                overdue = (
                    ov.groupby("ins_name", as_index=False)
                    .agg(
                        expected_payment=("expected_amount", "sum"),
                        avg_overdue_days=("overdue_days", "mean"),
                        line_count=("expected_amount", "count"),
                    )
                    .sort_values("expected_payment", ascending=False)
                )
                overdue["avg_overdue_days"] = overdue["avg_overdue_days"].round(1)
            else:
                overdue = (
                    ov.groupby("ins_name", as_index=False)
                    .agg(
                        expected_payment=("expected_amount", "sum"),
                        line_count=("expected_amount", "count"),
                    )
                    .sort_values("expected_payment", ascending=False)
                )
            overdue["expected_payment"] = overdue["expected_payment"].round(2)
    return {
        "stages": _records(stages),
        "risk_by_flag": _records(by_flag),
        "overdue_by_insurance": _records(overdue.head(15)),
        "sla": _records(_read_csv(_forecast_dir() / "payer_sla.csv").head(25)),
    }


@app.get("/api/insights")
def insights(
    facility: str | None = None,
    ins: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    audit = load_audit_bundle(_audit_dir())
    cpt, icd = filter_audit(
        audit["cpt_violations"],
        audit["icd_violations"],
        facilities=_split_multi(facility) or None,
        insurers=_split_multi(ins) or None,
        severities=_split_multi(severity) or None,
    )
    cards = build_insight_cards(audit["summary"], cpt, icd, audit["unmapped_insurance"])
    risk = risk_audit_exposure(
        _filter_risk(
            _cached_risk(_risk_cache_key()),
            facility=_split_multi(facility),
            ins=_split_multi(ins),
            risk_flags=[],
        )
    )
    audit_exposure = float(risk["exposure_amount"].sum()) if not risk.empty else 0.0
    audit_visits = (
        int(risk["webpt_patient_id"].nunique())
        if not risk.empty and "webpt_patient_id" in risk.columns
        else 0
    )
    return {
        "cards": cards,
        "top_cpt_rules": _records(top_cpt_rules(cpt, 10)),
        "icd_categories": _records(icd_category_breakdown(icd).head(10)),
        "facility_severity": _records(facility_severity_matrix(cpt)),
        "icd_guidance": _records(icd_guidance_samples(icd)),
        "unmapped": _records(unmapped_ranked(audit["unmapped_insurance"])),
        "summary": _records(audit["summary"]),
        "audit_risk_exposure": round(audit_exposure, 2),
        "audit_risk_visits": audit_visits,
    }


@app.get("/api/drill/outcomes")
def drill_outcomes(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = Query(400, ge=1, le=2000),
) -> list[dict[str, Any]]:
    df = _scoped_outcomes(
        facility=facility,
        ins=ins,
        stage=stage,
        month=month,
        date_from=date_from,
        date_to=date_to,
    )
    if q and not df.empty:
        mask = pd.Series(False, index=df.index)
        for col in ("patient_name", "ins_name", "facility_name", "webpt_patient_id", "cpt_code"):
            if col in df.columns:
                mask |= df[col].astype(str).str.contains(q, case=False, na=False)
        df = df.loc[mask]
    return _records(df, limit)


@app.get("/api/drill/risk")
def drill_risk(
    facility: str | None = None,
    ins: str | None = None,
    risk_flag: str | None = None,
    q: str | None = None,
    limit: int = Query(400, ge=1, le=2000),
) -> list[dict[str, Any]]:
    df = _filter_risk(
        _cached_risk(_risk_cache_key()),
        facility=_split_multi(facility),
        ins=_split_multi(ins),
        risk_flags=_split_multi(risk_flag),
    )
    if q and not df.empty:
        mask = pd.Series(False, index=df.index)
        for col in ("patient_name", "ins_name", "facility_name", "risk_flag"):
            if col in df.columns:
                mask |= df[col].astype(str).str.contains(q, case=False, na=False)
        df = df.loc[mask]
    return _records(df, limit)


@app.get("/api/drill/audit-cpt")
def drill_audit_cpt(
    facility: str | None = None,
    ins: str | None = None,
    severity: str | None = None,
    q: str | None = None,
    limit: int = Query(400, ge=1, le=2000),
) -> list[dict[str, Any]]:
    audit = load_audit_bundle(_audit_dir())
    cpt, _ = filter_audit(
        audit["cpt_violations"],
        audit["icd_violations"],
        facilities=_split_multi(facility) or None,
        insurers=_split_multi(ins) or None,
        severities=_split_multi(severity) or None,
    )
    if q and not cpt.empty:
        mask = pd.Series(False, index=cpt.index)
        for col in ("patient_name", "insurance_name", "facility_name", "rule_id", "cpt_codes"):
            if col in cpt.columns:
                mask |= cpt[col].astype(str).str.contains(q, case=False, na=False)
        cpt = cpt.loc[mask]
    return _records(cpt, limit)


@app.get("/api/drill/audit-icd")
def drill_audit_icd(
    facility: str | None = None,
    ins: str | None = None,
    severity: str | None = None,
    q: str | None = None,
    limit: int = Query(400, ge=1, le=2000),
) -> list[dict[str, Any]]:
    audit = load_audit_bundle(_audit_dir())
    _, icd = filter_audit(
        audit["cpt_violations"],
        audit["icd_violations"],
        facilities=_split_multi(facility) or None,
        insurers=_split_multi(ins) or None,
        severities=_split_multi(severity) or None,
    )
    if q and not icd.empty:
        mask = pd.Series(False, index=icd.index)
        for col in ("patient_name", "insurance_name", "facility_name", "rule_id", "category"):
            if col in icd.columns:
                mask |= icd[col].astype(str).str.contains(q, case=False, na=False)
        icd = icd.loc[mask]
    return _records(icd, limit)


def _alias_api_routes_to_v1() -> None:
    """Expose every /api/* forecast route also under /api/v1/* (idempotent)."""
    from fastapi.routing import APIRoute

    existing = {r.path for r in app.routes if isinstance(r, APIRoute)}
    extras: list[APIRoute] = []
    for route in list(app.routes):
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if not path.startswith("/api/"):
            continue
        if path.startswith("/api/v1"):
            continue
        # /api/foo -> /api/v1/foo
        v1 = "/api/v1" + path[len("/api") :]
        if v1 in existing:
            continue
        extras.append(
            APIRoute(
                path=v1,
                endpoint=route.endpoint,
                methods=route.methods,
                name=f"{route.name}_v1" if route.name else None,
                response_model=route.response_model,
                tags=route.tags,
            )
        )
        existing.add(v1)
    for r in extras:
        app.routes.append(r)


_alias_api_routes_to_v1()


def main() -> None:
    import uvicorn

    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8787"))
    reload = os.environ.get("API_RELOAD", "0").strip().lower() in ("1", "true", "yes")
    uvicorn.run("cashflow_forecast.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
