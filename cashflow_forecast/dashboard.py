"""Futuristic Streamlit dashboard — Mission Control + Audit Business Insights.

Prefer the React app (see run_web.ps1). This Streamlit UI is a fallback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `streamlit run cashflow_forecast/dashboard.py` without pip install -e .
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from cashflow_forecast.dashboard_insights import (
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FORECAST = REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/forecast"

SPACE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Exo+2:wght@400;600&display=swap');

.stApp {
  background:
    radial-gradient(ellipse 120% 80% at 10% -10%, #0a3d4d55 0%, transparent 50%),
    radial-gradient(ellipse 80% 60% at 90% 10%, #06304a44 0%, transparent 45%),
    radial-gradient(circle at 20% 80%, #0c2a3840 0%, transparent 35%),
    #050a12;
  color: #d7e6f2;
  font-family: 'Exo 2', sans-serif;
}
.stApp::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background-image:
    radial-gradient(1px 1px at 8% 12%, #9fdfff88, transparent),
    radial-gradient(1px 1px at 22% 40%, #ffffff66, transparent),
    radial-gradient(1.5px 1.5px at 48% 18%, #7ec8e688, transparent),
    radial-gradient(1px 1px at 70% 55%, #ffffff55, transparent),
    radial-gradient(1px 1px at 85% 22%, #a8e6ff77, transparent),
    radial-gradient(1px 1px at 35% 75%, #ffffff44, transparent),
    radial-gradient(1.5px 1.5px at 92% 70%, #6ec6d888, transparent),
    radial-gradient(1px 1px at 55% 88%, #ffffff55, transparent);
  opacity: 0.85;
}
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #071018 0%, #0b1824 100%);
  border-right: 1px solid #1a4a5c;
}
[data-testid="stSidebar"] * { color: #cfe7f5 !important; }
h1, h2, h3 {
  font-family: 'Orbitron', sans-serif !important;
  color: #e8f7ff !important;
  letter-spacing: 0.04em;
}
[data-testid="stMetricValue"] {
  font-family: 'Orbitron', sans-serif;
  color: #7fffff !important;
  text-shadow: 0 0 12px #2ec4c666;
}
[data-testid="stMetricLabel"] { color: #9bb8c9 !important; }
.glass-panel {
  background: linear-gradient(145deg, rgba(12, 36, 52, 0.72), rgba(8, 20, 32, 0.55));
  border: 1px solid rgba(46, 196, 198, 0.35);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  margin-bottom: 1rem;
  box-shadow: 0 0 24px rgba(46, 196, 198, 0.08), inset 0 1px 0 rgba(255,255,255,0.06);
  backdrop-filter: blur(8px);
}
.risk-glass {
  background: linear-gradient(145deg, rgba(48, 20, 18, 0.65), rgba(20, 10, 12, 0.55));
  border: 1px solid rgba(232, 120, 80, 0.45);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  margin-bottom: 1rem;
  box-shadow: 0 0 22px rgba(232, 120, 80, 0.1);
}
.insight-card {
  background: rgba(10, 28, 40, 0.75);
  border-left: 3px solid #2ec4c6;
  border-radius: 10px;
  padding: 0.9rem 1rem;
  margin-bottom: 0.75rem;
}
.insight-card.danger { border-left-color: #e87450; }
.insight-card.warn { border-left-color: #e8b84a; }
.insight-card.info { border-left-color: #4aa8e8; }
.insight-card.ok { border-left-color: #3dcf8e; }
.insight-card h4 {
  margin: 0 0 0.35rem 0;
  font-family: 'Orbitron', sans-serif;
  font-size: 0.95rem;
  color: #e8f7ff;
}
.insight-card p { margin: 0; color: #b8d0de; font-size: 0.92rem; line-height: 1.45; }
.mission-title {
  font-family: 'Orbitron', sans-serif;
  font-size: 1.65rem;
  background: linear-gradient(90deg, #7fffff, #4aa8e8);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 0.15rem;
}
.caption-dim { color: #7a9aab; font-size: 0.85rem; margin-bottom: 1rem; }
div[data-testid="stTabs"] button { font-family: 'Exo 2', sans-serif; }
</style>
"""

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(5,12,20,0.55)",
    font=dict(color="#c8dce8", family="Exo 2"),
    margin=dict(l=40, r=20, t=50, b=40),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
)


