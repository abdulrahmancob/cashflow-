"""Fail-closed QA + edge-case inventory for Case schedule_cases.csv.

Hard-fail (exit 2) on empty case_id or duplicate facility+case+patient+DOS keys.
Edge-case and post-download metrics are inventory-only (do not fail the run).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORENSIC_PATIENT_IDS = frozenset({"52985234", "47856242", "53008686"})
SAMPLE_LIMIT = 50
REOPEN_SPAN_DAYS = 90


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _unit_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        (row.get("facility_id") or "").strip(),
        (row.get("case_id") or "").strip(),
        (row.get("patient_id") or "").strip(),
        (row.get("dos") or "")[:10],
    )


def hard_checks(
    accepted: list[dict[str, str]],
    rejects: list[dict[str, str]],
    summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return hard-check results; pass=False if any rule broken."""
    empty_case = [
        r for r in accepted if not (r.get("case_id") or "").strip()
    ]
    keys: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for r in accepted:
        keys[_unit_key(r)] += 1
    dup_keys = {k: n for k, n in keys.items() if n > 1 and all(k)}

    rejects_missing_reason = [
        r for r in rejects if not (r.get("reject_reason") or "").strip()
    ]
    case_missing_rejects = sum(
        1
        for r in rejects
        if (r.get("reject_reason") or "").strip() == "CaseMissingOnSchedule"
    )
    summary_missing = int((summary or {}).get("case_missing_count") or 0)
    summary_match = summary is None or summary_missing == case_missing_rejects

    input_rows = int((summary or {}).get("input_rows") or 0)
    rejected = int((summary or {}).get("rejected_units") or len(rejects))
    out_of_window = int((summary or {}).get("out_of_window_skipped") or 0)
    accounting_ok = True
    if input_rows:
        # After dedupe, accepted+rejected+out_of_window <= input (dupes collapsed).
        accounting_ok = (
            len(accepted) + rejected + out_of_window
        ) <= input_rows

    dedupe_collapsed = max(
        0, input_rows - len(accepted) - rejected - out_of_window
    ) if input_rows else 0

    failures: list[str] = []
    if empty_case:
        failures.append(f"empty_case_id_in_accepted={len(empty_case)}")
    if dup_keys:
        failures.append(f"duplicate_keys={len(dup_keys)}")
    if rejects_missing_reason:
        failures.append(f"rejects_missing_reason={len(rejects_missing_reason)}")
    if not summary_match:
        failures.append(
            f"case_missing_count mismatch summary={summary_missing} "
            f"rejects={case_missing_rejects}"
        )
    if not accounting_ok:
        failures.append(
            "row_accounting_failed: "
            f"accepted({len(accepted)})+rejected({rejected})+"
            f"out_of_window({out_of_window}) > input({input_rows})"
        )

    return {
        "pass": len(failures) == 0,
        "failures": failures,
        "empty_case_count": len(empty_case),
        "dup_count": len(dup_keys),
        "dup_sample_keys": [
            {"facility_id": k[0], "case_id": k[1], "patient_id": k[2], "dos": k[3], "n": n}
            for k, n in list(dup_keys.items())[:SAMPLE_LIMIT]
        ],
        "rejects_missing_reason": len(rejects_missing_reason),
        "case_missing_rejects": case_missing_rejects,
        "summary_case_missing_match": summary_match,
        "accounting_ok": accounting_ok,
        "dedupe_collapsed": dedupe_collapsed,
        "dedupe_ratio": round(dedupe_collapsed / input_rows, 4) if input_rows else 0.0,
    }


