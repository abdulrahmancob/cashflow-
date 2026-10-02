"""Nightly warehouse load: Snowflake visits/CPT + RevFlow checks + Waystar remits + map."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from cashflow_db.config import WAYSTAR_CLAIMS_CSV
from cashflow_db.loaders.load_checks_deposits import load_checks_deposits
from cashflow_db.loaders.load_cpt_guide import load_cpt_guide
from cashflow_db.loaders.load_forecast import load_rules
from cashflow_db.loaders.load_pt_city import load_pt_city_visit
from cashflow_db.loaders.load_pt_city_charges import load_pt_city_charges
from cashflow_db.loaders.load_pt_city_patient_map import load_pt_city_patient_map
from cashflow_db.loaders.load_revflow import load_revflow
from cashflow_db.loaders.load_tracker import load_tracker
from cashflow_db.loaders.load_waystar import load_waystar
from cashflow_db.loaders.load_waystar_claims import load_waystar_claims

NIGHTLY_STEPS: tuple[str, ...] = (
    "rules",
    "cpt_guide",
    "pt_city_visit",
    "pt_city_charges",
    "revflow",
    "tracker",
    "checks_deposits",
    "waystar_claims",
    "pt_city_patient_map",
    "waystar_denials",
)


def resolve_waystar_claims_path(explicit: Path | None = None) -> Path | None:
    if explicit and explicit.exists():
        return explicit
    if WAYSTAR_CLAIMS_CSV.exists():
        return WAYSTAR_CLAIMS_CSV
    parent = WAYSTAR_CLAIMS_CSV.parent
    for name in ("claims_recent.json", "claims_merged.json"):
        cand = parent / name
        if cand.exists():
            return cand
    recent = Path("/data/waystar/claims_recent/claims_recent.json")
    if recent.exists():
        return recent
    merged = Path("/data/waystar/claims_listing_2026/claims_merged.json")
    if merged.exists():
        return merged
    return None


def run_load_nightly(*, limit: int | None = None) -> dict[str, Any]:
    claims_path = resolve_waystar_claims_path()
    steps: dict[str, Callable[[], Any]] = {
        "rules": load_rules,
        "cpt_guide": load_cpt_guide,
        "pt_city_visit": lambda: load_pt_city_visit(limit=limit),
        "pt_city_charges": lambda: load_pt_city_charges(limit=limit),
        "revflow": lambda: load_revflow(limit_files=limit, bootstrap_claims=False),
        "tracker": load_tracker,
        "checks_deposits": load_checks_deposits,
        "waystar_claims": lambda: load_waystar_claims(claims_path=claims_path, limit=limit),
        "pt_city_patient_map": load_pt_city_patient_map,
        "waystar_denials": lambda: load_waystar(limit=limit),
    }
    results: dict[str, Any] = {"claims_path": str(claims_path) if claims_path else None}
    for name in NIGHTLY_STEPS:
        fn = steps[name]
        if name == "waystar_denials":
            try:
                results[name] = fn()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"skipped": True, "error": str(exc)[:500]}
            continue
        results[name] = fn()
    from cashflow_db.repository.billing_analysis import refresh_billing_collect_visit

    refresh_billing_collect_visit()
    results["billing_collect_visit"] = "refreshed"
    results["ok"] = True
    results["steps"] = list(NIGHTLY_STEPS)
    return results
