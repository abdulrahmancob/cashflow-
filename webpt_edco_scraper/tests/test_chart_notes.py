"""Unit tests for chart notes parsing and naming."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chart_notes_api import ChartNoteRef, parse_chart_notes_html
from chart_notes_download import chart_note_filename

SAMPLE_HTML = ROOT.parent / "webpt edco" / "patient_chart_note_sample.html"


def test_parse_sample_html_finds_four_notes() -> None:
    html = SAMPLE_HTML.read_text(encoding="utf-8")
    notes = parse_chart_notes_html(html, case_id=30987198)
    assert len(notes) == 4
    cnsids = {n.cnsid for n in notes if n.cnsid}
    assert cnsids == {"1011269254", "1015342611"}
    uri_notes = [n for n in notes if n.uri]
    assert len(uri_notes) == 2
    assert all("DN" in n.uri for n in uri_notes)


def test_parse_filters_other_case() -> None:
    html = SAMPLE_HTML.read_text(encoding="utf-8")
    notes = parse_chart_notes_html(html, case_id=99999999)
    assert notes == []


def test_parse_dedupes_duplicate_links() -> None:
    html = SAMPLE_HTML.read_text(encoding="utf-8")
    duplicate = html + html
    notes = parse_chart_notes_html(duplicate, case_id=30987198)
    assert len(notes) == 4


def test_chart_note_filenames() -> None:
    html = SAMPLE_HTML.read_text(encoding="utf-8")
    notes = parse_chart_notes_html(html, case_id=30987198)
    names = [chart_note_filename(n) for n in notes]
    assert any("1011269254" in n for n in names)
    assert any("1015342611" in n for n in names)
    assert any("DN1006667478" in n for n in names)
    assert any("DN1018170998" in n for n in names)
    assert all(n.endswith(".pdf") for n in names)


def test_build_print_url_from_ref() -> None:
    note = ChartNoteRef(
        cnsid="1015342611",
        case_id="30987198",
        print_url="https://app.webpt.com/printPDF.php?CNSID=1015342611&CaseID=30987198",
    )
    assert "CNSID=1015342611" in note.print_url
    assert "CaseID=30987198" in note.print_url


if __name__ == "__main__":
    test_parse_sample_html_finds_four_notes()
    test_parse_filters_other_case()
    test_parse_dedupes_duplicate_links()
    test_chart_note_filenames()
    test_build_print_url_from_ref()
    print("All chart notes tests passed.")