def edge_case_inventory(accepted: list[dict[str, str]]) -> dict[str, Any]:
    """Schedule-level edge cases (offline, no WebPT)."""
    fac_by_patient: dict[str, set[str]] = defaultdict(set)
    case_by_patient: dict[str, set[str]] = defaultdict(set)
    cases_by_patient_dos: dict[tuple[str, str], set[str]] = defaultdict(set)
    cases_by_patient_dos_fac: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    dos_by_case: dict[tuple[str, str], list[str]] = defaultdict(list)
    unit_ids_by_patient_dos: dict[tuple[str, str], list[str]] = defaultdict(list)

    for r in accepted:
        pid = (r.get("patient_id") or "").strip()
        fid = (r.get("facility_id") or "").strip()
        cid = (r.get("case_id") or "").strip()
        dos = (r.get("dos") or "")[:10]
        if not pid:
            continue
        fac_by_patient[pid].add(fid)
        case_by_patient[pid].add(cid)
        if dos:
            cases_by_patient_dos[(pid, dos)].add(cid)
            cases_by_patient_dos_fac[(pid, dos, fid)].add(cid)
            unit_ids_by_patient_dos[(pid, dos)].append(
                r.get("unit_id") or f"{fid}:{cid}:{pid}:{dos}"
            )
        if fid and cid and dos:
            dos_by_case[(fid, cid)].append(dos)

    multi_fac = sorted(p for p, fs in fac_by_patient.items() if len(fs) > 1)
    multi_case = sorted(p for p, cs in case_by_patient.items() if len(cs) > 1)
    multi_case_dos = sorted(
        (pid, dos, sorted(cs))
        for (pid, dos), cs in cases_by_patient_dos.items()
        if len(cs) > 1
    )
    multi_case_dos_fac = sorted(
        (pid, dos, fid, sorted(cs))
        for (pid, dos, fid), cs in cases_by_patient_dos_fac.items()
        if len(cs) > 1
    )

    # CaseReopened inventory: DOS span > 90 days for same facility+case
    reopened: list[dict[str, Any]] = []
    for (fid, cid), dlist in dos_by_case.items():
        dates = sorted({d for d in dlist if len(d) >= 10})
        if len(dates) < 2:
            continue
        try:
            d0 = date.fromisoformat(dates[0])
            d1 = date.fromisoformat(dates[-1])
        except ValueError:
            continue
        span = (d1 - d0).days
        if span > REOPEN_SPAN_DAYS:
            reopened.append(
                {
                    "facility_id": fid,
                    "case_id": cid,
                    "dos_min": dates[0],
                    "dos_max": dates[-1],
                    "span_days": span,
                    "visit_count": len(dlist),
                }
            )
    reopened.sort(key=lambda x: -x["span_days"])

    forensic_hits = [
        {
            "patient_id": pid,
            "facilities": sorted(fac_by_patient[pid]),
            "cases": sorted(case_by_patient[pid]),
            "units": sum(1 for r in accepted if (r.get("patient_id") or "").strip() == pid),
        }
        for pid in sorted(FORENSIC_PATIENT_IDS)
        if pid in fac_by_patient
    ]

    samples: list[dict[str, str]] = []
    for pid in multi_fac[:SAMPLE_LIMIT]:
        samples.append(
            {
                "edge": "multi_facility",
                "patient_id": pid,
                "detail": ",".join(sorted(fac_by_patient[pid])),
                "unit_id": "",
            }
        )
    for pid, dos, cases in multi_case_dos[:SAMPLE_LIMIT]:
        samples.append(
            {
                "edge": "patient_dos_multi_case",
                "patient_id": pid,
                "detail": f"dos={dos};cases={','.join(cases)}",
                "unit_id": ";".join(unit_ids_by_patient_dos[(pid, dos)][:5]),
            }
        )
    for row in reopened[:SAMPLE_LIMIT]:
        samples.append(
            {
                "edge": "case_reopened_span",
                "patient_id": "",
                "detail": (
                    f"facility={row['facility_id']};case={row['case_id']};"
                    f"span_days={row['span_days']}"
                ),
                "unit_id": "",
            }
        )

    return {
        "patients_with_multi_facility": len(multi_fac),
        "patients_with_multi_case": len(multi_case),
        "patient_dos_with_multi_case": len(multi_case_dos),
        "patient_dos_facility_with_multi_case": len(multi_case_dos_fac),
        "cases_reopened_span_gt_90d": len(reopened),
        "forensic_patient_hits": forensic_hits,
        "samples": samples,
        "reopened_sample": reopened[:SAMPLE_LIMIT],
        "multi_case_dos_sample": [
            {
                "patient_id": pid,
                "dos": dos,
                "case_ids": cases,
                "unit_ids": unit_ids_by_patient_dos[(pid, dos)][:5],
            }
            for pid, dos, cases in multi_case_dos[:SAMPLE_LIMIT]
        ],
    }


