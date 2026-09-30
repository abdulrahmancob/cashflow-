"""Chart-list kwargs for note catch-up. The scraper image may not accept not_before."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_note_catchup.py"
    spec = importlib.util.spec_from_file_location("run_note_catchup", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chart_list_extra_omits_unknown_not_before() -> None:
    module = _load()
    assert module.chart_list_extra({"patient_id", "prefer_http"}, "2026-01-01") == {}


def test_chart_list_extra_passes_not_before_when_supported() -> None:
    module = _load()
    assert module.chart_list_extra(
        {"patient_id", "not_before"}, "2026-01-01"
    ) == {"not_before": "2026-01-01"}


def test_chart_list_extra_skips_blank_cutoff() -> None:
    module = _load()
    assert module.chart_list_extra({"not_before"}, None) == {}
    assert module.chart_list_extra({"not_before"}, "") == {}


def test_select_cases_keeps_only_named_clinics() -> None:
    module = _load()
    cases = [
        {"facility_id": "200", "patient_id": "2", "case_id": "b", "dos": {"2026-02-01"}},
        {"facility_id": "100", "patient_id": "1", "case_id": "a", "dos": {"2026-01-01"}},
        {"facility_id": "300", "patient_id": "3", "case_id": "c", "dos": {"2026-03-01"}},
    ]
    chosen = module.select_cases(cases, 0, 3, ["300", "100"])
    assert [case["facility_id"] for case in chosen] == ["100", "300"]
