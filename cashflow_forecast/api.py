"""FastAPI backend for the React forecast dashboard.

Run from repo root:
  python -m cashflow_forecast.api
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
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
from cashflow_forecast.aggregations import (  # noqa: E402
    outcome_stage_counts,
    overdue_by_insurance,
    risk_totals_by_insurance,
)
from cashflow_forecast.tracker_posting import (  # noqa: E402
    horizon_kind,
    last_settled_bank_date,
)

try:
    from cashflow_forecast.forecast_engine import exclude_unscheduled_projection  # noqa: E402
except ImportError:  # older server engine without this helper
    def exclude_unscheduled_projection(df: pd.DataFrame) -> pd.DataFrame:
        return df

try:
    from cashflow_ops.security import cors_allow_origins as _cors_allow_origins
except Exception:  # noqa: BLE001 — forecast API still works without ops
    def _cors_allow_origins(raw: str | None = None) -> list[str]:
        value = os.environ.get("CASHFLOW_CORS_ORIGINS", "") if raw is None else raw
        return [part.strip() for part in value.split(",") if part.strip()]

DEFAULT_FORECAST = _REPO / "webpt_edco_scraper/output/jun_jul_2026/forecast"
DEFAULT_AUDIT = _REPO / "webpt_edco_scraper/output/jun_jul_2026/audit"

app = FastAPI(title="RCM Platform API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Platform ops + auth + eligibility under /api/v1 and /api (alias)
try:
    from cashflow_ops.activity_api import router as _activity_router
    from cashflow_ops.admin_db_api import router as _admin_db_router
    from cashflow_ops.api import router as _ops_router
    from cashflow_ops.auth_api import router as _auth_router
    from cashflow_ops.cpt_audit_api import router as _cpt_audit_router
    from cashflow_ops.cpt_guide_api import router as _cpt_guide_router
    from cashflow_ops.eligibility_api import router as _eligibility_router
    from cashflow_ops.security import seed_portal_users
    from cashflow_ops.tracker_api import router as _tracker_router
    from cashflow_ops.checks_deposits_api import router as _checks_deposits_router
    from cashflow_ops.billing_analysis_api import router as _billing_analysis_router
    from cashflow_ops.collection_api import router as _collection_router
    from cashflow_ops.away_api import router as _away_router
    from cashflow_ops.work_analytics_api import router as _analytics_router

    app.include_router(_ops_router, prefix="/api/v1")
    app.include_router(_ops_router, prefix="/api")
    app.include_router(_auth_router, prefix="/api/v1")
    app.include_router(_auth_router, prefix="/api")
    app.include_router(_eligibility_router, prefix="/api/v1")
    app.include_router(_eligibility_router, prefix="/api")
    app.include_router(_tracker_router, prefix="/api/v1")
    app.include_router(_tracker_router, prefix="/api")
    app.include_router(_checks_deposits_router, prefix="/api/v1")
    app.include_router(_checks_deposits_router, prefix="/api")
    app.include_router(_cpt_guide_router, prefix="/api/v1")
    app.include_router(_cpt_guide_router, prefix="/api")
    app.include_router(_cpt_audit_router, prefix="/api/v1")
    app.include_router(_cpt_audit_router, prefix="/api")
    app.include_router(_admin_db_router, prefix="/api/v1")
    app.include_router(_admin_db_router, prefix="/api")
    app.include_router(_analytics_router, prefix="/api/v1")
    app.include_router(_analytics_router, prefix="/api")
    app.include_router(_away_router, prefix="/api/v1")
    app.include_router(_away_router, prefix="/api")
    app.include_router(_billing_analysis_router, prefix="/api/v1")
    app.include_router(_billing_analysis_router, prefix="/api")
    app.include_router(_activity_router, prefix="/api/v1")
    app.include_router(_activity_router, prefix="/api")
    app.include_router(_collection_router, prefix="/api/v1")
    app.include_router(_collection_router, prefix="/api")

    @app.on_event("startup")
    def _portal_startup() -> None:
        try:
            seed_portal_users()
        except Exception:  # noqa: BLE001
            pass

except Exception:  # noqa: BLE001 — forecast API still works without ops
    pass


@app.on_event("startup")
def _forecast_warmup() -> None:
    """Preload hot Mission Control frames so the first user is not cold.

    Do not load forecast_prediction (~1M rows) here — the API tmpfs is 256M.
    KPI / monthly / daily / facility features are enough for the empty-filter view.
    """
    if not _use_db():
        return
    try:
        _ = _kpi_summary_base()
        _ = _projected_monthly_frame()
        _ = _read_feature("projected_cash_daily", "projected_cash_daily")
        _ = _read_feature(
            "projected_cash_monthly_by_facility", "projected_cash_monthly_by_facility"
        )
        _ = _read_feature("outcome_stage_counts", "outcome_stage_counts")
        _ = _read_feature("overdue_by_insurance", "overdue_by_insurance")
        _ = _read_feature("risk_totals_by_insurance", "risk_totals_by_insurance")
        _ = _tracker_actual_daily()
        _ = _meta_filters_payload(_outcomes_cache_key())
    except Exception:  # noqa: BLE001
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
        "/api/auth/logout",
        "/api/v1/auth/logout",
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
        "/api/behavior",
        "/api/mission",
        "/api/overdue",
        "/api/unbanked",
        "/api/v1/kpi",
        "/api/v1/projected",
        "/api/v1/actual",
        "/api/v1/outcomes",
        "/api/v1/insights",
        "/api/v1/drill",
        "/api/v1/meta",
        "/api/v1/behavior",
        "/api/v1/mission",
        "/api/v1/overdue",
        "/api/v1/unbanked",
    )
    if not any(path.startswith(p) for p in forecast_prefixes):
        return await call_next(request)
    try:
        from cashflow_ops.security import auth_user_from_token, extract_access_token
        from fastapi.responses import JSONResponse

        token = extract_access_token(request)
        if not token:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        user = auth_user_from_token(token)
        if not user.is_finance:
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


_STAMP_TTL_SEC = 20.0
_stamp_cache: tuple[float, str] | None = None
_TRACKER_TTL_SEC = 30.0
_tracker_daily_cache: tuple[float, pd.DataFrame] | None = None
_tracker_daily_lock = threading.Lock()


def _feature_cache_stamp() -> str:
    if _use_db():
        return _latest_forecast_run_stamp()
    d = _forecast_dir()
    return f"csv|{d}|{_file_mtime_key(d)}"


@lru_cache(maxsize=64)
def _cached_feature_df(stamp: str, kind: str, csv_base: str) -> pd.DataFrame:
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


def _read_feature(kind: str, csv_base: str | None = None) -> pd.DataFrame:
    # Copy: callers often mutate columns (amount coercion, filters).
    return _cached_feature_df(_feature_cache_stamp(), kind, csv_base or "").copy()


def _json_cell(value: Any) -> Any:
    """Make a cell JSON-safe (fix broken UTF-8 / NaN that crash to_json)."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and (value != value):  # NaN
        return None
    if isinstance(value, str):
        # Replace lone surrogates / invalid sequences pandas may leave in object cols
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, (dict, list)):
        try:
            return json.loads(
                json.dumps(value, default=str, ensure_ascii=False).encode(
                    "utf-8", "replace"
                ).decode("utf-8")
            )
        except Exception:
            return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df if limit is None else df.head(limit)
    # Nested payload is already flattened into columns for Mission Control / drill
    if "payload" in out.columns:
        out = out.drop(columns=["payload"])
    rows = out.to_dict(orient="records")
    return [{k: _json_cell(v) for k, v in row.items()} for row in rows]


def _file_mtime_key(path: Path) -> str:
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return "0"