def post_download_inventory(
    accepted: list[dict[str, str]],
    cases_base: Path | None,
) -> dict[str, Any]:
    """Inventory CaseNoDailyNotes / CaseNoEdocs when cases/ manifests exist."""
    if cases_base is None or not cases_base.is_dir():
        return {
            "cases_dir": "",
            "cases_checked": 0,
            "CaseNoDailyNotes": 0,
            "CaseNoEdocs": 0,
            "note": "cases_dir missing — post-download inventory skipped",
        }

    case_keys = sorted({(r["facility_id"], r["case_id"]) for r in accepted if r.get("case_id")})
    no_dn = 0
    no_edoc = 0
    checked = 0
    samples: list[dict[str, str]] = []

    for fid, cid in case_keys:
        manifest = (
            cases_base / "cases" / fid / cid / "manifests" / "artifacts_manifest.csv"
        )
        if not manifest.is_file():
            # also allow cases_base itself to be the cases/ root
            alt = cases_base / fid / cid / "manifests" / "artifacts_manifest.csv"
            manifest = alt if alt.is_file() else manifest
        if not manifest.is_file():
            continue
        checked += 1
        rows = _read_csv(manifest)
        has_chart = any(
            (r.get("doc_source") or "").strip() == "chart_note" for r in rows
        )
        has_edoc = any((r.get("doc_source") or "").strip() == "edoc" for r in rows)
        if not has_chart:
            no_dn += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(
                    {
                        "edge": "CaseNoDailyNotes",
                        "facility_id": fid,
                        "case_id": cid,
                    }
                )
        if not has_edoc:
            no_edoc += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(
                    {
                        "edge": "CaseNoEdocs",
                        "facility_id": fid,
                        "case_id": cid,
                    }
                )

    return {
        "cases_dir": str(cases_base),
        "cases_checked": checked,
        "CaseNoDailyNotes": no_dn,
        "CaseNoEdocs": no_edoc,
        "samples": samples,
    }


def volume_stats(accepted: list[dict[str, str]], summary: dict[str, Any] | None) -> dict[str, Any]:
    facilities = {r.get("facility_id", "") for r in accepted}
    cases = {(r.get("facility_id", ""), r.get("case_id", "")) for r in accepted}
    patients = {r.get("patient_id", "") for r in accepted}
    return {
        "accepted_units": len(accepted),
        "unique_facilities": len(facilities),
        "unique_facility_case": len(cases),
        "unique_patients": len(patients),
        "input_rows": int((summary or {}).get("input_rows") or 0),
        "rejected_units": int((summary or {}).get("rejected_units") or 0),
        "out_of_window_skipped": int((summary or {}).get("out_of_window_skipped") or 0),
        "reject_counts": (summary or {}).get("reject_counts") or {},
        "case_missing_count": int((summary or {}).get("case_missing_count") or 0),
        "build_elapsed_sec": (summary or {}).get("elapsed_sec"),
        "window_start": (summary or {}).get("window_start"),
        "window_end": (summary or {}).get("window_end"),
    }


def validate_schedule_artifacts(
    schedule_dir: Path,
    *,
    cases_base: Path | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    schedule_path = schedule_dir / "schedule_cases.csv"
    reject_path = schedule_dir / "case_missing_rejects.csv"
    summary_path = schedule_dir / "schedule_build_summary.json"

    accepted = _read_csv(schedule_path)
    rejects = _read_csv(reject_path)
    summary: dict[str, Any] | None = None
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

    hard = hard_checks(accepted, rejects, summary)
    edges = edge_case_inventory(accepted)
    post = post_download_inventory(accepted, cases_base)
    volume = volume_stats(accepted, summary)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schedule_dir": str(schedule_dir),
        "hard_checks": hard,
        "volume": volume,
        "edge_cases": {
            k: v
            for k, v in edges.items()
            if k not in {"samples", "reopened_sample", "multi_case_dos_sample"}
        },
        "edge_case_detail": {
            "multi_case_dos_sample": edges.get("multi_case_dos_sample"),
            "reopened_sample": edges.get("reopened_sample"),
            "forensic_patient_hits": edges.get("forensic_patient_hits"),
        },
        "post_download": post,
        "elapsed_sec": round(time.perf_counter() - t0, 3),
        "pass": hard["pass"],
        "promote_blocked": True,
        "window_note": (
            "Source schedule export may end before 2026-09-30; "
            "do not invent Sep units until export-schedule covers the gap."
        ),
    }
    return report, edges.get("samples") or []


