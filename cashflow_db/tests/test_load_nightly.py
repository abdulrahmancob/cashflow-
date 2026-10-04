"""Nightly loader order (no live warehouse)."""

import inspect

from cashflow_db.loaders.nightly import (
    NIGHTLY_STEPS,
    resolve_waystar_claims_path,
    run_load_nightly,
)


def test_nightly_step_order() -> None:
    assert NIGHTLY_STEPS == (
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
    assert "snowflake_kpi" not in NIGHTLY_STEPS
    nightly_src = inspect.getsource(run_load_nightly)
    assert "refresh_billing_collect_visit" in nightly_src
    assert "refresh_eligibility_sheet_facet" in nightly_src
    assert "schedule" not in NIGHTLY_STEPS
    assert "webpt" not in NIGHTLY_STEPS


def test_resolve_waystar_claims_path_explicit(tmp_path) -> None:
    p = tmp_path / "claims_recent.json"
    p.write_text("[]", encoding="utf-8")
    assert resolve_waystar_claims_path(p) == p


def test_resolve_waystar_claims_path_missing(tmp_path) -> None:
    missing = tmp_path / "nope.json"
    assert resolve_waystar_claims_path(missing) != missing