def _money(v: float) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "$0"


@st.cache_data(show_spinner=False)
def _load_csv(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


@st.cache_data(show_spinner=False)
def _load_kpi(path_str: str) -> dict:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


@st.cache_data(show_spinner=False)
def _load_audit(path_str: str) -> dict[str, pd.DataFrame]:
    return load_audit_bundle(path_str)


def _prefer_csv(forecast_dir: Path, base: str, may_aug_suffix: bool = True) -> pd.DataFrame:
    if may_aug_suffix:
        p = forecast_dir / f"{base}_may_aug.csv"
        if p.exists():
            return _load_csv(str(p))
    return _load_csv(str(forecast_dir / f"{base}.csv"))


def _plotly_bar(
    df: pd.DataFrame,
    x: str,
    y: str,
    title: str,
    color: str = "#2ec4c6",
    *,
    horizontal: bool = False,
) -> go.Figure:
    if horizontal:
        fig = px.bar(df, x=x, y=y, title=title, orientation="h")
    else:
        fig = px.bar(df, x=x, y=y, title=title)
    fig.update_traces(marker_color=color, marker_line_width=0)
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=14)
    fig.update_xaxes(gridcolor="#1a3344", zeroline=False)
    fig.update_yaxes(gridcolor="#1a3344", zeroline=False)
    return fig


def _plotly_line(df: pd.DataFrame, x: str, y: str, title: str) -> go.Figure:
    fig = px.line(df, x=x, y=y, title=title, markers=True)
    fig.update_traces(line_color="#7fffff", marker=dict(size=5, color="#4aa8e8"))
    fig.update_layout(**PLOTLY_LAYOUT, title_font_size=14)
    fig.update_xaxes(gridcolor="#1a3344")
    fig.update_yaxes(gridcolor="#1a3344")
    return fig


def _recompute_kpis(filtered: pd.DataFrame, risk_f: pd.DataFrame, kpi: dict) -> dict:
    """KPIs from filtered outcomes when filters active; else global kpi."""
    if filtered.empty:
        return kpi

    def _sum_stage(stage: str) -> tuple[float, int]:
        m = filtered["outcome_stage"] == stage
        return float(filtered.loc[m, "expected_amount"].sum()), int(m.sum())

    on_amt, on_n = _sum_stage("on_track")
    ov_amt, ov_n = _sum_stage("overdue")
    den_amt, den_n = _sum_stage("denied")
    rej_amt, rej_n = _sum_stage("rejected")
    return {
        **kpi,
        "on_track_amount": on_amt,
        "on_track_count": on_n,
        "overdue_amount": ov_amt,
        "overdue_count": ov_n,
        "denied_amount": den_amt + rej_amt,
        "denied_count": den_n + rej_n,
        "projected_cash_in": on_amt + ov_amt,
        "risk_exposure_amount": float(risk_f["exposure_amount"].sum())
        if not risk_f.empty and "exposure_amount" in risk_f.columns
        else kpi.get("risk_exposure_amount", 0),
        "risk_visit_count": int(risk_f["webpt_patient_id"].nunique())
        if not risk_f.empty and "webpt_patient_id" in risk_f.columns
        else kpi.get("risk_visit_count", 0),
    }


