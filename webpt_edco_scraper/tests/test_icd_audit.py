"""Unit tests for generalized ICD denial audit checks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit.icd10_catalog import (  # noqa: E402
    Icd10Catalog,
    IcdCode,
    format_icd10_code,
    parse_order_line,
)
from scripts.audit_billing import (  # noqa: E402
    NoteRecord,
    audit_icd,
    check_bilateral_split,
    check_invalid_or_nonbillable,
    check_lateralization,
    check_site_pair_map,
    load_yaml,
)

RULES_PATH = ROOT / "audit" / "icd_denial_rules.yaml"


@pytest.fixture(scope="module")
def icd_config() -> dict:
    return load_yaml(RULES_PATH)


@pytest.fixture(scope="module")
def mini_catalog() -> Icd10Catalog:
    entries = {
        "M54.50": IcdCode("M54.50", "Low back pain, unspecified", True, "M"),
        "M54.5": IcdCode("M54.5", "Low back pain", False, "M"),
        "M17.11": IcdCode("M17.11", "Unilateral primary OA right knee", True, "M"),
        "S83": IcdCode("S83", "Dislocation and sprain of joints... knee", False, "S"),
        "ZZZ.99": IcdCode("ZZZ.99", "fake billable", True, "Z"),
    }
    return Icd10Catalog(entries)


def test_format_icd10_code_inserts_dot() -> None:
    assert format_icd10_code("M5450") == "M54.50"
    assert format_icd10_code("A00") == "A00"
    assert format_icd10_code("m25.561") == "M25.561"


def test_parse_order_line_billable_flag() -> None:
    header = (
        "00001 A00     0 Cholera                                                      Cholera"
    )
    leaf = (
        "00002 A000    1 Cholera due to Vibrio cholerae 01, biovar cholerae           "
        "Cholera due to Vibrio cholerae 01, biovar cholerae"
    )
    h = parse_order_line(header)
    b = parse_order_line(leaf)
    assert h is not None and h.code == "A00" and h.billable is False
    assert b is not None and b.code == "A00.0" and b.billable is True


def test_lateralization_unspecified_with_side() -> None:
    hit = check_lateralization({"M25.561", "M25.569"})
    assert hit == ["M25.561", "M25.569"]
    assert check_lateralization({"M25.561", "M25.562"}) is None
    # LBP subtypes ending in 1/9 should not count as laterality.
    assert check_lateralization({"M54.51", "M54.59"}) is None


def test_site_pair_map_joint_pain_oa(icd_config: dict) -> None:
    pairs = icd_config["site_maps"]["joint_pain_oa"]
    hit = check_site_pair_map({"M25.561", "M17.11"}, pairs)
    assert hit == ["M25.561", "M17.11"]
    # Hip pain + hip OA
    hit_hip = check_site_pair_map({"M25.551", "M16.11"}, pairs)
    assert hit_hip == ["M25.551", "M16.11"]
    # Cross-site should not fire (knee pain + hip OA)
    assert check_site_pair_map({"M25.561", "M16.11"}, pairs) is None


def test_site_pair_map_prosthetic(icd_config: dict) -> None:
    pairs = icd_config["site_maps"]["joint_pain_prosthetic"]
    hit = check_site_pair_map({"M25.552", "Z96.642"}, pairs)
    assert hit == ["M25.552", "Z96.642"]
    assert check_site_pair_map({"M25.561", "Z96.642"}, pairs) is None


def test_bilateral_split(icd_config: dict) -> None:
    families = icd_config["bilateral_families"]
    hit = check_bilateral_split({"M17.11", "M17.12"}, families)
    assert hit == ["M17.11", "M17.12"]
    hit_hip = check_bilateral_split({"M16.11", "M16.12"}, families)
    assert hit_hip == ["M16.11", "M16.12"]
    assert check_bilateral_split({"M17.11"}, families) is None


def test_invalid_or_nonbillable(mini_catalog: Icd10Catalog) -> None:
    hit = check_invalid_or_nonbillable({"M54.5", "M54.50", "S83", "FAKE99"}, mini_catalog)
    assert hit == ["FAKE99", "M54.5", "S83"]


def test_audit_icd_union_diagnosis_and_treatment(icd_config: dict, mini_catalog: Icd10Catalog) -> None:
    note = NoteRecord(
        daily_note_id="DN1",
        diagnosis_icd_codes="M25.561",
        treatment_diagnosis_icd_codes="Z96.652",
    )
    rows = audit_icd(
        note,
        icd_config["rules"],
        site_maps=icd_config["site_maps"],
        bilateral_families=icd_config["bilateral_families"],
        catalog=mini_catalog,
    )
    rule_ids = {r["rule_id"] for r in rows}
    assert "status_vs_acute_prosthetic" in rule_ids
    status = next(r for r in rows if r["rule_id"] == "status_vs_acute_prosthetic")
    assert "M25.561" in status["diagnosis_icd_codes"]
    assert "Z96.652" in status["diagnosis_icd_codes"]


def test_audit_icd_radiculopathy_family(icd_config: dict, mini_catalog: Icd10Catalog) -> None:
    note = NoteRecord(
        daily_note_id="DN2",
        diagnosis_icd_codes="M54.12; G54.2",
    )
    rows = audit_icd(
        note,
        icd_config["rules"],
        site_maps=icd_config["site_maps"],
        bilateral_families=icd_config["bilateral_families"],
        catalog=mini_catalog,
    )
    assert any(r["rule_id"] == "anatomical_overlap_radiculopathy_nerve" for r in rows)


def test_rules_yaml_loads() -> None:
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    assert "site_maps" in raw
    assert "bilateral_families" in raw
    assert any(r.get("check") == "invalid_or_nonbillable" for r in raw["rules"])
