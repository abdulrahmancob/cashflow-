"""Audit aggregations for Business Insights tab."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_audit_bundle(audit_dir: Path | str) -> dict[str, pd.DataFrame]:
    audit_dir = Path(audit_dir)
    out: dict[str, pd.DataFrame] = {}
    for name in ("summary", "cpt_violations", "icd_violations", "unmapped_insurance"):
        path = audit_dir / f"{name}.csv"
        if path.exists():
            out[name] = pd.read_csv(path, dtype=str, keep_default_na=False)
        else:
            out[name] = pd.DataFrame()
    # numeric helpers
    if not out["summary"].empty and "count" in out["summary"].columns:
        out["summary"]["count"] = pd.to_numeric(out["summary"]["count"], errors="coerce").fillna(0).astype(int)
    if not out["unmapped_insurance"].empty and "note_count" in out["unmapped_insurance"].columns:
        out["unmapped_insurance"]["note_count"] = (
            pd.to_numeric(out["unmapped_insurance"]["note_count"], errors="coerce").fillna(0).astype(int)
        )
    return out


def _meta_count(summary: pd.DataFrame, rule_id: str) -> int:
    if summary.empty:
        return 0
    m = summary[(summary.get("section") == "meta") & (summary.get("rule_id") == rule_id)]
    if m.empty:
        return 0
    return int(m.iloc[0]["count"])


def build_insight_cards(
    summary: pd.DataFrame,
    cpt: pd.DataFrame,
    icd: pd.DataFrame,
    unmapped: pd.DataFrame,
) -> list[dict[str, str]]:
    """Human-readable business insight cards from audit aggregates."""
    cards: list[dict[str, str]] = []

    total_notes = _meta_count(summary, "total_notes")
    cpt_rows = _meta_count(summary, "cpt_violation_rows") or (len(cpt) if not cpt.empty else 0)
    icd_rows = _meta_count(summary, "icd_violation_rows") or (len(icd) if not icd.empty else 0)
    unmapped_notes = _meta_count(summary, "notes_with_cpt_unmapped_insurance")

    if not cpt.empty and "rule_id" in cpt.columns:
        top_rule = cpt["rule_id"].value_counts().index[0]
        top_n = int(cpt["rule_id"].value_counts().iloc[0])
        err_pct = 0.0
        if "severity" in cpt.columns and len(cpt):
            err_pct = 100.0 * (cpt["severity"].str.lower() == "error").sum() / len(cpt)
        cards.append(
            {
                "title": "CPT billing rule pressure",
                "body": (
                    f"{cpt_rows:,} CPT violation rows across the audit window. "
                    f"Top rule `{top_rule}` accounts for {top_n:,} hits "
                    f"({100.0 * top_n / max(cpt_rows, 1):.0f}% of CPT findings). "
                    f"{err_pct:.0f}% of CPT rows are severity=error."
                ),
                "tone": "danger",
            }
        )

    if not icd.empty:
        top_fac = ""
        top_ins = ""
        if "facility_name" in icd.columns and icd["facility_name"].astype(str).str.strip().ne("").any():
            top_fac = str(icd["facility_name"].value_counts().index[0])
        if "insurance_name" in icd.columns and icd["insurance_name"].astype(str).str.strip().ne("").any():
            top_ins = str(icd["insurance_name"].value_counts().index[0])
        loc = []
        if top_fac:
            loc.append(f"clinic {top_fac}")
        if top_ins:
            loc.append(f"payor {top_ins}")
        where = " / ".join(loc) if loc else "multiple clinics"
        cards.append(
            {
                "title": "ICD conflict concentration",
                "body": (
                    f"{icd_rows:,} ICD denial-risk conflicts detected. "
                    f"Volume concentrates in {where}. "
                    "These are predictive documentation risks — not the same as cash outcome stages."
                ),
                "tone": "warn",
            }
        )

    if not unmapped.empty:
        n_ins = len(unmapped)
        notes = int(unmapped["note_count"].sum()) if "note_count" in unmapped.columns else unmapped_notes
        cards.append(
            {
                "title": "Unmapped insurance blind spot",
                "body": (
                    f"{n_ins:,} insurance names are unmapped to CPT guide rules, "
                    f"touching ~{notes:,} notes"
                    + (f" ({unmapped_notes:,} from summary meta)." if unmapped_notes else ".")
                    + " Rules cannot fire for these payors — audit coverage is incomplete."
                ),
                "tone": "info",
            }
        )

    if total_notes:
        mapped = _meta_count(summary, "notes_with_cpt_mapped")
        cards.append(
            {
                "title": "Audit coverage",
                "body": (
                    f"{total_notes:,} daily notes loaded · "
                    f"{mapped:,} with CPT mapped to an insurance rule "
                    f"({100.0 * mapped / max(total_notes, 1):.0f}% coverage)."
                ),
                "tone": "ok",
            }
        )

    return cards


def top_cpt_rules(cpt: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    if cpt.empty or "rule_id" not in cpt.columns:
        return pd.DataFrame(columns=["rule_id", "count", "error_share"])
    g = cpt.groupby("rule_id", as_index=False).agg(count=("rule_id", "size"))
    if "severity" in cpt.columns:
        err = (
            cpt.assign(_err=cpt["severity"].str.lower().eq("error"))
            .groupby("rule_id")["_err"]
            .mean()
            .rename("error_share")
        )
        g = g.merge(err, on="rule_id", how="left")
        g["error_share"] = (g["error_share"].fillna(0) * 100).round(1)
    else:
        g["error_share"] = 0.0
    return g.sort_values("count", ascending=False).head(n)


def facility_severity_matrix(cpt: pd.DataFrame) -> pd.DataFrame:
    if cpt.empty or "facility_name" not in cpt.columns or "severity" not in cpt.columns:
        return pd.DataFrame()
    df = cpt.copy()
    df["facility_name"] = df["facility_name"].astype(str).str.strip().replace("", "(blank)")
    df["severity"] = df["severity"].astype(str).str.lower().str.strip()
    pivot = pd.crosstab(df["facility_name"], df["severity"])
    pivot["total"] = pivot.sum(axis=1)
    return pivot.sort_values("total", ascending=False).head(20).reset_index()


def icd_category_breakdown(icd: pd.DataFrame) -> pd.DataFrame:
    if icd.empty:
        return pd.DataFrame(columns=["category", "count"])
    col = "category" if "category" in icd.columns else "rule_id"
    g = icd.groupby(col, as_index=False).size().rename(columns={"size": "count", col: "category"})
    return g.sort_values("count", ascending=False)


def icd_guidance_samples(icd: pd.DataFrame, n: int = 8) -> pd.DataFrame:
    if icd.empty or "correct_approach" not in icd.columns:
        return pd.DataFrame()
    cols = [c for c in ("rule_id", "category", "description", "correct_approach", "facility_name") if c in icd.columns]
    # one sample per rule_id
    if "rule_id" in icd.columns:
        return icd.drop_duplicates("rule_id")[cols].head(n)
    return icd[cols].head(n)


def unmapped_ranked(unmapped: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    if unmapped.empty:
        return pd.DataFrame()
    df = unmapped.copy()
    if "note_count" in df.columns:
        df = df.sort_values("note_count", ascending=False)
    return df.head(n)


def filter_audit(
    cpt: pd.DataFrame,
    icd: pd.DataFrame,
    *,
    facilities: list[str] | None = None,
    insurers: list[str] | None = None,
    severities: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cpt_f = cpt.copy() if cpt is not None else pd.DataFrame()
    icd_f = icd.copy() if icd is not None else pd.DataFrame()

    def _apply(df: pd.DataFrame, fac_col: str, ins_col: str) -> pd.DataFrame:
        if df.empty:
            return df
        out = df
        if facilities and fac_col in out.columns:
            out = out[out[fac_col].isin(facilities)]
        if insurers and ins_col in out.columns:
            out = out[out[ins_col].isin(insurers)]
        if severities and "severity" in out.columns:
            sev = {s.lower() for s in severities}
            out = out[out["severity"].astype(str).str.lower().isin(sev)]
        return out

    return (
        _apply(cpt_f, "facility_name", "insurance_name"),
        _apply(icd_f, "facility_name", "insurance_name"),
    )


def risk_audit_exposure(risk: pd.DataFrame) -> pd.DataFrame:
    """Forecast risk_flags rows tied to audit (audit_cpt / audit_icd)."""
    if risk is None or risk.empty or "risk_flag" not in risk.columns:
        return pd.DataFrame()
    mask = risk["risk_flag"].astype(str).str.lower().isin(["audit_cpt", "audit_icd"])
    sub = risk.loc[mask].copy()
    if sub.empty:
        return sub
    if "exposure_amount" in sub.columns:
        sub["exposure_amount"] = pd.to_numeric(sub["exposure_amount"], errors="coerce").fillna(0.0)
    return sub