def main() -> None:
    st.set_page_config(
        page_title="Cash Conversion Cycle — Orbital Forecast",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(SPACE_CSS, unsafe_allow_html=True)

    st.sidebar.markdown("### Navigation array")
    forecast_dir = Path(st.sidebar.text_input("Forecast dir", str(DEFAULT_FORECAST)))
    audit_dir = Path(
        st.sidebar.text_input(
            "Audit dir",
            str(forecast_dir.parent / "audit"),
        )
    )

    kpi_path = forecast_dir / "kpi_summary.json"
    if not kpi_path.exists():
        st.error(f"No kpi_summary.json in {forecast_dir}. Run: python -m cashflow_forecast build")
        return

    kpi = _load_kpi(str(kpi_path))
    outcomes = _load_csv(str(forecast_dir / "outcome_stages.csv"))
    risk = _load_csv(str(forecast_dir / "risk_flags.csv"))
    actual_daily = _prefer_csv(forecast_dir, "actual_cash_daily")
    projected_daily = _prefer_csv(forecast_dir, "projected_cash_daily")
    projected_monthly = _prefer_csv(forecast_dir, "projected_cash_monthly")
    proj_by_fac = _prefer_csv(forecast_dir, "projected_cash_monthly_by_facility")
    proj_by_ins = _prefer_csv(forecast_dir, "projected_cash_monthly_by_insurance")
    stage_counts = _load_csv(str(forecast_dir / "outcome_stage_counts.csv"))
    overdue_ins = _load_csv(str(forecast_dir / "overdue_by_insurance.csv"))
    denied_ins = _load_csv(str(forecast_dir / "denied_by_insurance.csv"))
    risk_ins = _load_csv(str(forecast_dir / "risk_by_insurance.csv"))
    sla = _load_csv(str(forecast_dir / "payer_sla.csv"))

    audit = _load_audit(str(audit_dir)) if audit_dir.exists() else {
        "summary": pd.DataFrame(),
        "cpt_violations": pd.DataFrame(),
        "icd_violations": pd.DataFrame(),
        "unmapped_insurance": pd.DataFrame(),
    }

    # --- Filters ---
    facilities = sorted(outcomes["facility_name"].dropna().unique()) if not outcomes.empty else []
    insurers = sorted(outcomes["ins_name"].dropna().unique()) if not outcomes.empty else []
    stages = sorted(outcomes["outcome_stage"].dropna().unique()) if not outcomes.empty else []
    risk_flags = sorted(risk["risk_flag"].dropna().unique()) if not risk.empty else []
    months = []
    if not projected_monthly.empty and "period" in projected_monthly.columns:
        months = sorted(projected_monthly["period"].astype(str).unique())

    sel_fac = st.sidebar.multiselect("Clinic", facilities, default=[])
    sel_ins = st.sidebar.multiselect("Insurance", insurers, default=[])
    sel_stage = st.sidebar.multiselect("Outcome stage", stages, default=stages)
    sel_risk = st.sidebar.multiselect("Risk flags", risk_flags, default=risk_flags)
    sel_month = st.sidebar.multiselect("Cash month (projected)", months, default=months)
    sel_sev = st.sidebar.multiselect(
        "Audit severity",
        ["error", "warning"],
        default=["error", "warning"],
    )

    filtered = outcomes.copy()
    if not filtered.empty:
        if "expected_amount" in filtered.columns:
            filtered["expected_amount"] = pd.to_numeric(filtered["expected_amount"], errors="coerce").fillna(0)
        if sel_fac:
            filtered = filtered[filtered["facility_name"].isin(sel_fac)]
        if sel_ins:
            filtered = filtered[filtered["ins_name"].isin(sel_ins)]
        if sel_stage:
            filtered = filtered[filtered["outcome_stage"].isin(sel_stage)]

    risk_f = risk.copy()
    if not risk_f.empty:
        if "exposure_amount" in risk_f.columns:
            risk_f["exposure_amount"] = pd.to_numeric(risk_f["exposure_amount"], errors="coerce").fillna(0)
        if sel_fac and "facility_name" in risk_f.columns:
            risk_f = risk_f[risk_f["facility_name"].isin(sel_fac)]
        if sel_ins and "ins_name" in risk_f.columns:
            risk_f = risk_f[risk_f["ins_name"].isin(sel_ins)]
        if sel_risk:
            risk_f = risk_f[risk_f["risk_flag"].isin(sel_risk)]

    view_kpi = _recompute_kpis(filtered, risk_f, kpi)

    # Month-filter projected frames
    def _month_filter(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or not sel_month or "period" not in df.columns:
            return df
        return df[df["period"].astype(str).isin(sel_month)]

    projected_monthly_f = _month_filter(projected_monthly)
    proj_by_fac_f = _month_filter(proj_by_fac)
    proj_by_ins_f = _month_filter(proj_by_ins)
    if sel_fac and not proj_by_fac_f.empty and "facility_name" in proj_by_fac_f.columns:
        proj_by_fac_f = proj_by_fac_f[proj_by_fac_f["facility_name"].isin(sel_fac)]
    if sel_ins and not proj_by_ins_f.empty and "ins_name" in proj_by_ins_f.columns:
        proj_by_ins_f = proj_by_ins_f[proj_by_ins_f["ins_name"].isin(sel_ins)]

    cpt_f, icd_f = filter_audit(
        audit["cpt_violations"],
        audit["icd_violations"],
        facilities=sel_fac or None,
        insurers=sel_ins or None,
        severities=sel_sev or None,
    )

    st.markdown('<div class="mission-title">CASH CONVERSION CYCLE</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="caption-dim">Orbital forecast · as-of {kpi.get("as_of", "")} · '
        f"filters reshape KPIs & charts in real time</div>",
        unsafe_allow_html=True,
    )

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Mission Control", "Cash Trajectory", "Business Insights", "Drill Decks"]
    )

    # ========== TAB 1 ==========
    with tab1:
        st.markdown('<div class="glass-panel">', unsafe_allow_html=True)
        st.subheader("Outcome — cash flow summary")
        st.caption("Outcome stages are realized collection states — not predictive risk flags.")
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Actual received", _money(view_kpi.get("actual_cash_received", 0)))
        c2.metric("Projected in", _money(view_kpi.get("projected_cash_in", 0)))
        c3.metric("On track", _money(view_kpi.get("on_track_amount", 0)), f"{view_kpi.get('on_track_count', 0):,} lines")
        c4.metric("Overdue", _money(view_kpi.get("overdue_amount", 0)), f"{view_kpi.get('overdue_count', 0):,} lines")
        c5.metric("Denied+reject", _money(view_kpi.get("denied_amount", 0)), f"{view_kpi.get('denied_count', 0):,} lines")
        c6.metric("May–Aug proj", _money(view_kpi.get("projected_cash_may_aug", 0)))
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="risk-glass">', unsafe_allow_html=True)
        st.subheader("Risk exposure — predictive")
        st.caption("Risk flags (audit / unsubmitted) are separate from outcome stages.")
        r1, r2, r3 = st.columns(3)
        r1.metric("Risk exposure $", _money(view_kpi.get("risk_exposure_amount", 0)))
        r2.metric("Visits with risk", f"{view_kpi.get('risk_visit_count', 0):,}")
        if not risk_f.empty and "risk_flag" in risk_f.columns:
            by_flag = (
                risk_f.groupby("risk_flag", as_index=False)["exposure_amount"]
                .sum()
                .sort_values("exposure_amount", ascending=False)
            )
            with r3:
                st.dataframe(by_flag, use_container_width=True, hide_index=True)
        st.markdown("</div>", unsafe_allow_html=True)

        col_a, col_b = st.columns(2)
        with col_a:
            if not filtered.empty:
                dist = (
                    filtered.groupby("outcome_stage", as_index=False)
                    .agg(line_count=("outcome_stage", "size"), amount=("expected_amount", "sum"))
                )
            else:
                dist = stage_counts
            if not dist.empty:
                ycol = "line_count" if "line_count" in dist.columns else dist.columns[-1]
                st.plotly_chart(
                    _plotly_bar(dist, "outcome_stage", ycol, "Outcome stage distribution", "#4aa8e8"),
                    use_container_width=True,
                )
        with col_b:
            if not overdue_ins.empty:
                top = overdue_ins.head(12).copy()
                if "expected_payment" in top.columns:
                    top["expected_payment"] = pd.to_numeric(top["expected_payment"], errors="coerce")
                    st.plotly_chart(
                        _plotly_bar(
                            top,
                            "expected_payment",
                            "ins_name",
                            "Top overdue by insurance",
                            "#e8b84a",
                            horizontal=True,
                        ),
                        use_container_width=True,
                    )
            elif not sla.empty:
                st.dataframe(
                    sla[["webpt_insurance", "sample_count", "median_lag_days", "confidence"]].head(15),
                    use_container_width=True,
                    hide_index=True,
                )

    # ========== TAB 2 ==========
    with tab2:
        grain = st.radio("Projected grain", ["Monthly", "Daily"], horizontal=True)
        c1, c2 = st.columns(2)
        with c1:
            if grain == "Monthly" and not projected_monthly_f.empty:
                df = projected_monthly_f.copy()
                df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
                st.plotly_chart(
                    _plotly_bar(df, "period", "amount", "Projected cash-in by month", "#2ec4c6"),
                    use_container_width=True,
                )
            elif not projected_daily.empty:
                df = projected_daily.copy()
                df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
                st.plotly_chart(
                    _plotly_line(df, "period", "amount", "Projected cash-in by day"),
                    use_container_width=True,
                )
            else:
                st.info("No projected cash series")
        with c2:
            if not actual_daily.empty:
                df = actual_daily.copy()
                df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
                st.plotly_chart(
                    _plotly_line(df, "period", "amount", "Actual cash by check date"),
                    use_container_width=True,
                )
            else:
                st.info("No actual daily cash")

        c3, c4 = st.columns(2)
        with c3:
            if not proj_by_fac_f.empty:
                agg = (
                    proj_by_fac_f.assign(amount=pd.to_numeric(proj_by_fac_f["amount"], errors="coerce"))
                    .groupby("facility_name", as_index=False)["amount"]
                    .sum()
                    .sort_values("amount", ascending=False)
                    .head(15)
                )
                st.plotly_chart(
                    _plotly_bar(agg, "amount", "facility_name", "Projected by clinic (selected months)", "#3dcf8e", horizontal=True),
                    use_container_width=True,
                )
        with c4:
            if not proj_by_ins_f.empty:
                agg = (
                    proj_by_ins_f.assign(amount=pd.to_numeric(proj_by_ins_f["amount"], errors="coerce"))
                    .groupby("ins_name", as_index=False)["amount"]
                    .sum()
                    .sort_values("amount", ascending=False)
                    .head(15)
                )
                st.plotly_chart(
                    _plotly_bar(agg, "amount", "ins_name", "Projected by insurance (selected months)", "#4aa8e8", horizontal=True),
                    use_container_width=True,
                )

        if not sla.empty:
            st.markdown('<div class="glass-panel">', unsafe_allow_html=True)
            st.subheader("Payer SLA — median DOS → cash lag")
            st.dataframe(
                sla.head(25),
                use_container_width=True,
                hide_index=True,
            )
            st.markdown("</div>", unsafe_allow_html=True)

    # ========== TAB 3 ==========
    with tab3:
        if not audit_dir.exists():
            st.warning(f"Audit folder not found: {audit_dir}")
        else:
            cards = build_insight_cards(
                audit["summary"], cpt_f, icd_f, audit["unmapped_insurance"]
            )
            st.subheader("Business insights — billing audit")
            st.caption(
                "Derived from CPT/ICD rule findings and unmapped payors. "
                "These are predictive signals, not cash outcome stages."
            )
            for card in cards:
                st.markdown(
                    f'<div class="insight-card {card["tone"]}">'
                    f'<h4>{card["title"]}</h4><p>{card["body"]}</p></div>',
                    unsafe_allow_html=True,
                )

            # Link to forecast risk exposure for audit flags
            audit_risk = risk_audit_exposure(risk_f)
            if not audit_risk.empty:
                exp = float(audit_risk["exposure_amount"].sum())
                st.markdown(
                    f'<div class="insight-card warn"><h4>Linked forecast risk exposure</h4>'
                    f"<p>Filtered risk_flags tagged audit_cpt / audit_icd: "
                    f"<b>{_money(exp)}</b> across {audit_risk['webpt_patient_id'].nunique():,} visits.</p></div>",
                    unsafe_allow_html=True,
                )

            a, b = st.columns(2)
            with a:
                top = top_cpt_rules(cpt_f, 10)
                if not top.empty:
                    st.plotly_chart(
                        _plotly_bar(top, "count", "rule_id", "Top CPT violation rules", "#e87450", horizontal=True),
                        use_container_width=True,
                    )
            with b:
                icd_cat = icd_category_breakdown(icd_f)
                if not icd_cat.empty:
                    st.plotly_chart(
                        _plotly_bar(
                            icd_cat.head(10),
                            "count",
                            "category",
                            "ICD conflict categories",
                            "#e8b84a",
                            horizontal=True,
                        ),
                        use_container_width=True,
                    )

            matrix = facility_severity_matrix(cpt_f)
            if not matrix.empty:
                st.markdown('<div class="glass-panel">', unsafe_allow_html=True)
                st.subheader("Clinic × severity (CPT violations)")
                st.dataframe(matrix, use_container_width=True, hide_index=True)
                st.markdown("</div>", unsafe_allow_html=True)

            guide = icd_guidance_samples(icd_f)
            if not guide.empty:
                st.markdown('<div class="glass-panel">', unsafe_allow_html=True)
                st.subheader("ICD guidance samples")
                st.dataframe(guide, use_container_width=True, hide_index=True)
                st.markdown("</div>", unsafe_allow_html=True)

            unmapped = unmapped_ranked(audit["unmapped_insurance"])
            if not unmapped.empty:
                st.markdown('<div class="glass-panel">', unsafe_allow_html=True)
                st.subheader("Unmapped insurance (rules cannot fire)")
                st.dataframe(unmapped, use_container_width=True, hide_index=True)
                st.markdown("</div>", unsafe_allow_html=True)

            if not audit["summary"].empty:
                with st.expander("Raw audit summary"):
                    st.dataframe(audit["summary"], use_container_width=True, hide_index=True)

    # ========== TAB 4 ==========
    with tab4:
        t1, t2, t3 = st.columns(3)
        with t1:
            st.subheader("Overdue by insurance")
            st.dataframe(overdue_ins.head(30), use_container_width=True, hide_index=True)
        with t2:
            st.subheader("Denied / rejected")
            st.dataframe(denied_ins.head(30), use_container_width=True, hide_index=True)
        with t3:
            st.subheader("Risk by insurance")
            st.dataframe(risk_ins.head(30), use_container_width=True, hide_index=True)

        q = st.text_input("Search patient / insurance / facility", "")
        lim = st.slider("Row limit", 50, 2000, 400, 50)

        def _search(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty or not q.strip():
                return df.head(lim)
            mask = pd.Series(False, index=df.index)
            for col in df.columns:
                if df[col].dtype == object or str(df[col].dtype) == "string":
                    mask |= df[col].astype(str).str.contains(q, case=False, na=False)
            return df.loc[mask].head(lim)

        st.subheader("Outcome lines")
        st.dataframe(_search(filtered), use_container_width=True, hide_index=True)

        st.subheader("Risk flags")
        st.dataframe(_search(risk_f), use_container_width=True, hide_index=True)

        st.subheader("CPT violations (filtered)")
        st.dataframe(_search(cpt_f), use_container_width=True, hide_index=True)

        st.subheader("ICD violations (filtered)")
        st.dataframe(_search(icd_f), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
