"""Link audit violations to Waystar denials/rejections via numeric match scores."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from cashflow_forecast.config import (
    MATCH_ACCEPT_THRESHOLD,
    MATCH_CPT,
    MATCH_DOS_EXACT,
    MATCH_DOS_NEAR,
    MATCH_NAME_EXACT,
    MATCH_PAYER,
    MATCH_REVIEW_THRESHOLD,
)


def _payer_overlap(ins: str, payer: str) -> bool:
    a = (ins or "").lower()
    b = (payer or "").lower()
    if not a or not b:
        return False
    # Token overlap on significant words
    stop = {"of", "the", "inc", "llc", "plan", "health", "insurance", "ny", "new", "york"}
    ta = {t for t in a.replace("-", " ").replace(",", " ").split() if t not in stop and len(t) > 2}
    tb = {t for t in b.replace("-", " ").replace(",", " ").split() if t not in stop and len(t) > 2}
    if not ta or not tb:
        return a in b or b in a
    return bool(ta & tb)


def _cpt_overlap(audit_cpts: str, denial_row: pd.Series) -> bool:
    codes = {c.strip() for c in (audit_cpts or "").replace(",", ";").split(";") if c.strip()}
    if not codes:
        return False
    for col in ("proc_code", "cpt_code", "cpt_codes"):
        val = str(denial_row.get(col) or "")
        if any(c in val for c in codes):
            return True
    return False


def score_pair(audit_row: pd.Series, waystar_row: pd.Series) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []

    if audit_row.get("name_key") and audit_row["name_key"] == waystar_row.get("name_key"):
        score += MATCH_NAME_EXACT
        signals.append("name_exact")
    else:
        return 0, []

    a_dos = audit_row.get("date_of_service")
    w_dos = waystar_row.get("service_date")
    if a_dos and w_dos:
        delta = abs((a_dos - w_dos).days)
        if delta == 0:
            score += MATCH_DOS_EXACT
            signals.append("dos_exact")
        elif delta == 1:
            score += MATCH_DOS_NEAR
            signals.append("dos_near")

    if _payer_overlap(str(audit_row.get("insurance_name") or ""), str(waystar_row.get("payer") or "")):
        score += MATCH_PAYER
        signals.append("payer")

    if _cpt_overlap(str(audit_row.get("cpt_codes") or ""), waystar_row):
        score += MATCH_CPT
        signals.append("cpt")

    return score, signals


def link_audit_to_waystar(
    audit: pd.DataFrame,
    denials: pd.DataFrame,
    rejections: pd.DataFrame | None = None,
    *,
    accept_threshold: int = MATCH_ACCEPT_THRESHOLD,
    review_threshold: int = MATCH_REVIEW_THRESHOLD,
) -> pd.DataFrame:
    """Return best match per audit row with match_score and match_signals."""
    if audit.empty:
        return pd.DataFrame()

    waystar_parts = []
    if denials is not None and not denials.empty:
        d = denials.copy()
        d["_ws_kind"] = "denial"
        d["_ws_amount"] = d.get("denied_amount", 0.0)
        d["_ws_id"] = d.get("denial_id", "")
        waystar_parts.append(d)
    if rejections is not None and not rejections.empty:
        r = rejections.copy()
        r["_ws_kind"] = "rejection"
        r["_ws_amount"] = r.get("charges", 0.0)
        r["_ws_id"] = r.get("claim_id", "")
        waystar_parts.append(r)

    if not waystar_parts:
        out = audit.copy()
        out["match_score"] = 0
        out["match_signals"] = ""
        out["match_status"] = "unmatched"
        return out

    ws = pd.concat(waystar_parts, ignore_index=True)
    # Index by name_key for speed
    by_name: dict[str, list[int]] = {}
    for i, key in enumerate(ws["name_key"].tolist()):
        if key:
            by_name.setdefault(key, []).append(i)

    results: list[dict] = []
    for _, arow in audit.iterrows():
        key = arow.get("name_key") or ""
        candidates = by_name.get(key, [])
        best_score = 0
        best_signals: list[str] = []
        best_idx = None
        for idx in candidates:
            sc, sigs = score_pair(arow, ws.iloc[idx])
            if sc > best_score:
                best_score = sc
                best_signals = sigs
                best_idx = idx

        row = {
            "patient_id": arow.get("patient_id", ""),
            "patient_name": arow.get("patient_name", ""),
            "date_of_service": arow.get("date_of_service"),
            "insurance_name": arow.get("insurance_name", ""),
            "rule_id": arow.get("rule_id", ""),
            "violation_type": arow.get("violation_type", ""),
            "severity": arow.get("severity", ""),
            "match_score": best_score,
            "match_signals": "|".join(best_signals),
        }
        if best_score >= accept_threshold:
            row["match_status"] = "accepted"
        elif best_score >= review_threshold:
            row["match_status"] = "review"
        else:
            row["match_status"] = "unmatched"

        if best_idx is not None and best_score >= review_threshold:
            w = ws.iloc[best_idx]
            row["matched_kind"] = w.get("_ws_kind", "")
            row["matched_id"] = w.get("_ws_id", "")
            row["matched_payer"] = w.get("payer", "")
            row["matched_amount"] = w.get("_ws_amount", 0.0)
            row["matched_service_date"] = w.get("service_date")
        else:
            row["matched_kind"] = ""
            row["matched_id"] = ""
            row["matched_payer"] = ""
            row["matched_amount"] = 0.0
            row["matched_service_date"] = None

        results.append(row)

    return pd.DataFrame(results)
