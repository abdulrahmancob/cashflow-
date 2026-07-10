"""Streamlit dashboard — Outcome KPIs separate from Risk Exposure."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FORECAST = REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/forecast"

DARK_CSS = """
<style>
    .stApp { background-color: #0e1621; color: #e8eef5; }
    h1, h2, h3 { color: #ffffff !important; }
    [data-testid="stMetricValue"] { color: #ffffff; }
    [data-testid="stSidebar"] { background-color: #15202b; }
    .risk-panel {
        border: 1px solid #c0392b;
        border-radius: 8px;
        padding: 1rem;
        background: #1a1214;
        margin-bottom: 1rem;
    }
    .outcome-panel {
        border: 1px solid #1a6f9a;
        border-radius: 8px;
        padding: 1rem;
        background: #121820;
        margin-bottom: 1rem;
    }
</style>
"""


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _money(v: float) -> str:
    return f"${v:,.0f}"


def main() -> None:
    st.set_page_config(page_title="Cash Conversion Cycle Forecasting", layout="wide")
    st.markdown(DARK_CSS, unsafe_allow_html=True)

    st.sidebar.title("Filters")
    forecast_dir = Path(
        st.sidebar.text_input("Forecast dir", str(DEFAULT_FORECAST))
    )

    kpi_path = forecast_dir / "kpi_summary.json"
    if not kpi_path.exists():
        st.error(f"No kpi_summary.json in {forecast_dir}. Run: python -m cashflow_forecast build")
        return

    kpi = json.loads(kpi_path.read_text(encoding="utf-8"))
    outcomes = _load_csv(forecast_dir / "outcome_stages.csv")
    risk = _load_csv(forecast_dir / "risk_flags.csv")
    actual_daily = _load_csv(forecast_dir / "actual_cash_daily.csv")
    projected_daily = _load_csv(forecast_dir / "projected_cash_daily.csv")
    projected_monthly = _load_csv(forecast_dir / "projected_cash_monthly.csv")
    stage_counts = _load_csv(forecast_dir / "outcome_stage_counts.csv")
    overdue_ins = _load_csv(forecast_dir / "overdue_by_insurance.csv")
    denied_ins = _load_csv(forecast_dir / "denied_by_insurance.csv")
    risk_ins = _load_csv(forecast_dir / "risk_by_insurance.csv")
    sla = _load_csv(forecast_dir / "payer_sla.csv")

    # Sidebar filters
    facilities = sorted(outcomes["facility_name"].dropna().unique()) if not outcomes.empty else []
    insurers = sorted(outcomes["ins_name"].dropna().unique()) if not outcomes.empty else []
    stages = sorted(outcomes["outcome_stage"].dropna().unique()) if not outcomes.empty else []
    risk_flags = sorted(risk["risk_flag"].dropna().unique()) if not risk.empty else []

    sel_fac = st.sidebar.multiselect("Clinic", facilities, default=[])
    sel_ins = st.sidebar.multiselect("Insurance", insurers, default=[])
    sel_stage = st.sidebar.multiselect("Outcome stage", stages, default=stages)
    sel_risk = st.sidebar.multiselect("Risk flags", risk_flags, default=risk_flags)

    filtered = outcomes.copy()
    if sel_fac:
        filtered = filtered[filtered["facility_name"].isin(sel_fac)]
    if sel_ins:
        filtered = filtered[filtered["ins_name"].isin(sel_ins)]
    if sel_stage:
        filtered = filtered[filtered["outcome_stage"].isin(sel_stage)]

    risk_f = risk.copy()
    if sel_fac and not risk_f.empty and "facility_name" in risk_f.columns:
        risk_f = risk_f[risk_f["facility_name"].isin(sel_fac)]
    if sel_ins and not risk_f.empty:
        risk_f = risk_f[risk_f["ins_name"].isin(sel_ins)]
    if sel_risk and not risk_f.empty:
        risk_f = risk_f[risk_f["risk_flag"].isin(sel_risk)]

    st.title("CASH CONVERSION CYCLE FORECASTING")
    st.caption(f"Last update / as-of: {kpi.get('as_of', '')}  |  Pilot jun_jul_2026")

    # --- Outcome KPIs ---
    st.markdown('<div class="outcome-panel">', unsafe_allow_html=True)
    st.subheader("Outcome — Cash Flow Summary")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Actual Cash Received", _money(kpi["actual_cash_received"]))
    c2.metric("Projected Cash-In", _money(kpi["projected_cash_in"]))
    c3.metric("On Track", _money(kpi["on_track_amount"]), f"{kpi['on_track_count']} lines")
    c4.metric("Overdue", _money(kpi["overdue_amount"]), f"{kpi['overdue_count']} lines")
    c5.metric("Cash Denied", _money(kpi["denied_amount"]), f"{kpi['denied_count']} lines")
    st.markdown("</div>", unsafe_allow_html=True)

    # --- Risk Exposure (SEPARATE) ---
    st.markdown('<div class="risk-panel">', unsafe_allow_html=True)
    st.subheader("Risk Exposure — Predictive (not an outcome stage)")
    r1, r2, r3 = st.columns(3)
    r1.metric("Risk Exposure $", _money(kpi["risk_exposure_amount"]))
    r2.metric("Visits with risk flags", f"{kpi['risk_visit_count']:,}")
    if not risk_f.empty:
        by_flag = risk_f.groupby("risk_flag")["exposure_amount"].sum()
        r3.write(by_flag)
    st.markdown("</div>", unsafe_allow_html=True)

    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Projected Cash-In Trend (by expected payment month)")
        if not projected_monthly.empty:
            chart = projected_monthly.set_index("period")["amount"]
            st.bar_chart(chart)
        else:
            st.info("No projected monthly data")

        st.subheader("Actual Cash Daily (Check Date)")
        if not actual_daily.empty:
            st.line_chart(actual_daily.set_index("period")["amount"])
        else:
            st.info("No actual daily data")

    with col_b:
        st.subheader("Outcome Stage Distribution")
        if not stage_counts.empty:
            st.bar_chart(stage_counts.set_index("outcome_stage")["line_count"])
        else:
            st.info("No stage counts")

        st.subheader("Payer SLA (median days DOS→cash)")
        if not sla.empty:
            st.dataframe(
                sla[["webpt_insurance", "sample_count", "median_lag_days", "confidence"]].head(20),
                use_container_width=True,
            )

    t1, t2, t3 = st.columns(3)
    with t1:
        st.subheader("Overdue by Insurance")
        st.dataframe(overdue_ins.head(25), use_container_width=True)
    with t2:
        st.subheader("Denied / Rejected by Insurance")
        st.dataframe(denied_ins.head(25), use_container_width=True)
    with t3:
        st.subheader("Risk by Insurance")
        st.dataframe(risk_ins.head(25), use_container_width=True)

    with st.expander("Drill-down: filtered outcome lines"):
        st.dataframe(filtered.head(500), use_container_width=True)

    with st.expander("Drill-down: risk flags"):
        st.dataframe(risk_f.head(500), use_container_width=True)


if __name__ == "__main__":
    main()