def _latest_forecast_run_stamp() -> str:
    """Cache-bust when a new successful forecast_run lands in DB.

    Stamp is TTL-cached so every request does not hit Postgres before lru_cache.
    """
    global _stamp_cache
    now = time.monotonic()
    if _stamp_cache is not None and (now - _stamp_cache[0]) < _STAMP_TTL_SEC:
        return _stamp_cache[1]
    stamp = "none"
    try:
        from cashflow_db.repository import connection

        with connection() as conn:
            row = conn.execute(
                """
                SELECT forecast_run_id::text AS id, created_at
                FROM analytics.forecast_run
                WHERE status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row:
            stamp = f"{row['id']}|{row['created_at']}"
    except Exception:
        pass
    _stamp_cache = (now, stamp)
    return stamp


def _outcomes_cache_key() -> str:
    """Invalidate when outcome_stages rebuild or DB mode toggles."""
    if _use_db():
        return f"db|{_latest_forecast_run_stamp()}"
    d = _forecast_dir()
    return f"{d}|{_file_mtime_key(d / 'outcome_stages.csv')}"


def _risk_cache_key() -> str:
    if _use_db():
        return f"db-risk|{_latest_forecast_run_stamp()}"
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


_DEFAULT_STAGES = ["denied", "on_track", "overdue", "paid", "rejected", "zero_pay"]


def _month_end_date(d: date) -> date:
    """Last calendar day of ``d``'s month."""
    bounds = _month_bounds(d.strftime("%Y-%m"))
    return bounds[1] if bounds else d


def _calendar_months(today: date | None = None) -> list[str]:
    """January of ``today``'s year through ``today``'s month."""
    today = today or date.today()
    return [f"{today.year}-{m:02d}" for m in range(1, today.month + 1)]


# Import-time snapshot for older callers; payload rebuilds from date.today().
_CALENDAR_MONTHS = _calendar_months()
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _unique_nonempty(df: pd.DataFrame, col: str) -> list[str]:
    if df.empty or col not in df.columns:
        return []
    vals = df[col].dropna().astype(str).str.strip()
    return sorted(v for v in vals.unique().tolist() if v and v.lower() not in {"nan", "none"})


def _clean_filter_names(values: list[str], *, allow_numeric: bool = False) -> list[str]:
    """Keep human labels; drop IDs, UUIDs, and feature_key leftovers."""
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text or text.lower() in {"nan", "none"}:
            continue
        if "|" in text or _UUID_RE.match(text):
            continue
        if not allow_numeric and text.isdigit():
            continue
        out.append(text)
    return sorted(set(out))


def _period_overlaps_bounds(period: str, d0: date | None, d1: date | None) -> bool:
    text = str(period or "").strip()
    if not text:
        return False
    bounds = _month_bounds(text[:7] if len(text) >= 7 and text[4] == "-" else text)
    if bounds:
        start, end = bounds
        if d0 is not None and end < d0:
            return False
        if d1 is not None and start > d1:
            return False
        return True
    day = _parse_iso_date(text)
    if day is None:
        return False
    if d0 is not None and day < d0:
        return False
    if d1 is not None and day > d1:
        return False
    return True


def _filter_monthly_frame(
    df: pd.DataFrame,
    *,
    months: list[str] | None = None,
    d0: date | None = None,
    d1: date | None = None,
) -> pd.DataFrame:
    if df.empty or "period" not in df.columns:
        return df
    out = df.copy()
    if months:
        return out[out["period"].astype(str).isin(months)]
    if d0 or d1:
        return out[out["period"].astype(str).map(lambda p: _period_overlaps_bounds(p, d0, d1))]
    return out


def _projected_monthly_frame() -> pd.DataFrame:
    """Monthly projected cash from the feature table, or rolled up from daily."""
    df = _read_feature("projected_cash_monthly", "projected_cash_monthly")
    if df.empty:
        df = _read_csv(_prefer("projected_cash_monthly"))
    if not df.empty and "period" in df.columns:
        out = df.copy()
        out["amount"] = pd.to_numeric(out.get("amount"), errors="coerce").fillna(0)
        return out.sort_values("period").reset_index(drop=True)
    daily = _read_feature("projected_cash_daily", "projected_cash_daily")
    if daily.empty:
        daily = _read_csv(_prefer("projected_cash_daily"))
    if daily.empty or "period" not in daily.columns:
        return pd.DataFrame()
    daily = daily.copy()
    daily["amount"] = pd.to_numeric(daily.get("amount"), errors="coerce").fillna(0)
    dt = pd.to_datetime(daily["period"], errors="coerce")
    daily = daily.loc[dt.notna()].copy()
    daily["period"] = dt.dt.strftime("%Y-%m")
    named: dict[str, tuple[str, str]] = {"amount": ("amount", "sum")}
    if "line_count" in daily.columns:
        daily["line_count"] = pd.to_numeric(daily["line_count"], errors="coerce").fillna(0)
        named["line_count"] = ("line_count", "sum")
    g = daily.groupby("period", as_index=False).agg(**named)
    g["amount"] = g["amount"].round(2)
    return g.sort_values("period").reset_index(drop=True)


def _projected_daily_frame() -> pd.DataFrame:
    df = _read_feature("projected_cash_daily", "projected_cash_daily")
    if df.empty:
        df = _read_csv(_prefer("projected_cash_daily"))
    if df.empty or "period" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["amount"] = pd.to_numeric(out.get("amount"), errors="coerce").fillna(0)
    return out


def _sum_feature_amount(
    kind: str,
    csv_base: str,
    *,
    name_col: str,
    names: list[str] | None,
    months: list[str] | None,
    d0: date | None,
    d1: date | None,
) -> float:
    df = _read_feature(kind, csv_base)
    if df.empty:
        df = _read_csv(_prefer(csv_base))
    if df.empty or "amount" not in df.columns:
        return 0.0
    df = df.copy()
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
    if names and name_col in df.columns:
        wanted = set(names)
        if "" in wanted or "(blank)" in wanted:
            blank = df[name_col].astype(str).str.strip().eq("")
            named = df[name_col].isin([n for n in names if n and n != "(blank)"])
            df = df[blank | named]
        else:
            df = df[df[name_col].isin(names)]
    return round(float(df["amount"].sum()), 2) if not df.empty else 0.0


def _prediction_filter_options() -> dict[str, list[str]]:
    """DISTINCT clinic/insurance names from predictions — no pandas scan."""
    if not _use_db():
        return {"facilities": [], "insurers": []}
    try:
        from cashflow_db.repository import connection, forecast as forecast_repo

        with connection() as conn:
            return forecast_repo.get_prediction_filter_options(conn)
    except Exception:
        return {"facilities": [], "insurers": []}


def _projected_and_monthly(
    *,
    fac: list[str],
    insurers: list[str],
    months: list[str],
    d0: date | None,
    d1: date | None,
) -> list[dict[str, Any]]:
    """Monthly projected cash with clinic AND insurance (SQL only in DB mode)."""
    if _use_db():
        try:
            from cashflow_db.repository import connection, forecast as forecast_repo

            with connection() as conn:
                return forecast_repo.sum_projected_monthly(
                    conn,
                    facilities=fac or None,
                    insurers=insurers or None,
                    d0=d0,
                    d1=d1,
                    months=months or None,
                )
        except Exception:
            return []
    outcomes = _filter_outcomes(
        _cached_outcomes(_outcomes_cache_key()),
        facility=fac,
        ins=insurers,
        stage=[],
    )
    df = _projected_from_outcomes(outcomes, months=months or None)
    df = _filter_monthly_frame(df, months=None, d0=d0, d1=d1)
    return _records(df)


def _queue_open_risk(
    *,
    fac: list[str],
    insurers: list[str],
    d0: date | None,
    d1: date | None,
) -> dict[str, Any] | None:
    """Live CPT / ICD / Demographics audit-queue risk. None if DB is off."""
    if not _use_db():
        return None
    try:
        from cashflow_db.repository import connection, cpt_audit

        with connection() as conn:
            return cpt_audit.open_risk_exposure(
                conn,
                d0=d0,
                d1=d1,
                facilities=fac or None,
                insurers=insurers or None,
            )
    except Exception:
        return None


def _empty_queue_risk() -> dict[str, Any]:
    return {
        "exposure_amount": 0.0,
        "visit_count": 0,
        "by_insurance": [],
        "by_flag": [],
    }


def _insurance_mix_from_conn(
    conn: Any,
    *,
    fac: list[str],
    insurers: list[str],
    d0: date | None,
    d1: date | None,
    overdue: list[dict[str, Any]],
    risk: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from cashflow_db.repository import eligibility
    from cashflow_db.repository import insurance as ins_repo

    landed = eligibility.sheet_paid_by_insurance(
        conn,
        d0=d0,
        d1=d1,
        facilities=fac or None,
        insurers=insurers or None,
    )
    return ins_repo.merge_insurance_mix(landed=landed, overdue=overdue, risk=risk)


def _insurance_mix_rows(
    *,
    fac: list[str],
    insurers: list[str],
    d0: date | None,
    d1: date | None,
    overdue: list[dict[str, Any]],
    risk: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _use_db():
        return []
    try:
        from cashflow_db.repository import connection

        with connection() as conn:
            return _insurance_mix_from_conn(
                conn, fac=fac, insurers=insurers, d0=d0, d1=d1, overdue=overdue, risk=risk
            )
    except Exception:
        return []


def _filtered_risk_totals(
    *,
    fac: list[str],
    insurers: list[str],
    date_from: str | None,
    date_to: str | None,
    months: list[str],
    queued: dict[str, Any] | None = None,
) -> tuple[float, int]:
    if queued is not None:
        return float(queued.get("exposure_amount") or 0), int(queued.get("visit_count") or 0)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if _use_db():
        queued = _queue_open_risk(fac=fac, insurers=insurers, d0=d0, d1=d1)
        if queued is not None:
            return float(queued.get("exposure_amount") or 0), int(queued.get("visit_count") or 0)
        return 0.0, 0
    risk = _filter_risk(
        _cached_risk(_risk_cache_key()),
        facility=fac,
        ins=insurers,
        risk_flags=[],
        date_from=date_from,
        date_to=date_to,
        months=months,
    )
    if risk.empty or "exposure_amount" not in risk.columns:
        return 0.0, 0
    amt = round(float(pd.to_numeric(risk["exposure_amount"], errors="coerce").fillna(0).sum()), 2)
    visits = (
        int(risk["webpt_patient_id"].nunique())
        if "webpt_patient_id" in risk.columns
        else int(len(risk))
    )
    return amt, visits


def _projected_filtered_amount(
    *,
    fac: list[str],
    insurers: list[str],
    months: list[str],
    d0: date | None,
    d1: date | None,
) -> float:
    """Projected cash for a filter from feature tables — never a prediction scan."""
    if fac and insurers:
        rows = _projected_and_monthly(
            fac=fac, insurers=insurers, months=months, d0=d0, d1=d1
        )
        return round(sum(float(r.get("amount") or 0) for r in rows), 2)
    if fac:
        return _sum_feature_amount(
            "projected_cash_monthly_by_facility",
            "projected_cash_monthly_by_facility",
            name_col="facility_name",
            names=fac,
            months=months,
            d0=d0,
            d1=d1,
        )
    if insurers:
        return _sum_feature_amount(
            "projected_cash_monthly_by_insurance",
            "projected_cash_monthly_by_insurance",
            name_col="ins_name",
            names=insurers,
            months=months,
            d0=d0,
            d1=d1,
        )
    daily = _projected_daily_frame()
    if daily.empty:
        return 0.0
    if d0 or d1:
        daily = _filter_period_column(daily, d0, d1)
    return _sum_actual_amount(daily)


def _closest_forecast_in_range(
    d0: date | None, d1: date | None
) -> tuple[float | None, str | None]:
    """Cash Trajectory stitch for a date window. None amount means fall back."""
    if not _use_db():
        return None, None
    try:
        from cashflow_db.repository import connection, forecast as forecast_repo

        with connection() as conn:
            payload = forecast_repo.list_projected_history(
                conn,
                d0=d0,
                d1=d1,
                settled=_settled_as_of(),
            )
    except Exception:
        return None, None
    daily = list(payload.get("daily") or [])
    if not daily:
        return None, None
    total = round(sum(float(r.get("amount") or 0) for r in daily), 2)
    as_of: str | None = None
    if d0 is not None and d1 is not None and d0 == d1:
        period = d0.isoformat()
        matches = [r for r in daily if str(r.get("period") or "") == period]
        pick = matches[0] if len(matches) == 1 else (daily[0] if len(daily) == 1 else None)
        if pick is not None:
            fa = pick.get("forecast_as_of")
            as_of = str(fa) if fa else None
    return total, as_of


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


def _settled_as_of() -> date:
    return last_settled_bank_date(date.today())


def _forecast_as_of() -> date:
    raw = (_kpi_summary_base() or {}).get("as_of")
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    return date.today()


def _tag_horizon(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    as_of = _forecast_as_of()
    for rec in records:
        period = rec.get("period")
        try:
            day = date.fromisoformat(str(period)[:10])
        except (TypeError, ValueError):
            continue
        rec["horizon_kind"] = horizon_kind(day, as_of)
    return records


def _clip_actual_bounds(
    d0: date | None, d1: date | None
) -> tuple[date | None, date | None]:
    """Cap actual windows at last settled bank date (pending tracker days are not $0)."""
    settled = _settled_as_of()
    cap = settled if d1 is None or d1 > settled else d1
    return d0, cap


def _ar_stages_in_range(
    d0: date | None,
    d1: date | None,
    fac: list[str],
    insurers: list[str],
) -> dict[str, Any]:
    """On-track (DOS) + overdue (pre-pack land). Dates optional. No prediction scan."""
    empty = {
        "on_track_amount": 0.0,
        "on_track_count": 0,
        "overdue_amount": 0.0,
        "overdue_count": 0,
    }
    if _use_db():
        try:
            from cashflow_db.repository import connection, forecast as forecast_repo

            with connection() as conn:
                return forecast_repo.sum_ar_stages_in_range(
                    conn,
                    d0=d0,
                    d1=d1,
                    facilities=fac or None,
                    insurers=insurers or None,
                )
        except Exception:
            return empty
    outcomes = _filter_outcomes(
        _cached_outcomes(_outcomes_cache_key()),
        facility=fac,
        ins=insurers,
        stage=[],
    )
    if outcomes.empty or "outcome_stage" not in outcomes.columns:
        return empty
    if d0 is not None or d1 is not None:
        outcomes = _filter_outcomes_by_dates(
            outcomes,
            date_from=d0.isoformat() if d0 else None,
            date_to=d1.isoformat() if d1 else None,
        )
    amt = pd.to_numeric(outcomes.get("expected_amount"), errors="coerce").fillna(0.0)
    out = dict(empty)
    for stage, key_amt, key_n in (
        ("on_track", "on_track_amount", "on_track_count"),
        ("overdue", "overdue_amount", "overdue_count"),
    ):
        mask = outcomes["outcome_stage"].astype(str).eq(stage)
        out[key_amt] = round(float(amt.loc[mask].sum()), 2)
        out[key_n] = int(mask.sum())
    return out


def _normalize_actual_df(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce tracker daily actuals to period, amount, line_count."""
    empty = pd.DataFrame(columns=["period", "amount", "line_count"])
    if df is None or df.empty:
        return empty
    work = df.copy()
    work["amount"] = pd.to_numeric(work.get("amount"), errors="coerce").fillna(0)
    if "line_count" in work.columns:
        work["line_count"] = (
            pd.to_numeric(work["line_count"], errors="coerce").fillna(0).astype(int)
        )
    else:
        work["line_count"] = 0
    if "period" not in work.columns:
        return empty
    if "facility_name" in work.columns or "ins_name" in work.columns:
        work = (
            work.groupby("period", as_index=False)
            .agg(amount=("amount", "sum"), line_count=("line_count", "sum"))
        )
    work["amount"] = work["amount"].round(2)
    return work[["period", "amount", "line_count"]].sort_values("period")


def _tracker_actual_daily() -> pd.DataFrame:
    """All-time daily actual cash from Transaction Tracker. Never RevFlow remits."""
    global _tracker_daily_cache
    empty = pd.DataFrame(columns=["period", "amount", "line_count"])
    now = time.monotonic()
    with _tracker_daily_lock:
        cached = _tracker_daily_cache
        if cached is not None and now - cached[0] < _TRACKER_TTL_SEC:
            return cached[1].copy()
    if _use_db():
        try:
            from cashflow_forecast.db_source import load_tracker_actuals_df

            df = _normalize_actual_df(load_tracker_actuals_df())
        except Exception:
            return empty
    else:
        df = _normalize_actual_df(_read_feature("actual_cash_daily", "actual_cash_daily"))
    with _tracker_daily_lock:
        _tracker_daily_cache = (time.monotonic(), df)
    return df.copy()


def _sum_actual_amount(df: pd.DataFrame) -> float:
    if df is None or df.empty or "amount" not in df.columns:
        return 0.0
    return round(float(df["amount"].sum()), 2)


def _sum_actual_line_count(df: pd.DataFrame) -> int:
    if df is None or df.empty or "line_count" not in df.columns:
        return 0
    return int(pd.to_numeric(df["line_count"], errors="coerce").fillna(0).sum())


def _actual_from_ledger(
    *,
    facility: str | None = None,
    ins: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    month: str | None = None,
) -> pd.DataFrame:
    """Daily actual cash from Transaction Tracker (txn_date × amount).

    Clinic/insurance arguments are ignored — tracker deposits are bank-level.
    Returns columns: period, amount, line_count (aggregated by day).
    """
    del facility, ins
    months = _split_multi(month)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    d0, d1 = _clip_actual_bounds(d0, d1)
    if d0 is not None and d1 is not None and d0 > d1:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    df = _filter_period_column(_tracker_actual_daily(), d0, d1)
    if df.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    return df.sort_values("period")


def _filter_outcomes_by_dates(
    df: pd.DataFrame,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    months: list[str] | None = None,
) -> pd.DataFrame:
    """Filter by cash dates: scheduled land day for AR stages, eob_date for paid.

    Non-paid stages use the packed forecast_date (when cash will actually land)
    so past-due AR shows on future capacity days, not on dates already past.
    """
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    if df.empty or (d0 is None and d1 is None):
        return df
    land_col = (
        "forecast_date"
        if "forecast_date" in df.columns
        else "original_forecast_date"
    )
    if land_col not in df.columns and "eob_date" not in df.columns:
        return df
    if land_col in df.columns:
        land_ok = _series_in_range(df[land_col], d0, d1)
        if land_col == "forecast_date" and "original_forecast_date" in df.columns:
            # Rows missing the packed date fall back to the pre-pack schedule.
            miss = pd.to_datetime(df[land_col], errors="coerce").isna()
            if miss.any():
                land_ok = land_ok | (
                    miss & _series_in_range(df["original_forecast_date"], d0, d1)
                )
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
    if "forecast_date" in outcomes.columns:
        return "forecast_date"
    if "original_forecast_date" in outcomes.columns:
        return "original_forecast_date"
    if "expected_pay_date" in outcomes.columns:
        return "expected_pay_date"
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
    if outcomes.empty or "outcome_stage" not in outcomes.columns:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    land_col = _land_date_col(outcomes)
    if land_col not in outcomes.columns:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    amt = pd.to_numeric(outcomes.get("expected_amount"), errors="coerce").fillna(0.0)
    proj = outcomes.loc[
        outcomes["outcome_stage"].isin(_PROJECT_STAGES)
        & outcomes[land_col].notna()
        & (amt > 0)
    ].copy()
    proj = exclude_unscheduled_projection(proj)
    if proj.empty:
        return pd.DataFrame(columns=["period", "amount", "line_count"])
    proj["expected_amount"] = pd.to_numeric(proj["expected_amount"], errors="coerce").fillna(0.0)
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


@lru_cache(maxsize=4)
def _monthly_from_outcomes_json(outcomes_key: str) -> str:
    rolled = _projected_from_outcomes(_cached_outcomes(outcomes_key))
    return json.dumps(_records(rolled))


@lru_cache(maxsize=4)
def _by_facility_unfiltered_json(outcomes_key: str) -> str:
    outcomes = _cached_outcomes(outcomes_key)
    land_col = _land_date_col(outcomes)
    if (
        outcomes.empty
        or land_col not in outcomes.columns
        or "facility_name" not in outcomes.columns
    ):
        return "[]"
    amt = pd.to_numeric(outcomes.get("expected_amount"), errors="coerce").fillna(0.0)
    proj = outcomes.loc[
        outcomes["outcome_stage"].isin(_PROJECT_STAGES)
        & outcomes[land_col].notna()
        & (amt > 0)
    ].copy()
    proj = exclude_unscheduled_projection(proj)
    if proj.empty:
        return "[]"
    proj["expected_amount"] = pd.to_numeric(
        proj["expected_amount"], errors="coerce"
    ).fillna(0.0)
    agg = (
        proj.groupby("facility_name", as_index=False)["expected_amount"]
        .sum()
        .rename(columns={"expected_amount": "amount"})
        .sort_values("amount", ascending=False)
        .head(25)
    )
    agg["amount"] = agg["amount"].round(2)
    return json.dumps(_records(agg))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "forecast": str(_forecast_dir()), "audit": str(_audit_dir())}


@lru_cache(maxsize=4)
def _meta_filters_payload(cache_key: str) -> dict[str, Any]:
    """Filter options from feature tables — never a full prediction scan or DISTINCT IDs."""
    monthly = _projected_monthly_frame()
    by_fac = _read_feature(
        "projected_cash_monthly_by_facility", "projected_cash_monthly_by_facility"
    )
    by_ins = _read_feature(
        "projected_cash_monthly_by_insurance", "projected_cash_monthly_by_insurance"
    )
    daily = _read_feature("projected_cash_daily", "projected_cash_daily")
    risk = _cached_risk(_risk_cache_key())

    facilities = _clean_filter_names(_unique_nonempty(by_fac, "facility_name"))
    insurers = _clean_filter_names(
        _unique_nonempty(by_ins, "ins_name"), allow_numeric=True
    )
    feat_months: list[str] = []
    if not monthly.empty and "period" in monthly.columns:
        feat_months = [
            str(p)[:7]
            for p in monthly["period"].astype(str).tolist()
            if str(p).strip()
        ]
    today = date.today()
    months = sorted(set(_calendar_months(today)) | set(feat_months))
    stages = list(_DEFAULT_STAGES)

    date_min = date(today.year, 1, 1)
    date_max = _month_end_date(today)
    if today > date_max:
        date_max = today
    try:
        ledger = _tracker_actual_daily()
        if not ledger.empty and "period" in ledger.columns:
            oldest = pd.to_datetime(ledger["period"], errors="coerce").dropna()
            if not oldest.empty:
                first = oldest.min().date()
                if first < date_min:
                    date_min = first
    except Exception:
        pass
    if not daily.empty and "period" in daily.columns:
        last = pd.to_datetime(daily["period"], errors="coerce").dropna()
        if not last.empty:
            last_d = last.max().date()
            if last_d > date_max:
                date_max = last_d

    if not facilities or not insurers:
        pred = _prediction_filter_options()
        if not facilities:
            facilities = _clean_filter_names(pred.get("facilities") or [])
        if not insurers:
            insurers = _clean_filter_names(
                pred.get("insurers") or [], allow_numeric=True
            )

    if not _use_db() and (not facilities or not insurers):
        outcomes = _cached_outcomes(cache_key)
        if not facilities and not outcomes.empty and "facility_name" in outcomes.columns:
            facilities = _clean_filter_names(_unique_nonempty(outcomes, "facility_name"))
        if not insurers and not outcomes.empty and "ins_name" in outcomes.columns:
            insurers = _clean_filter_names(
                _unique_nonempty(outcomes, "ins_name"), allow_numeric=True
            )
        if not outcomes.empty and "outcome_stage" in outcomes.columns:
            stages = sorted(outcomes["outcome_stage"].dropna().unique().tolist())

    return {
        "facilities": facilities,
        "insurers": insurers,
        "stages": stages,
        "risk_flags": _unique_nonempty(risk, "risk_flag")
        if not risk.empty and "risk_flag" in risk.columns
        else [],
        "months": months,
        "date_min": date_min.isoformat(),
        "date_max": date_max.isoformat(),
        "last_settled_date": last_settled_bank_date(today).isoformat(),
        "severities": ["error", "warning"],
    }


@app.get("/api/meta/filters")
def meta_filters() -> dict[str, Any]:
    return _meta_filters_payload(_outcomes_cache_key())


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


@lru_cache(maxsize=8)
def _kpi_summary_base_cached(stamp: str) -> str:
    """JSON blob of kpi_summary keyed by forecast stamp / file mtime.

    In DB mode the warehouse feature wins even if a stale kpi_summary.json
    is sitting in FORECAST_DIR (baked image / leftover CSV).
    """
    if _use_db():
        try:
            feat = _read_feature("kpi_summary", "kpi_summary")
            if not feat.empty:
                row = feat.iloc[0].to_dict()
                row.pop("feature_key", None)
                row.pop("forecast_run_id", None)
                return json.dumps(row, default=str)
        except Exception:
            pass
        return "{}"
    path = _forecast_dir() / "kpi_summary.json"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            pass
    return "{}"


def _kpi_summary_base() -> dict[str, Any]:
    """DB forecast_feature kpi_summary when CASHFLOW_FORECAST_FROM_DB; else JSON file."""
    if _use_db():
        stamp = _latest_forecast_run_stamp()
    else:
        stamp = f"file|{_file_mtime_key(_forecast_dir() / 'kpi_summary.json')}"
    try:
        data = json.loads(_kpi_summary_base_cached(stamp))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@app.get("/api/kpi")
def kpi(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    return _kpi_payload(
        facility=facility,
        ins=ins,
        stage=stage,
        month=month,
        date_from=date_from,
        date_to=date_to,
    )


def _kpi_payload(
    *,
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    queued: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _kpi_summary_base()
    fac, insurers, stages = _split_multi(facility), _split_multi(ins), _split_multi(stage)
    months = _split_multi(month)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    filtered = bool(fac or insurers or stages or months or d0 or d1)

    # Actual received is always live Transaction Tracker cash, not kpi_summary.json.
    ledger_all = _tracker_actual_daily()
    actual_global = _sum_actual_amount(ledger_all)
    actual_global_n = _sum_actual_line_count(ledger_all)

    # Unfiltered Mission Control: serve pre-aggregated kpi_summary (no full outcomes scan).
    if not filtered:
        projected = float(base.get("projected_cash_in") or 0)
        risk_amt, risk_visits = _filtered_risk_totals(
            fac=[],
            insurers=[],
            date_from=None,
            date_to=None,
            months=[],
            queued=queued,
        )
        return {
            **base,
            "actual_cash_received": actual_global,
            "actual_cash_received_filtered": actual_global,
            "actual_line_count": actual_global_n,
            "on_track_amount": float(base.get("on_track_amount") or 0),
            "on_track_count": int(base.get("on_track_count") or 0),
            "overdue_amount": float(base.get("overdue_amount") or 0),
            "overdue_count": int(base.get("overdue_count") or 0),
            "denied_amount": float(base.get("denied_amount") or 0),
            "denied_count": int(base.get("denied_count") or 0),
            "paid_count": int(base.get("paid_count") or 0),
            "projected_cash_in": projected,
            "projected_cash_may_aug": float(
                base.get("projected_cash_may_aug") or projected
            ),
            "variance_amount": round(actual_global - projected, 2),
            "risk_exposure_amount": risk_amt,
            "risk_visit_count": risk_visits,
            "filtered": False,
            "date_from": None,
            "date_to": None,
            "last_settled_date": _settled_as_of().isoformat(),
            "closest_forecast_as_of": None,
            "projected_source": None,
        }

    projected_source: str | None = "latest_run"
    closest_forecast_as_of: str | None = None
    actual_ready = True
    if months or d0 or d1:
        act_d0, act_d1 = _clip_actual_bounds(d0, d1)
        if act_d0 is not None and act_d1 is not None and act_d0 > act_d1:
            actual_filtered = 0.0
            actual_filtered_n = 0
            actual_ready = False
        else:
            ledger_win = _filter_period_column(ledger_all, act_d0, act_d1)
            actual_filtered = _sum_actual_amount(ledger_win)
            actual_filtered_n = _sum_actual_line_count(ledger_win)
        stitch_amt, stitch_as_of = _closest_forecast_in_range(d0, d1)
        if stitch_amt is None:
            projected = _projected_filtered_amount(
                fac=fac,
                insurers=insurers,
                months=months if not (d0 or d1) else [],
                d0=d0,
                d1=d1,
            )
        else:
            projected = stitch_amt
            closest_forecast_as_of = stitch_as_of
            projected_source = "closest_prior"
    else:
        actual_filtered = actual_global
        actual_filtered_n = actual_global_n
        projected = _projected_filtered_amount(
            fac=fac, insurers=insurers, months=months, d0=d0, d1=d1
        )
    variance = None if not actual_ready else round(actual_filtered - projected, 2)
    stages = _ar_stages_in_range(d0, d1, fac, insurers)
    risk_amt, risk_visits = _filtered_risk_totals(
        fac=fac,
        insurers=insurers,
        date_from=date_from,
        date_to=date_to,
        months=months,
        queued=queued,
    )
    return {
        **base,
        "actual_cash_received": actual_global,
        "actual_cash_received_filtered": actual_filtered,
        "actual_line_count": actual_filtered_n,
        "on_track_amount": float(stages.get("on_track_amount") or 0),
        "on_track_count": int(stages.get("on_track_count") or 0),
        "overdue_amount": float(stages.get("overdue_amount") or 0),
        "overdue_count": int(stages.get("overdue_count") or 0),
        "denied_amount": 0.0,
        "denied_count": 0,
        "paid_count": 0,
        "projected_cash_in": projected,
        "projected_cash_may_aug": projected,
        "variance_amount": variance,
        "risk_exposure_amount": risk_amt,
        "risk_visit_count": risk_visits,
        "filtered": True,
        "date_from": d0.isoformat() if d0 else None,
        "date_to": d1.isoformat() if d1 else None,
        "last_settled_date": _settled_as_of().isoformat(),
        "closest_forecast_as_of": closest_forecast_as_of,
        "projected_source": projected_source,
    }


@app.get("/api/projected/history")
def projected_history(
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Expected cash from forecast history (day-ahead stitch) plus current-run tail."""
    d0, d1 = _resolve_date_bounds(date_from, date_to, _split_multi(month))
    empty: dict[str, Any] = {"daily": [], "monthly": []}
    if not _use_db():
        return empty
    try:
        from cashflow_db.repository import connection, forecast as forecast_repo

        with connection() as conn:
            payload = forecast_repo.list_projected_history(
                conn,
                d0=d0,
                d1=d1,
                settled=_settled_as_of(),
            )
    except Exception:
        return empty
    daily = _tag_horizon(list(payload.get("daily") or []))
    return {"daily": daily, "monthly": list(payload.get("monthly") or [])}


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
    del stages

    if fac and insurers:
        return _projected_and_monthly(
            fac=fac, insurers=insurers, months=months, d0=d0, d1=d1
        )

    if fac:
        df = _read_feature(
            "projected_cash_monthly_by_facility", "projected_cash_monthly_by_facility"
        )
        if df.empty:
            df = _read_csv(_prefer("projected_cash_monthly_by_facility"))
        if df.empty:
            return []
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
        df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
        if "" in fac or "(blank)" in fac:
            blank = df["facility_name"].astype(str).str.strip().eq("")
            named = df["facility_name"].isin([f for f in fac if f and f != "(blank)"])
            df = df[blank | named]
        else:
            df = df[df["facility_name"].isin(fac)]
        g = (
            df.groupby("period", as_index=False)["amount"].sum()
            if "period" in df.columns
            else df
        )
        if "amount" in g.columns:
            g["amount"] = pd.to_numeric(g["amount"], errors="coerce").fillna(0).round(2)
        return _records(g.sort_values("period") if "period" in g.columns else g)

    if insurers:
        df = _read_feature(
            "projected_cash_monthly_by_insurance", "projected_cash_monthly_by_insurance"
        )
        if df.empty:
            df = _read_csv(_prefer("projected_cash_monthly_by_insurance"))
        if df.empty:
            return []
        df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
        df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
        df = df[df["ins_name"].isin(insurers)] if "ins_name" in df.columns else df
        g = (
            df.groupby("period", as_index=False)["amount"].sum()
            if "period" in df.columns
            else df
        )
        if "amount" in g.columns:
            g["amount"] = pd.to_numeric(g["amount"], errors="coerce").fillna(0).round(2)
        return _records(g.sort_values("period") if "period" in g.columns else g)

    df = _projected_monthly_frame()
    if df.empty:
        return []
    df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
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
    if fac or insurers or stages:
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
        proj = exclude_unscheduled_projection(proj)
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
        return _tag_horizon(_records(g))

    df = _read_feature("projected_cash_daily", "projected_cash_daily")
    if df.empty:
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
    records = _records(df)
    return _tag_horizon(records)


def _projected_by_name_sql(
    *,
    name_field: str,
    fac: list[str],
    insurers: list[str],
    months: list[str],
    d0: date | None,
    d1: date | None,
) -> list[dict[str, Any]]:
    if not _use_db():
        return []
    try:
        from cashflow_db.repository import connection, forecast as forecast_repo

        with connection() as conn:
            return forecast_repo.sum_projected_by_name(
                conn,
                name_field=name_field,
                facilities=fac or None,
                insurers=insurers or None,
                d0=d0,
                d1=d1,
                months=months or None,
            )
    except Exception:
        return []


def _feature_outcomes_summary(
    queued: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    stages = _read_feature("outcome_stage_counts", "outcome_stage_counts")
    overdue = _read_feature("overdue_by_insurance", "overdue_by_insurance")
    if stages.empty and overdue.empty:
        return None
    if not stages.empty and "amount" in stages.columns:
        stages["amount"] = pd.to_numeric(stages["amount"], errors="coerce").fillna(0).round(2)
        if "share_pct" not in stages.columns:
            from cashflow_forecast.aggregations import with_share

            stages = with_share(stages, "amount")
    if not overdue.empty and "expected_payment" in overdue.columns:
        overdue["expected_payment"] = pd.to_numeric(
            overdue["expected_payment"], errors="coerce"
        ).fillna(0).round(2)
        overdue = overdue.sort_values("expected_payment", ascending=False)
        if "share_pct" not in overdue.columns:
            from cashflow_forecast.aggregations import with_share

            overdue = with_share(overdue, "expected_payment")
    if queued is None:
        queued = _queue_open_risk(fac=[], insurers=[], d0=None, d1=None)
    if queued is None:
        queued = _empty_queue_risk()
    overdue_rows = _records(overdue)
    return {
        "stages": _records(stages),
        "risk_by_flag": list(queued.get("by_flag") or []),
        "overdue_by_insurance": overdue_rows,
        "risk_by_insurance": list(queued.get("by_insurance") or []),
        "insurance_mix": _insurance_mix_rows(
            fac=[],
            insurers=[],
            d0=None,
            d1=None,
            overdue=overdue_rows,
            risk=list(queued.get("by_insurance") or []),
        ),
    }


def _sql_outcomes_summary(
    *,
    fac: list[str],
    insurers: list[str],
    d0: date | None,
    d1: date | None,
    queued: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from cashflow_db.repository import connection, cpt_audit, forecast as forecast_repo

    with connection() as conn:
        stages = forecast_repo.summarize_outcome_stages(
            conn, d0=d0, d1=d1, facilities=fac or None, insurers=insurers or None
        )
        overdue = forecast_repo.summarize_overdue_by_insurance(
            conn, d0=d0, d1=d1, facilities=fac or None, insurers=insurers or None
        )
        if queued is None:
            queued = cpt_audit.open_risk_exposure(
                conn, d0=d0, d1=d1, facilities=fac or None, insurers=insurers or None
            )
        mix = _insurance_mix_from_conn(
            conn,
            fac=fac,
            insurers=insurers,
            d0=d0,
            d1=d1,
            overdue=overdue,
            risk=list(queued.get("by_insurance") or []),
        )
    return {
        "stages": stages,
        "risk_by_flag": list(queued.get("by_flag") or []),
        "overdue_by_insurance": overdue,
        "risk_by_insurance": list(queued.get("by_insurance") or []),
        "insurance_mix": mix,
    }


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
    if insurers:
        if _use_db():
            return _projected_by_name_sql(
                name_field="facility_name",
                fac=fac,
                insurers=insurers,
                months=months if not (d0 or d1) else [],
                d0=d0,
                d1=d1,
            )
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            month=month if not (date_from or date_to) else None,
            date_from=date_from,
            date_to=date_to,
        )
        if outcomes.empty or "facility_name" not in outcomes.columns:
            return []
        land_col = _land_date_col(outcomes)
        amt = pd.to_numeric(outcomes.get("expected_amount"), errors="coerce").fillna(0.0)
        proj = outcomes.loc[
            outcomes["outcome_stage"].isin(_PROJECT_STAGES) & (amt > 0)
        ].copy()
        proj = exclude_unscheduled_projection(proj)
        if land_col in proj.columns:
            proj = proj.loc[proj[land_col].notna()]
        if proj.empty:
            return []
        proj["expected_amount"] = pd.to_numeric(proj["expected_amount"], errors="coerce").fillna(0)
        if fac:
            proj = _filter_outcomes(proj, facility=fac, ins=[], stage=[])
        agg = (
            proj.groupby("facility_name", as_index=False)["expected_amount"]
            .sum()
            .rename(columns={"expected_amount": "amount"})
            .sort_values("amount", ascending=False)
            .head(25)
        )
        agg["amount"] = agg["amount"].round(2)
        return _records(agg)
    df = _read_feature(
        "projected_cash_monthly_by_facility", "projected_cash_monthly_by_facility"
    )
    if df.empty:
        df = _read_csv(_prefer("projected_cash_monthly_by_facility"))
    if df.empty:
        return []
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
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
    if fac:
        if _use_db():
            return _projected_by_name_sql(
                name_field="ins_name",
                fac=fac,
                insurers=insurers,
                months=months if not (d0 or d1) else [],
                d0=d0,
                d1=d1,
            )
        outcomes = _scoped_outcomes(
            facility=facility,
            ins=ins,
            month=month if not (date_from or date_to) else None,
            date_from=date_from,
            date_to=date_to,
        )
        if outcomes.empty or "ins_name" not in outcomes.columns:
            return []
        amt = pd.to_numeric(outcomes.get("expected_amount"), errors="coerce").fillna(0.0)
        proj = outcomes.loc[
            outcomes["outcome_stage"].isin(_PROJECT_STAGES) & (amt > 0)
        ].copy()
        proj = exclude_unscheduled_projection(proj)
        land_col = _land_date_col(proj) if not proj.empty else ""
        if land_col and land_col in proj.columns:
            proj = proj.loc[proj[land_col].notna()]
        if proj.empty:
            return []
        proj["expected_amount"] = pd.to_numeric(proj["expected_amount"], errors="coerce").fillna(0)
        agg = (
            proj.groupby("ins_name", as_index=False)["expected_amount"]
            .sum()
            .rename(columns={"expected_amount": "amount"})
            .sort_values("amount", ascending=False)
            .head(25)
        )
        agg["amount"] = agg["amount"].round(2)
        return _records(agg)
    df = _read_feature(
        "projected_cash_monthly_by_insurance", "projected_cash_monthly_by_insurance"
    )
    if df.empty:
        df = _read_csv(_prefer("projected_cash_monthly_by_insurance"))
    if df.empty:
        return []
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0)
    df = _filter_monthly_frame(df, months=months, d0=d0, d1=d1)
    if insurers and "ins_name" in df.columns:
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
    """Actual cash by Transaction Tracker txn_date (clinic/insurance ignored)."""
    return _records(
        _actual_from_ledger(
            facility=facility,
            ins=ins,
            date_from=date_from,
            date_to=date_to,
            month=month,
        )
    )


@app.get("/api/overdue/claims")
def overdue_claims(
    facility: str | None = None,
    ins: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
) -> list[dict[str, Any]]:
    """Forecast claims past Insurance-behavior expected land date."""
    if not _use_db():
        return []
    fac, insurers = _split_multi(facility), _split_multi(ins)
    d0, d1 = _resolve_date_bounds(date_from, date_to, _split_multi(month))
    try:
        from cashflow_db.repository import connection, forecast as forecast_repo

        with connection() as conn:
            return forecast_repo.list_overdue_claims(
                conn,
                d0=d0,
                d1=d1,
                facilities=fac or None,
                insurers=insurers or None,
                q=q,
                limit=limit,
            )
    except Exception:
        return []


@app.get("/api/outcomes/summary")
def outcomes_summary(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    return _outcomes_summary_payload(
        facility=facility,
        ins=ins,
        stage=stage,
        month=month,
        date_from=date_from,
        date_to=date_to,
    )


def _outcomes_summary_payload(
    *,
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    queued: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fac, insurers = _split_multi(facility), _split_multi(ins)
    months = _split_multi(month)
    d0, d1 = _resolve_date_bounds(date_from, date_to, months)
    filtered = bool(fac or insurers or months or d0 or d1 or _split_multi(stage))
    sla = _records(_read_csv(_forecast_dir() / "payer_sla.csv").head(25))
    empty = {
        "stages": [],
        "risk_by_flag": [],
        "overdue_by_insurance": [],
        "risk_by_insurance": [],
        "insurance_mix": [],
        "sla": sla,
    }
    if _use_db():
        if not filtered:
            feat = _feature_outcomes_summary(queued=queued)
            if feat is not None:
                feat["sla"] = sla
                return feat
        try:
            data = _sql_outcomes_summary(
                fac=fac, insurers=insurers, d0=d0, d1=d1, queued=queued
            )
            data["sla"] = sla
            return data
        except Exception:
            return empty

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
    stages = outcome_stage_counts(outcomes)
    if not stages.empty and "amount" in stages.columns:
        stages["amount"] = pd.to_numeric(stages["amount"], errors="coerce").fillna(0).round(2)
    by_flag = (
        risk.groupby("risk_flag", as_index=False)["exposure_amount"]
        .sum()
        .sort_values("exposure_amount", ascending=False)
        if not risk.empty and "risk_flag" in risk.columns
        else pd.DataFrame()
    )
    overdue = overdue_by_insurance(outcomes)
    risk_ins = risk_totals_by_insurance(risk)
    return {
        "stages": _records(stages),
        "risk_by_flag": _records(by_flag),
        "overdue_by_insurance": _records(overdue.head(40)),
        "risk_by_insurance": _records(risk_ins.head(40)),
        "insurance_mix": [],
        "sla": sla,
    }


@app.get("/api/mission")
def mission_dashboard(
    facility: str | None = None,
    ins: str | None = None,
    stage: str | None = None,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    grain: str = Query("day"),
) -> dict[str, Any]:
    """One round-trip for Mission Control. Never loads forecast_prediction into pandas."""
    del grain
    fac, insurers = _split_multi(facility), _split_multi(ins)
    d0, d1 = _resolve_date_bounds(date_from, date_to, _split_multi(month))

    def _load_behavior() -> dict[str, Any]:
        try:
            return behavior_trend(
                grain="day",
                ins=ins,
                date_from=date_from,
                date_to=date_to,
                month=month,
                include_checks=False,
            )
        except Exception:
            return {"grain": "day", "series": [], "insurers": [], "checks": []}

    def _load_risk() -> dict[str, Any] | None:
        if not _use_db():
            return None
        queued = _queue_open_risk(fac=fac, insurers=insurers, d0=d0, d1=d1)
        return queued if queued is not None else _empty_queue_risk()

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_behavior = pool.submit(_load_behavior)
        fut_risk = pool.submit(_load_risk)
        queued = fut_risk.result()
        fut_kpi = pool.submit(
            _kpi_payload,
            facility=facility,
            ins=ins,
            stage=stage,
            month=month,
            date_from=date_from,
            date_to=date_to,
            queued=queued,
        )
        fut_outcomes = pool.submit(
            _outcomes_summary_payload,
            facility=facility,
            ins=ins,
            stage=stage,
            month=month,
            date_from=date_from,
            date_to=date_to,
            queued=queued,
        )
        behavior = fut_behavior.result()
        kpi_data = fut_kpi.result()
        outcomes = fut_outcomes.result()
    return {
        "kpi": kpi_data,
        "monthly": [],
        "by_facility": [],
        "by_insurance": [],
        "outcomes": outcomes,
        "behavior": behavior,
        "day_ahead": _day_ahead_recent(),
    }


def _behavior_grain(grain: str | None) -> str:
    g = str(grain or "").lower()
    if g.startswith("year"):
        return "year"
    if g.startswith("day"):
        return "day"
    return "month"


@app.get("/api/behavior/trend")
def behavior_trend(
    grain: str = Query("month"),
    ins: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    month: str | None = None,
    include_checks: bool = True,
) -> dict[str, Any]:
    """Tracker paid $ by insurer (txn_date) plus individual check rows."""
    kind = _behavior_grain(grain)
    insurers = _split_multi(ins)
    d0, d1 = _resolve_date_bounds(date_from, date_to, _split_multi(month))
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    if _use_db():
        try:
            from cashflow_db.repository import connection, insurance as ins_repo

            with connection() as conn:
                rows = ins_repo.summarize_tracker_paid_trend(
                    conn,
                    grain=kind,
                    insurers=insurers or None,
                    d0=d0,
                    d1=d1,
                    limit_insurers=1 if len(insurers) == 1 else 2,
                )
                if include_checks:
                    ranked: list[str] = []
                    seen_ranked: set[str] = set()
                    for r in rows:
                        name = str(r.get("ins_name") or "").strip()
                        if name and name not in seen_ranked:
                            seen_ranked.add(name)
                            ranked.append(name)
                    checks = ins_repo.list_tracker_checks(
                        conn,
                        insurers=None if ranked else (insurers or None),
                        exact_names=ranked or None,
                        d0=d0,
                        d1=d1,
                    )
        except Exception:
            rows = []
            checks = []
    names = []
    seen: set[str] = set()
    for r in rows:
        name = str(r.get("ins_name") or "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return {"grain": kind, "series": rows, "insurers": names, "checks": checks}


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


_DRILL_OUTCOME_COLS = (
    "patient_name",
    "webpt_patient_id",
    "facility_name",
    "ins_name",
    "case_id",
    "cpt_code",
    "modifier",
    "date_of_service",
    "outcome_stage",
    "expected_amount",
    "forecast_date",
    "original_forecast_date",
    "expected_pay_date",
    "eob_date",
    "paid_amount",
    "overdue_days",
    "denied_amount",
    "denial_category",
)


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
    keep = [c for c in _DRILL_OUTCOME_COLS if c in df.columns]
    if keep:
        df = df[keep]
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


def _day_ahead_recent() -> dict[str, Any]:
    if not _use_db():
        return {"yesterday": None, "recent": []}
    try:
        from cashflow_db.repository import connection

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT bank_date, forecast_as_of, forecast_total, actual_total, error_pct, by_component
                FROM analytics.day_ahead_score
                ORDER BY bank_date DESC
                LIMIT 5
                """
            ).fetchall()
    except Exception:  # noqa: BLE001
        return {"yesterday": None, "recent": []}
    recent = [
        {
            "bank_date": str(r["bank_date"]),
            "forecast_as_of": str(r["forecast_as_of"]),
            "forecast_total": float(r["forecast_total"]),
            "actual_total": float(r["actual_total"]),
            "error_pct": None if r["error_pct"] is None else float(r["error_pct"]),
            "by_component": r["by_component"] or {},
        }
        for r in rows
    ]
    return {"yesterday": recent[0] if recent else None, "recent": recent}


@app.get("/api/day-ahead")
def day_ahead_card() -> dict[str, Any]:
    """Yesterday's frozen day-ahead score and the last five settled bank days."""
    return _day_ahead_recent()


def _compact_check_sql(expr: str) -> str:
    """Same check key as eligibility overlay: drop .0, punctuation, and leading zeros."""
    stripped = f"regexp_replace(coalesce(({expr})::text, ''), '\\.0+$', '')"
    alnum = f"regexp_replace(upper({stripped}), '[^A-Z0-9]', '', 'g')"
    return f"""
    CASE
      WHEN {alnum} ~ '^[0-9]+$'
      THEN COALESCE(NULLIF(ltrim({alnum}, '0'), ''), '0')
      ELSE {alnum}
    END
    """


def _real_check_sql(expr: str) -> str:
    """ZEROPAY notices and tokens with no digits are not checks."""
    return f"""
    {expr} IS NOT NULL
    AND btrim({expr}) <> ''
    AND upper(btrim({expr})) !~ '^ZEROPAY'
    AND {expr} ~ '[0-9]'
    """


_CHECKS_VS_TRACKER_SQL = f"""
WITH waystar_src AS (
    SELECT
        waystar_claim_id,
        payer_name,
        trans_date,
        COALESCE(total_remit_amount, 0) AS total_remit_amount,
        unnest(remit_numbers) AS ref
    FROM billing.waystar_claim
    WHERE COALESCE(array_length(remit_numbers, 1), 0) > 0
),
waystar_real AS (
    SELECT
        waystar_claim_id,
        payer_name,
        trans_date,
        total_remit_amount,
        btrim(ref) AS ref,
        {_compact_check_sql("ref")} AS compact
    FROM waystar_src
    WHERE {_real_check_sql("ref")}
),
waystar_claim_check AS (
    SELECT
        compact,
        waystar_claim_id,
        max(ref) AS ref,
        max(payer_name) AS payer_name,
        max(trans_date) AS trans_date,
        max(total_remit_amount) AS total_remit_amount
    FROM waystar_real
    WHERE compact <> ''
    GROUP BY compact, waystar_claim_id
),
waystar_keys AS (
    SELECT DISTINCT compact FROM waystar_claim_check
),
tracker_src AS (
    SELECT row_id, unnest(ARRAY[eft_1, eft_2, check_reference]) AS ref
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL
),
tracker_keys AS (
    SELECT DISTINCT {_compact_check_sql("ref")} AS compact
    FROM tracker_src
    WHERE {_real_check_sql("ref")}
),
deposit_keys AS (
    SELECT DISTINCT {_compact_check_sql("eft_1")} AS compact
    FROM billing.bank_deposit
    WHERE source_system = 'checks_deposits'
      AND {_real_check_sql("eft_1")}
),
found_keys AS (
    SELECT compact FROM tracker_keys WHERE compact <> ''
    UNION
    SELECT compact FROM deposit_keys WHERE compact <> ''
),
waystar_grouped AS (
    SELECT
        compact,
        COALESCE(NULLIF(btrim(payer_name), ''), 'Unknown') AS payer,
        (array_agg(ref ORDER BY length(ref) DESC, ref))[1] AS check_number,
        count(*)::int AS claim_count,
        sum(total_remit_amount) AS sum_amount,
        max(trans_date) AS latest_date
    FROM waystar_claim_check
    WHERE NOT EXISTS (
        SELECT 1 FROM found_keys k
        WHERE k.compact <> '' AND k.compact = waystar_claim_check.compact
    )
    GROUP BY compact, COALESCE(NULLIF(btrim(payer_name), ''), 'Unknown')
    HAVING sum(total_remit_amount) <> 0
),
revflow_amt AS (
    SELECT
        {_compact_check_sql("check_eft_num")} AS compact,
        sum(COALESCE(paid_amount_sum, 0)) AS amount
    FROM billing.eob_check
    WHERE {_real_check_sql("check_eft_num")}
      AND COALESCE(paid_amount_sum, 0) <> 0
    GROUP BY 1
),
waystar_missing AS (
    SELECT
        g.payer,
        g.check_number,
        g.claim_count,
        CASE
            WHEN r.amount IS NOT NULL AND g.payer_rows = 1 THEN r.amount
            ELSE g.sum_amount
        END AS amount,
        g.latest_date
    FROM (
        SELECT
            waystar_grouped.*,
            count(*) OVER (PARTITION BY compact) AS payer_rows
        FROM waystar_grouped
    ) g
    LEFT JOIN revflow_amt r
        ON r.compact <> '' AND r.compact = g.compact
),
tracker_rows AS (
    SELECT
        row_id,
        txn_date,
        COALESCE(amount, 0) AS amount,
        description,
        transaction_type,
        eft_1,
        eft_2,
        check_reference
    FROM billing.transaction_tracker_row
    WHERE deleted_at IS NULL
),
tracker_refs AS (
    SELECT
        t.row_id,
        btrim(v.ref) AS ref,
        {_compact_check_sql("v.ref")} AS compact
    FROM tracker_rows t
    CROSS JOIN LATERAL (
        VALUES (t.eft_1), (t.eft_2), (t.check_reference)
    ) AS v(ref)
    WHERE {_real_check_sql("v.ref")}
)
SELECT 'waystar' AS side,
       NULL::text AS row_id,
       check_number,
       payer,
       claim_count,
       amount,
       latest_date AS txn_date,
       NULL::text AS description,
       NULL::text AS transaction_type
FROM waystar_missing
UNION ALL
SELECT 'tracker' AS side,
       t.row_id::text,
       string_agg(DISTINCT r.ref, ', ' ORDER BY r.ref) AS check_number,
       NULL::text AS payer,
       NULL::int AS claim_count,
       t.amount,
       t.txn_date,
       t.description,
       t.transaction_type
FROM tracker_rows t
JOIN tracker_refs r ON r.row_id = t.row_id AND r.compact <> ''
WHERE t.amount <> 0
GROUP BY t.row_id, t.txn_date, t.amount, t.description, t.transaction_type
HAVING count(*) FILTER (
    WHERE r.compact IN (SELECT compact FROM waystar_keys WHERE compact <> '')
) = 0
"""


def _unbanked_cell(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            value = float(value)
        out[key] = _json_cell(value)
    return out


@app.get("/api/unbanked")
def unbanked_cash() -> dict[str, Any]:
    """Waystar checks missing from the tracker, and tracker checks missing from Waystar."""
    empty: dict[str, Any] = {"waystar_missing": [], "tracker_missing": []}
    if not _use_db():
        return empty
    from cashflow_db.repository import connection

    with connection() as conn:
        rows = conn.execute(_CHECKS_VS_TRACKER_SQL).fetchall()
    waystar: list[dict[str, Any]] = []
    tracker: list[dict[str, Any]] = []
    for raw in rows:
        row = _unbanked_cell(dict(raw))
        side = row.pop("side", None)
        if side == "waystar":
            waystar.append(
                {
                    "check_number": row.get("check_number") or "",
                    "payer": row.get("payer") or "",
                    "claim_count": int(row.get("claim_count") or 0),
                    "amount": float(row.get("amount") or 0),
                    "latest_date": row.get("txn_date"),
                }
            )
        else:
            tracker.append(
                {
                    "row_id": row.get("row_id") or "",
                    "txn_date": row.get("txn_date"),
                    "check_number": row.get("check_number") or "",
                    "description": row.get("description") or "",
                    "transaction_type": row.get("transaction_type") or "",
                    "amount": float(row.get("amount") or 0),
                }
            )
    waystar.sort(key=lambda item: (-item["amount"], item["check_number"]))
    tracker.sort(key=lambda item: (item["txn_date"] or "", item["amount"]), reverse=True)
    return {"waystar_missing": waystar, "tracker_missing": tracker}


@app.get("/api/unbanked.csv")
def unbanked_csv() -> Any:
    from fastapi.responses import PlainTextResponse

    payload = unbanked_cash()
    lines = ["side,check_number,payer,claim_count,amount,date,description,type"]
    for row in payload.get("waystar_missing") or []:
        lines.append(
            ",".join(
                str(value or "").replace(",", " ")
                for value in (
                    "waystar",
                    row.get("check_number"),
                    row.get("payer"),
                    row.get("claim_count"),
                    row.get("amount"),
                    row.get("latest_date"),
                    "",
                    "",
                )
            )
        )
    for row in payload.get("tracker_missing") or []:
        lines.append(
            ",".join(
                str(value or "").replace(",", " ")
                for value in (
                    "tracker",
                    row.get("check_number"),
                    "",
                    "",
                    row.get("amount"),
                    row.get("txn_date"),
                    row.get("description"),
                    row.get("transaction_type"),
                )
            )
        )
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=checks-vs-tracker.csv"},
    )


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
