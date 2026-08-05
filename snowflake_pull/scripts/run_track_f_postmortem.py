"""Track F postmortem: classify residual note_exists_cpt_missing failures.

Offline only. No Track D, no promote, no re-download.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402

BASE = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
EDOCS = BASE / "edocs"

REASON_LABELS = {
    "classification_false_positive": "Classification false positive",
    "pdf_missing": "PDF missing",
    "unsupported_pdf_layout": "Unsupported PDF layout",
    "no_cpt_in_source_pdf": "No CPT in source PDF",
    "ocr_or_parse_issue": "OCR / parse issue",
    "incomplete_data": "Incomplete data",
}

DIGIT5_RE = re.compile(r"\b(\d{5})\b")
MOD_INLINE_RE = re.compile(r"\b([A-Z]{2})\s*:\s*(\d{5})\b")
CPT_LABEL_RE = re.compile(r"(?:CPT|Code)\s*[:#]?\s*(\d{5})", re.I)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _rec_keys(path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in _read_csv(path):
        pid = (row.get("webpt_patient_id") or "").strip()
        dos = (row.get("date_of_service") or "")[:10]
        if pid and dos:
            keys.add((pid, dos))
    return keys


def _cpt_keys(path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in _read_csv(path):
        pid = (row.get("patient_id") or "").strip()
        dos = (row.get("date_of_daily_note") or "")[:10]
        if pid and dos:
            keys.add((pid, dos))
    return keys


def _find_pdf(emr: str, dos: str, hint: str = "") -> Path | None:
    chart = EDOCS / emr / "chart_notes"
    if hint:
        p = Path(hint)
        if p.is_file():
            return p
        if chart.is_dir():
            cand = chart / Path(hint).name
            if cand.is_file():
                return cand
    if not chart.is_dir():
        return None
    matches = sorted(chart.glob(f"{dos}_DailyNote*.pdf"))
    if matches:
        return matches[0]
    matches = sorted(chart.glob(f"*{dos}*DailyNote*.pdf"))
    if matches:
        return matches[0]
    return None


def _classify_case(
    *,
    emr: str,
    dos: str,
    pdf: Path | None,
    in_rec: bool,
    in_cpt: bool,
) -> dict[str, Any]:
    from chart_notes_parse import (  # noqa: E402
        BARE_CPT_RE,
        MODIFIER_CPT_RE,
        _cpt_section,
        parse_daily_note_cpt,
        pdf_to_plain_text,
    )

    out: dict[str, Any] = {
        "emr_id": emr,
        "date_of_service": dos,
        "pdf_path": str(pdf) if pdf else "",
        "failure_reason": "",
        "has_direct_timed": "no",
        "raw_cpt_section_preview": "",
        "digit5_hits": "0",
        "modifier_hits": "0",
        "parser_cpt_n": "0",
        "text_len": "0",
        "evidence_note": "",
    }

    if in_rec or in_cpt:
        out["failure_reason"] = "classification_false_positive"
        out["evidence_note"] = (
            "already_in_side_rec" if in_rec else "cpt_already_in_side_extract"
        )
        return out

    if pdf is None or not pdf.is_file():
        out["failure_reason"] = "pdf_missing"
        out["evidence_note"] = "no_daily_note_pdf_on_disk"
        return out

    try:
        text = pdf_to_plain_text(pdf)
    except Exception as exc:  # noqa: BLE001
        out["failure_reason"] = "ocr_or_parse_issue"
        out["evidence_note"] = f"pdf_text_extract_error:{exc}"
        return out

    text_len = len(text or "")
    out["text_len"] = str(text_len)
    if text_len < 80:
        out["failure_reason"] = "ocr_or_parse_issue"
        out["evidence_note"] = "empty_or_image_like_pdf_text"
        return out

    section = _cpt_section(text) or ""
    preview = " | ".join(
        ln.strip() for ln in section.splitlines() if ln.strip()
    )[:500]
    out["raw_cpt_section_preview"] = preview

    has_direct = "direct timed" in section.lower()
    out["has_direct_timed"] = "yes" if has_direct else "no"

    digit_hits = DIGIT5_RE.findall(section)
    mod_hits = MODIFIER_CPT_RE.findall(section)  # on full lines via match elsewhere
    mod_inline = MOD_INLINE_RE.findall(section)
    bare_line_hits = [
        ln.strip()
        for ln in section.splitlines()
        if BARE_CPT_RE.match(ln.strip() or "")
    ]
    label_hits = CPT_LABEL_RE.findall(section)
    out["digit5_hits"] = str(len(digit_hits))
    out["modifier_hits"] = str(len(mod_inline) + len(mod_hits))

    try:
        parsed = parse_daily_note_cpt(text)
    except Exception as exc:  # noqa: BLE001
        out["failure_reason"] = "ocr_or_parse_issue"
        out["parser_cpt_n"] = "0"
        out["evidence_note"] = f"parser_exception:{exc}"
        return out

    out["parser_cpt_n"] = str(len(parsed))
    if parsed:
        # Should not happen for residuals, but treat as false positive / stale
        out["failure_reason"] = "classification_false_positive"
        out["evidence_note"] = "parser_now_returns_cpt_stale_gap"
        return out

    # Codes visible in text but parser zero → fixable parse pattern
    codes_visible = bool(bare_line_hits or mod_inline or label_hits)
    if codes_visible and has_direct:
        out["failure_reason"] = "ocr_or_parse_issue"
        out["evidence_note"] = (
            f"codes_in_section_but_parser_zero;"
            f"bare_lines={len(bare_line_hits)};mod_inline={len(mod_inline)};"
            f"label_hits={len(label_hits)}"
        )
        return out

    if codes_visible and not has_direct:
        out["failure_reason"] = "unsupported_pdf_layout"
        out["evidence_note"] = (
            "cpt_like_tokens_outside_direct_timed_layout;"
            f"digit5={len(digit_hits)};label={len(label_hits)}"
        )
        return out

    if not has_direct:
        # Billing sheet / goals / plan chrome without timed CPT block
        # If note CSV says note exists but PDF is not a billing CPT sheet
        out["failure_reason"] = "unsupported_pdf_layout"
        out["evidence_note"] = "no_direct_timed_codes_section"
        return out

    # Has Direct Timed section but no recoverable codes in source
    if has_direct and not digit_hits and not mod_inline and not label_hits:
        out["failure_reason"] = "no_cpt_in_source_pdf"
        out["evidence_note"] = "direct_timed_present_but_no_cpt_tokens"
        return out

    # Ambiguous: section present, some digits but not as CPT lines (e.g. goals 2+/5)
    if has_direct and digit_hits and not bare_line_hits and not mod_inline:
        # Digits are likely MMT grades / zip / visit numbers inside noisy section
        out["failure_reason"] = "no_cpt_in_source_pdf"
        out["evidence_note"] = (
            "direct_timed_or_fallback_section_has_digits_but_not_cpt_lines;"
            f"digit5={len(digit_hits)}"
        )
        return out

    out["failure_reason"] = "incomplete_data"
    out["evidence_note"] = "unclassified_edge;manual_review"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_track_f_postmortem.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("track_f_postmortem")
    run.obs.online = False

    out_dir = run.artifacts / "track_f_postmortem"
    out_dir.mkdir(parents=True, exist_ok=True)

    gap_csv = run.artifacts / "remaining_gap" / "remaining_gap_breakdown.csv"
    track_f = run.artifacts / "track_f"
    recovery_log_path = track_f / "recovery_log.json"
    rejected_path = track_f / "rejected_candidates.csv"

    residuals: dict[tuple[str, str], dict[str, str]] = {}
    for row in _read_csv(gap_csv):
        if (row.get("root_cause") or "") != "note_exists_cpt_missing":
            continue
        emr = (row.get("emr_id") or "").strip()
        dos = (row.get("date_of_service") or "")[:10]
        if emr and dos:
            residuals[(emr, dos)] = row

    # Ensure rejected pdf_missing are included even if reclassified away
    for row in _read_csv(rejected_path):
        emr = (row.get("emr_id") or "").strip()
        dos = (row.get("date_of_service") or "")[:10]
        if emr and dos and (emr, dos) not in residuals:
            residuals[(emr, dos)] = {
                **row,
                "root_cause": "note_exists_cpt_missing",
                "_from": "rejected_candidates",
            }

    # Recovery log hints for PDF paths
    pdf_hints: dict[tuple[str, str], str] = {}
    if recovery_log_path.is_file():
        log = json.loads(recovery_log_path.read_text(encoding="utf-8"))
        for e in log.get("entries") or []:
            key = (
                (e.get("emr_id") or "").strip(),
                (e.get("date_of_service") or "")[:10],
            )
            if key[0] and key[1] and e.get("pdf_path"):
                pdf_hints[key] = e["pdf_path"]

    side_rec = (
        run.side_by_side / "reconciliation" / "reconciliation_visits.csv"
    )
    side_cpt = run.side_by_side / "extracted" / "cpt_codes.csv"
    rec_keys = _rec_keys(side_rec)
    cpt_keys = _cpt_keys(side_cpt)

    cases: list[dict[str, str]] = []
    for (emr, dos), src in sorted(residuals.items()):
        pdf = _find_pdf(emr, dos, pdf_hints.get((emr, dos), ""))
        classified = _classify_case(
            emr=emr,
            dos=dos,
            pdf=pdf,
            in_rec=(emr, dos) in rec_keys,
            in_cpt=(emr, dos) in cpt_keys,
        )
        cases.append(
            {
                **classified,
                "sf_patient": (src.get("sf_patient") or "").strip(),
                "sf_clinic": (src.get("sf_clinic") or "").strip(),
                "failure_reason_label": REASON_LABELS.get(
                    classified["failure_reason"], classified["failure_reason"]
                ),
            }
        )

    fields = [
        "emr_id",
        "date_of_service",
        "sf_patient",
        "sf_clinic",
        "failure_reason",
        "failure_reason_label",
        "pdf_path",
        "has_direct_timed",
        "digit5_hits",
        "modifier_hits",
        "parser_cpt_n",
        "text_len",
        "evidence_note",
        "raw_cpt_section_preview",
    ]
    _write_csv(out_dir / "failure_cases.csv", cases, fields)

    counts = Counter(c["failure_reason"] for c in cases)
    # Ordered display
    order = [
        "unsupported_pdf_layout",
        "no_cpt_in_source_pdf",
        "ocr_or_parse_issue",
        "classification_false_positive",
        "pdf_missing",
        "incomplete_data",
    ]
    summary_rows = []
    for reason in order:
        n = int(counts.get(reason, 0))
        summary_rows.append(
            {
                "failure_reason": reason,
                "label": REASON_LABELS[reason],
                "count": n,
            }
        )
    for reason, n in counts.items():
        if reason not in order:
            summary_rows.append(
                {
                    "failure_reason": reason,
                    "label": REASON_LABELS.get(reason, reason),
                    "count": n,
                }
            )

    ocr_n = int(counts.get("ocr_or_parse_issue", 0))
    unsupported_n = int(counts.get("unsupported_pdf_layout", 0))
    no_cpt_n = int(counts.get("no_cpt_in_source_pdf", 0))
    pdf_miss_n = int(counts.get("pdf_missing", 0))
    fp_n = int(counts.get("classification_false_positive", 0))

    # Decision: F.1 if parse-worthy cluster >= 10 OR dominant single fixable pattern
    recommend_f1 = ocr_n >= 10
    decision = "track_f1_parser_patch" if recommend_f1 else "proceed_track_d"
    rationale = (
        f"ocr_or_parse_issue={ocr_n} >= 10 → narrow parser patch + Track F.1 re-run"
        if recommend_f1
        else (
            f"ocr_or_parse_issue={ocr_n} (<10); "
            f"unsupported_layout={unsupported_n}, no_cpt_in_source={no_cpt_n}, "
            f"pdf_missing={pdf_miss_n}, false_positive={fp_n}. "
            "Residuals are special/one-off — proceed to Track D; "
            "document Track F residual as waived/special."
        )
    )

    summary = {
        "run_id": run.run_id,
        "ts": utc_now_iso(),
        "track": "F_postmortem",
        "residual_count": len(cases),
        "by_failure_reason": {r: int(counts.get(r, 0)) for r in order},
        "counts_raw": dict(counts),
        "decision": decision,
        "recommend_track_f1": recommend_f1,
        "recommend_track_d": not recommend_f1,
        "rationale": rationale,
        "promote_blocked": True,
        "planning_yields_unchanged": True,
        "artifacts_dir": str(out_dir),
    }
    (out_dir / "failure_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    md_lines = [
        "# Track F Postmortem — Failure Summary",
        "",
        f"**Run:** `{run.run_id}`",
        f"**Residual cases:** {len(cases)}",
        "",
        "| Failure Reason | Count |",
        "|---|---:|",
    ]
    for row in summary_rows:
        if row["count"] == 0:
            continue
        md_lines.append(f"| {row['label']} | {row['count']} |")
    md_lines.extend(
        [
            "",
            f"**Decision:** `{decision}`",
            "",
            rationale,
            "",
            f"Detail CSV: `{out_dir / 'failure_cases.csv'}`",
        ]
    )
    (out_dir / "failure_summary.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    if recommend_f1:
        rec_lines = [
            "# Track F Postmortem Recommendation",
            "",
            "## Go: Track F.1 (narrow parser patch)",
            "",
            f"- `ocr_or_parse_issue` count = **{ocr_n}** (threshold ≥ 10).",
            "- Recommend a focused parser fix for the clustered layout, then re-run "
            "Track F recovery on the residual set only.",
            "- Expected: lift yield from ~95.1% toward 99%+ if the cluster shares one pattern.",
            "",
            "Do **not** start Track D until F.1 completes or is explicitly waived.",
            "",
            "- Promote remains blocked.",
            "- Planning yields unchanged.",
        ]
    else:
        rec_lines = [
            "# Track F Postmortem Recommendation",
            "",
            "## Go: Track D (next execution priority)",
            "",
            f"- `ocr_or_parse_issue` = **{ocr_n}** (< 10) — no large fixable parser cluster.",
            f"- Dominant residuals: unsupported layout (**{unsupported_n}**), "
            f"no CPT in source (**{no_cpt_n}**), PDF missing (**{pdf_miss_n}**).",
            "",
            "These look like special/non-standard billing sheets or absent CPT in source, "
            "not a second shared bare-code bug. A general parser change is unlikely to "
            "recover most of the 26.",
            "",
            "## Action",
            "",
            "1. **Proceed to Track D** (`dos_before_first_note`) as planned — first large "
            "data-recovery track (not a parser fix).",
            "2. Document Track F residual (26) as waived/special cases unless a later "
            "manual audit finds a new pattern.",
            "3. C.2 remains deferred; promote remains blocked.",
            "",
            "- Planning yields unchanged.",
        ]
    (out_dir / "recommendation.md").write_text(
        "\n".join(rec_lines) + "\n", encoding="utf-8"
    )

    (run.run_dir / "summaries" / "track_f_postmortem_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    run.obs.stage_end(
        "track_f_postmortem",
        residual_count=len(cases),
        decision=decision,
        ocr_or_parse_issue=ocr_n,
    )
    print(json.dumps(summary, indent=2))
    finish_run(run, status="track_f_postmortem_done")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
