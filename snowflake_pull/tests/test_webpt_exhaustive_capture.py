"""Tests for inventory / raw coverage / enrich-from-raw / coverage gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_enrich_parse import enrich_case_from_raw, parse_edoc_list_metadata
from case_raw_capture import ensure_raw_layout, raw_coverage, save_text
from patient_chart_api import parse_patient_chart_html
from patient_payments_api import parse_patient_payments_html
from snowflake_pull.case_export_aggregate import evaluate_promote_gate
from snowflake_pull.webpt_coverage import build_coverage_report
from snowflake_pull.webpt_inventory import (
    build_inventory_from_http_log,
    normalize_endpoint,
)


FIXTURE_CHART = next(
    (SCRAPER / "output").rglob("patient_chart_23863774_68908102.html"),
    None,
)
FIXTURE_PAY = next(
    (SCRAPER / "output").rglob("payments_http_23865053.html"),
    None,
)


def test_normalize_endpoint():
    assert normalize_endpoint("https://app.webpt.com/patientChart.php?ID=1") == (
        "/patientchart.php"
    )


def test_parse_extended_chart_fields():
    if FIXTURE_CHART is None or not FIXTURE_CHART.is_file():
        return
    html = FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    info = parse_patient_chart_html(html)
    assert info.diagnosis
    assert info.physician
    assert info.physician_npi == "1245605914"
    assert info.dob
    assert info.insurance


def test_payments_extended_keys():
    if FIXTURE_PAY is None or not FIXTURE_PAY.is_file():
        return
    html = FIXTURE_PAY.read_text(encoding="utf-8", errors="replace")
    txns, totals = parse_patient_payments_html(html)
    assert txns
    assert txns[0].transaction_id or txns[0].payment_type
    assert "total_charge" in totals


def test_edoc_metadata_keys():
    meta = parse_edoc_list_metadata(
        [
            {
                "ExtDocID": 9,
                "URI": "a.pdf",
                "UserDefName": "X",
                "DateFiled": "2026-01-01",
                "Category": "Clinical",
                "Signed": True,
            }
        ]
    )
    assert meta["edoc_meta_count"] == 1
    assert "DateFiled" in meta["keys"]
    assert "Signed" in meta["keys"]


def test_enrich_from_raw_roundtrip(tmp_path: Path):
    if FIXTURE_CHART is None or not FIXTURE_CHART.is_file():
        return
    from case_raw_capture import light_raw_snapshot_from_page_html

    fac, case = "99999", "68908102"
    html = FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    light_raw_snapshot_from_page_html(
        tmp_path,
        facility_id=fac,
        case_id=case,
        patient_id="23863774",
        html=html,
    )
    raw = ensure_raw_layout(tmp_path, fac, case)
    save_text(raw / "patientChart.html", html)
    (raw / "edoc_list.json").write_text("[]", encoding="utf-8")
    (raw / "scheduler.json").write_text("[]", encoding="utf-8")
    row = enrich_case_from_raw(
        tmp_path,
        facility_id=fac,
        case_id=case,
        patient_id="23863774",
        run_ocr=False,
    )
    assert row["diagnosis"]
    assert row["enrich_source"] in {"raw", "staged_raw"}
    assert (tmp_path / "cases" / fac / case / "manifests" / "case_enrich.json").is_file()
    cov = raw_coverage(raw)
    assert cov["required_present"] >= 2


def test_inventory_from_minimal_http(tmp_path: Path):
    log = tmp_path / "http.jsonl"
    log.write_text(
        json.dumps(
            {
                "url": "https://app.webpt.com/patientChart.php?ID=1&CaseID=2",
                "endpoint": "/patientChart.php",
                "method": "GET",
                "status": 200,
                "phase_name": "open_s1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    inv = build_inventory_from_http_log(log)
    assert inv["endpoint_count"] >= 1
    assert inv["endpoints"][0]["currently_parsed_by"] == "patient_chart_api"


def test_promote_gate_blocks_on_queue():
    gate = evaluate_promote_gate(
        store_summary={"queued": 10, "retry_1": 0, "retry_2": 0, "retry_3": 0},
        coverage_report={"coverage_pct": 80.0},
        raw_stats={"cases_with_raw": 5, "mean_raw_coverage_pct": 90.0},
    )
    assert gate["promote_allowed"] is False
    assert gate["checks"]["queue_empty"] is False


def test_coverage_report_structure():
    report = build_coverage_report(sample_enrich_rows=None)
    assert "coverage_pct" in report
    assert report["discoverable_fields"] > 0
    assert any(f["reason"] == "golden_rule_forbidden" for f in report["fields"])
