"""Tests for Clean Case Full Capture v2 contract + staged enrich."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_artifact_contract import (  # noqa: E402
    clean_html,
    file_sha256,
    flatten_provenance,
    load_audit,
    provenance_field,
    update_audit,
)
from case_enrich_parse import run_stage, stage_parse_html  # noqa: E402
from case_paths import MANIFEST_FIELDNAMES  # noqa: E402
from case_payments_stage import (  # noqa: E402
    build_payments_summary,
    store_payments_from_json,
)
from case_raw_capture import light_raw_snapshot_from_page_html, raw_coverage  # noqa: E402
from patient_chart_api import parse_patient_chart_html  # noqa: E402


FIXTURE_CHART = next(
    (SCRAPER / "output").rglob("patient_chart_23863774_68908102.html"),
    None,
)


def test_manifest_has_sha256_columns():
    assert "sha256" in MANIFEST_FIELDNAMES
    assert "size" in MANIFEST_FIELDNAMES


def test_clean_html_strips_scripts():
    raw = "<html><script>alert(1)</script><body>Hi</body></html>"
    assert "alert" not in clean_html(raw)
    assert "Hi" in clean_html(raw)


def test_provenance_flatten():
    flat = flatten_provenance(
        {"diagnosis": provenance_field("M51.26", source="patientChart", confidence=1.0)}
    )
    assert flat["diagnosis"] == "M51.26"
    assert flat["diagnosis_source"] == "patientChart"
    assert flat["diagnosis_confidence"] == 1.0


def test_light_snapshot_and_audit(tmp_path: Path):
    html = "<html><body><strong>Diagnosis:</strong></body></html>"
    if FIXTURE_CHART and FIXTURE_CHART.is_file():
        html = FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    result = light_raw_snapshot_from_page_html(
        tmp_path,
        facility_id="21535",
        case_id="68908102",
        patient_id="23863774",
        html=html,
        page_url="https://app.webpt.com/patientChartNote.php?ID=1&CaseID=68908102",
    )
    assert result["coverage"]["required_present"] >= 2
    case_dir = tmp_path / "cases" / "21535" / "68908102"
    assert (case_dir / "raw" / "chart_notes.html").is_file()
    assert (case_dir / "raw" / "chart_notes.cleaned.html").is_file()
    assert (case_dir / "raw" / "request_log.json").is_file()
    assert (case_dir / "case_sources.json").is_file()
    audit = load_audit(case_dir)
    assert audit.get("raw_snapshot_complete") is True


def test_payments_summary_and_layout(tmp_path: Path):
    txns = [
        {
            "dateOfService": "01/15/2026",
            "dateOfTransaction": "01/15/2026",
            "type": "Copay",
            "description": "Office Visit Copay",
            "amountDue": 25,
            "amountPaid": 25,
            "paidMethodType": "Cash",
            "transactionId": 1,
            "caseId": 1,
            "facilityId": 21535,
            "patientId": 9,
        }
    ]
    out = store_payments_from_json(
        tmp_path, facility_id="21535", case_id="1", transactions=txns
    )
    pdir = tmp_path / "cases" / "21535" / "1" / "payments"
    assert (pdir / "transactions.csv").is_file()
    assert (pdir / "summary.json").is_file()
    assert (pdir / "payments.json").is_file()
    assert out["summary"]["Payments"] == 25.0
    summary = build_payments_summary([])
    assert "Charges" in summary


def test_staged_parse_html(tmp_path: Path):
    if FIXTURE_CHART is None or not FIXTURE_CHART.is_file():
        return
    html = FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    light_raw_snapshot_from_page_html(
        tmp_path,
        facility_id="21535",
        case_id="68908102",
        patient_id="23863774",
        html=html,
    )
    # Also store as patientChart for parse_html preference
    raw = tmp_path / "cases" / "21535" / "68908102" / "raw"
    (raw / "patientChart.html").write_text(html, encoding="utf-8")
    (raw / "patientChart.cleaned.html").write_text(clean_html(html), encoding="utf-8")
    result = stage_parse_html(tmp_path, facility_id="21535", case_id="68908102")
    assert result["fields"] > 0
    chart = json.loads(
        (tmp_path / "cases/21535/68908102/parsed/chart_fields.json").read_text(
            encoding="utf-8"
        )
    )
    assert chart.get("diagnosis", {}).get("value")
    assert chart["diagnosis"]["source"] == "patientChart"


def test_run_stage_merge_without_ocr(tmp_path: Path):
    if FIXTURE_CHART is None or not FIXTURE_CHART.is_file():
        return
    html = FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    light_raw_snapshot_from_page_html(
        tmp_path,
        facility_id="21535",
        case_id="68908102",
        patient_id="23863774",
        html=html,
    )
    raw = tmp_path / "cases" / "21535" / "68908102" / "raw"
    (raw / "patientChart.html").write_text(html, encoding="utf-8")
    (raw / "edoc_list.json").write_text(
        json.dumps([{"ExtDocID": 1, "URI": "a.pdf", "DateFiled": "2026-01-01"}]),
        encoding="utf-8",
    )
    (raw / "scheduler.json").write_text(
        json.dumps(
            [
                {
                    "p_id": 23863774,
                    "case_id": 68908102,
                    "title": "DOE, JANE - 01/01/1990 - (Default)",
                    "start_date": "2026-01-15 10:00:00",
                    "status": 4,
                    "checkin_time": "x",
                    "checkout_time": "y",
                }
            ]
        ),
        encoding="utf-8",
    )
    run_stage(
        "all",
        tmp_path,
        facility_id="21535",
        case_id="68908102",
        patient_id="23863774",
        run_ocr=False,
    )
    enrich = tmp_path / "cases/21535/68908102/manifests/case_enrich.json"
    assert enrich.is_file()
    data = json.loads(enrich.read_text(encoding="utf-8"))
    assert data.get("diagnosis")
    assert data.get("diagnosis_source") == "patientChart"
    audit = load_audit(tmp_path / "cases/21535/68908102")
    assert audit.get("parse_html_complete")
    assert audit.get("merge_complete")


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    digest = file_sha256(p)
    assert len(digest) == 64


def test_parse_chart_still_works():
    if FIXTURE_CHART is None or not FIXTURE_CHART.is_file():
        return
    info = parse_patient_chart_html(
        FIXTURE_CHART.read_text(encoding="utf-8", errors="replace")
    )
    assert info.diagnosis
    assert info.physician_npi