def write_qa_artifacts(
    schedule_dir: Path,
    report: dict[str, Any],
    samples: list[dict[str, str]],
) -> dict[str, Path]:
    schedule_dir.mkdir(parents=True, exist_ok=True)
    json_path = schedule_dir / "schedule_qa_report.json"
    md_path = schedule_dir / "schedule_qa_report.md"
    samples_path = schedule_dir / "edge_case_samples.csv"

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    hard = report["hard_checks"]
    vol = report["volume"]
    edge = report["edge_cases"]
    lines = [
        "# Case Schedule QA Report",
        "",
        f"**Pass:** {report['pass']}",
        f"**Generated:** {report['generated_at']}",
        f"**Elapsed:** {report['elapsed_sec']}s",
        "",
        "## Hard checks",
        "",
        f"- empty_case_count: {hard['empty_case_count']} (must be 0)",
        f"- dup_count: {hard['dup_count']} (must be 0)",
        f"- case_missing_rejects: {hard['case_missing_rejects']}",
        f"- dedupe_collapsed: {hard['dedupe_collapsed']} "
        f"(ratio {hard['dedupe_ratio']})",
        f"- failures: {hard['failures'] or 'none'}",
        "",
        "## Volume",
        "",
        f"- input_rows: {vol['input_rows']}",
        f"- accepted_units: {vol['accepted_units']}",
        f"- unique_facility_case (cases/ fan-out): {vol['unique_facility_case']}",
        f"- unique_patients: {vol['unique_patients']}",
        f"- unique_facilities: {vol['unique_facilities']}",
        f"- rejected_units: {vol['rejected_units']}",
        f"- CaseMissingOnSchedule: {vol['case_missing_count']}",
        f"- build_elapsed_sec: {vol.get('build_elapsed_sec')}",
        f"- window: {vol.get('window_start')} .. {vol.get('window_end')}",
        "",
        "## Edge cases (inventory)",
        "",
        f"- patients_with_multi_facility: {edge['patients_with_multi_facility']}",
        f"- patients_with_multi_case: {edge['patients_with_multi_case']}",
        f"- patient_dos_with_multi_case: {edge['patient_dos_with_multi_case']}",
        f"- patient_dos_facility_with_multi_case: "
        f"{edge['patient_dos_facility_with_multi_case']}",
        f"- cases_reopened_span_gt_90d: {edge['cases_reopened_span_gt_90d']}",
        "",
        "## Post-download",
        "",
        f"- cases_checked: {report['post_download'].get('cases_checked')}",
        f"- CaseNoDailyNotes: {report['post_download'].get('CaseNoDailyNotes')}",
        f"- CaseNoEdocs: {report['post_download'].get('CaseNoEdocs')}",
        "",
        f"_{report.get('window_note', '')}_",
        "",
        "**Promote blocked** until S0–S6 green.",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    fields = ["edge", "patient_id", "detail", "unit_id", "facility_id", "case_id"]
    with samples_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in samples:
            w.writerow({k: row.get(k, "") for k in fields})
        for row in report["post_download"].get("samples") or []:
            w.writerow({k: row.get(k, "") for k in fields})

    return {
        "json": json_path,
        "md": md_path,
        "samples": samples_path,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--schedule-dir",
        type=Path,
        default=ROOT
        / "snowflake_pull"
        / "artifacts"
        / "side_by_side_case"
        / "schedule",
    )
    ap.add_argument(
        "--cases-base",
        type=Path,
        default=None,
        help="Parent of cases/ or cases/ itself for post-download inventory",
    )
    args = ap.parse_args()

    report, samples = validate_schedule_artifacts(
        args.schedule_dir, cases_base=args.cases_base
    )
    paths = write_qa_artifacts(args.schedule_dir, report, samples)
    print(
        json.dumps(
            {
                "pass": report["pass"],
                "hard_checks": report["hard_checks"],
                "volume": report["volume"],
                "edge_cases": report["edge_cases"],
                "paths": {k: str(v) for k, v in paths.items()},
            },
            indent=2,
        )
    )
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
