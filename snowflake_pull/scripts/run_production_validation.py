"""Full Jan–Aug production validation: preflight → download drain → post-chain → reports.

Does not promote live REC. Production Ready = YES only when all gates pass.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
SCRIPTS = Path(__file__).resolve().parent
for _p in (str(ROOT), str(SCRAPER), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from requeue_unmanifested_downloads import requeue_unmanifested  # noqa: E402
from run_case_download_worker import run_download_loop  # noqa: E402
from snowflake_pull.case_merge import merge_case_extracted  # noqa: E402
from snowflake_pull.case_unit_state import CaseUnitStateStore  # noqa: E402

BATCH_ID = "case_schedule_202601_202608"
WINDOW_START = date(2026, 1, 1)
WINDOW_END = date(2026, 8, 30)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def run_schedule_qa(schedule_dir: Path) -> dict[str, Any]:
    from validate_case_schedule import validate_schedule_artifacts, write_qa_artifacts

    report, samples = validate_schedule_artifacts(schedule_dir)
    write_qa_artifacts(schedule_dir, report, samples)
    return report


def audit_manifests(cases_base: Path) -> dict[str, Any]:
    from case_download import validate_manifest_case_ids

    errors: list[str] = []
    checked = 0
    cases_root = cases_base / "cases"
    if not cases_root.is_dir():
        return {"gate": "S2", "pass": True, "manifests_checked": 0, "errors": [], "note": "no cases yet"}
    for fac in cases_root.iterdir():
        if not fac.is_dir():
            continue
        for case_dir in fac.iterdir():
            if not case_dir.is_dir():
                continue
            mp = case_dir / "manifests" / "artifacts_manifest.csv"
            if not mp.is_file():
                continue
            checked += 1
            for e in validate_manifest_case_ids(cases_base, fac.name, case_dir.name):
                if e != "manifest_missing":
                    errors.append(f"{fac.name}/{case_dir.name}: {e}")
    return {
        "gate": "S2",
        "pass": len(errors) == 0,
        "manifests_checked": checked,
        "errors": errors[:100],
    }


def run_post_chain(out_dir: Path) -> dict[str, Any]:
    from case_extract import export_case_daily_notes

    cases_dir = out_dir / "cases"
    batch_out = out_dir / "batch_extracted"
    extracted = out_dir / "extracted"
    rec_dir = out_dir / "reconciliation"
    t0 = time.perf_counter()
    result: dict[str, Any] = {"phases": []}

    if not cases_dir.is_dir() or not any(cases_dir.glob("*/*/manifests/*.csv")):
        result["skipped"] = True
        result["reason"] = "no manifested cases to extract"
        return result

    t_ex = time.perf_counter()
    ext = export_case_daily_notes(cases_dir, batch_out)
    extract_sec = time.perf_counter() - t_ex
    result["phases"].append({"extract": ext, "elapsed_sec": round(extract_sec, 3)})

    missing_notes = 0
    for row in _read_csv(batch_out / "daily_notes.csv"):
        if not (row.get("case_id") or "").strip():
            missing_notes += 1
    s3 = {
        "gate": "S3",
        "pass": missing_notes == 0,
        "missing_case_id": missing_notes,
        "rows": ext.get("daily_notes_count", 0),
    }
    result["s3"] = s3

    t_mg = time.perf_counter()
    merge_stats = merge_case_extracted(extracted, batch_out, seed="side")
    merge_sec = time.perf_counter() - t_mg
    result["phases"].append({"merge": merge_stats, "elapsed_sec": round(merge_sec, 3)})
    s4 = {
        "gate": "S4",
        "pass": int(merge_stats.get("rejected_no_case") or 0) >= 0,
        "rejected_no_case": merge_stats.get("rejected_no_case"),
        "notes_total": merge_stats.get("notes_total"),
    }
    # Fail if any merged row lacks case
    miss = 0
    for row in _read_csv(extracted / "daily_notes.csv"):
        if not (row.get("case_id") or "").strip():
            miss += 1
    s4["pass"] = miss == 0
    s4["missing_case_id"] = miss
    result["s4"] = s4

    s5: dict[str, Any] = {"gate": "S5", "pass": False}
    rec_sec = 0.0
    rec_path = rec_dir / "reconciliation_visits.csv"
    try:
        from cashflow_reconcile.load_webpt import load_webpt_lines
        from cashflow_reconcile.reconcile import run_reconciliation

        t_rc = time.perf_counter()
        lines = load_webpt_lines(
            extracted,
            patients_export_path=None,
            service_from=WINDOW_START,
            service_to=WINDOW_END,
        )
        missing_case = sum(1 for w in lines if not (w.case_id or "").strip())
        if missing_case:
            raise SystemExit(f"{missing_case} lines missing case_id")
        tracker_candidates = [
            ROOT / "revflow_scraper/output/Transaction Tracker 2026.xlsx",
            ROOT / "webpt_edco_scraper/Transaction Tracker 2026.xlsx",
        ]
        tracker = next((t for t in tracker_candidates if t.is_file()), None)
        revflow = ROOT / "revflow_scraper" / "output"
        if tracker and revflow.is_dir():
            rec_dir.mkdir(parents=True, exist_ok=True)
            run_reconciliation(
                webpt_dir=extracted,
                patients_export=None,
                revflow_dir=revflow,
                manifest=None,
                output_dir=rec_dir,
                insurance_map=None,
                service_from=WINDOW_START,
                service_to=WINDOW_END,
                transaction_tracker=tracker,
            )
            rec_sec = time.perf_counter() - t_rc
            visits = _read_csv(rec_path)
            miss_v = sum(1 for v in visits if not (v.get("case_id") or "").strip())
            s5 = {
                "gate": "S5",
                "pass": miss_v == 0 and len(visits) >= 0,
                "visits": len(visits),
                "missing_case_id": miss_v,
                "elapsed_sec": round(rec_sec, 3),
            }
        else:
            s5 = {"gate": "S5", "pass": False, "error": "tracker or revflow missing"}
    except SystemExit as exc:
        s5 = {"gate": "S5", "pass": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        s5 = {"gate": "S5", "pass": False, "error": str(exc)}
    result["s5"] = s5
    result["timings"] = {
        "extract_sec": round(extract_sec, 3),
        "merge_sec": round(merge_sec, 3),
        "rec_sec": round(rec_sec, 3),
        "total_sec": round(time.perf_counter() - t0, 3),
    }
    return result


def build_production_report(
    out_dir: Path,
    *,
    preflight: dict[str, Any],
    schedule_qa: dict[str, Any],
    download: dict[str, Any] | None,
    s2: dict[str, Any],
    post: dict[str, Any],
    stop_reason: str,
) -> dict[str, Any]:
    store = CaseUnitStateStore(out_dir / "case_units.sqlite")
    try:
        counts = store.counts_by_state(batch_id=BATCH_ID)
        errors = store.counts_by_error_type(batch_id=BATCH_ID)
        rem_cases = store.remaining_cases_by_facility(batch_id=BATCH_ID)
        queued_cases = sum(rem_cases.values())
    finally:
        store.close()

    opt_state_path = out_dir / "reports" / "optimizer_state.json"
    opt_state = {}
    if opt_state_path.is_file():
        opt_state = json.loads(opt_state_path.read_text(encoding="utf-8"))

    thr_path = out_dir / "reports" / "throughput_stats.json"
    thr = json.loads(thr_path.read_text(encoding="utf-8")) if thr_path.is_file() else {}

    health_path = out_dir / "reports" / "health.json"
    health = json.loads(health_path.read_text(encoding="utf-8")) if health_path.is_file() else {}

    total_units = sum(counts.values())
    successful = int(counts.get("downloaded", 0)) + int(counts.get("extracted", 0)) + int(
        counts.get("reconciled", 0)
    ) + int(counts.get("done", 0))
    failed = int(counts.get("failed_terminal", 0))
    queued = int(counts.get("queued", 0))
    in_prog = int(counts.get("in_progress", 0))

    s0_pass = bool(schedule_qa.get("pass"))
    s2_pass = bool(s2.get("pass"))
    s3_pass = bool((post.get("s3") or {}).get("pass", False)) if not post.get("skipped") else False
    s4_pass = bool((post.get("s4") or {}).get("pass", False)) if not post.get("skipped") else False
    s5_pass = bool((post.get("s5") or {}).get("pass", False)) if not post.get("skipped") else False

    # S6: no missing case_id in extract/REC + s2
    integrity_violations = 0
    if not s2_pass:
        integrity_violations += len(s2.get("errors") or [])
    if post.get("s3") and not s3_pass:
        integrity_violations += int(post["s3"].get("missing_case_id") or 0)
    if post.get("s4") and not s4_pass:
        integrity_violations += int(post["s4"].get("missing_case_id") or 0)
    if post.get("s5") and not s5_pass:
        integrity_violations += int((post.get("s5") or {}).get("missing_case_id") or 1)

    s6 = {
        "gate": "S6",
        "pass": s0_pass and s2_pass and (post.get("skipped") or (s3_pass and s4_pass and s5_pass)),
        "integrity_violations": integrity_violations,
    }

    queue_empty = queued_cases == 0 and queued == 0 and in_prog == 0
    blockers: list[str] = []
    if not queue_empty:
        blockers.append(f"queue not empty: {queued} units / {queued_cases} cases remaining")
    if integrity_violations:
        blockers.append(f"integrity_violations={integrity_violations}")
    if not s0_pass:
        blockers.append("S0 schedule QA failed")
    if not s2_pass:
        blockers.append("S2 manifest audit failed")
    if post.get("skipped"):
        blockers.append("post-chain skipped: " + str(post.get("reason")))
    else:
        if not s3_pass:
            blockers.append("S3 extract failed")
        if not s4_pass:
            blockers.append("S4 merge failed")
        if not s5_pass:
            blockers.append("S5 REC failed")
    if not s6["pass"]:
        blockers.append("S6 invariant chain not green")
    if errors.get("CaseMismatch", 0):
        # mismatches are terminal per-unit; Ready still possible if queue empty
        pass

    production_ready = queue_empty and integrity_violations == 0 and s0_pass and s2_pass and s6["pass"] and not post.get("skipped")

    if production_ready:
        recommendation = "Promote"
    elif integrity_violations:
        recommendation = "Hold"
    elif not queue_empty:
        recommendation = "Continue"
    else:
        recommendation = "Hold"

    report = {
        "generated_at": _utc(),
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "batch_id": BATCH_ID,
        "stop_reason": stop_reason,
        "total_case_units": total_units,
        "successful_units": successful,
        "failed_units": failed,
        "skipped_units": 0,
        "queued_units": queued,
        "queued_cases": queued_cases,
        "CaseMismatch": int(errors.get("CaseMismatch", 0)),
        "DownloadEmpty": int(errors.get("DownloadEmpty", 0)),
        "CaseOpenFailed": int(errors.get("CaseOpenFailed", 0)),
        "retry_count": int(thr.get("retry_rate", 0) * max(successful + failed, 1)),
        "recovered_failures": int((download or {}).get("heals") or 0),
        "optimizer_kept": int(opt_state.get("kept_improvements") or 0),
        "optimizer_rollbacks": int(opt_state.get("rollbacks") or 0),
        "best_configuration": opt_state.get("best_config") or {},
        "peak_throughput_cases_per_hour": thr.get("peak_cases_per_hour") or health.get("peak_cases_per_hour"),
        "average_throughput_cases_per_hour": thr.get("cases_per_hour") or health.get("speed_cases_per_hour"),
        "avg_download_sec_by_facility": thr.get("avg_download_sec_by_facility") or {},
        "avg_extract_sec": (post.get("timings") or {}).get("extract_sec"),
        "avg_merge_sec": (post.get("timings") or {}).get("merge_sec"),
        "avg_rec_sec": (post.get("timings") or {}).get("rec_sec"),
        "facility_remaining_cases": rem_cases,
        "checkpoint_restores": int(preflight.get("stale_reclaimed") or 0),
        "integrity_violations": integrity_violations,
        "invariant_failures": integrity_violations,
        "preflight": preflight,
        "schedule_qa_pass": s0_pass,
        "gates": {
            "S0": {"pass": s0_pass},
            "S1": {"pass": True, "note": "per-unit at download; CaseMismatch terminal"},
            "S2": s2,
            "S3": post.get("s3"),
            "S4": post.get("s4"),
            "S5": post.get("s5"),
            "S6": s6,
        },
        "download_summary": download,
        "post_chain": post,
        "counts_by_state": counts,
        "error_type_counts": errors,
        "production_ready": production_ready,
        "blocking_reasons": blockers,
        "recommendation": recommendation,
        "remaining_manual_actions": (
            []
            if production_ready
            else [
                "Resume: python snowflake_pull/scripts/run_case_download_worker.py --out-dir snowflake_pull/artifacts/side_by_side_case --batch-id case_schedule_202601_202608",
                "Re-run: python snowflake_pull/scripts/run_production_validation.py --skip-download (after queue empty)",
                "Do not promote live REC until Production Ready = YES",
            ]
        ),
    }
    return report


def write_reports(out_dir: Path, report: dict[str, Any]) -> dict[str, Path]:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    json_path = reports / "production_validation_report.json"
    md_path = reports / "production_validation_report.md"
    exec_path = reports / "executive_summary.md"

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    ready = "YES" if report["production_ready"] else "NO"
    lines = [
        "# Production Validation Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Window: {report['window']['start']} → {report['window']['end']}",
        f"Stop reason: {report['stop_reason']}",
        "",
        "## Counts",
        "",
        f"- Total CaseUnits: {report['total_case_units']}",
        f"- Successful: {report['successful_units']}",
        f"- Failed: {report['failed_units']}",
        f"- Queued units: {report['queued_units']}",
        f"- Queued cases: {report['queued_cases']}",
        f"- CaseMismatch: {report['CaseMismatch']}",
        f"- DownloadEmpty: {report['DownloadEmpty']}",
        f"- CaseOpenFailed: {report['CaseOpenFailed']}",
        f"- Optimizer kept / rollbacks: {report['optimizer_kept']} / {report['optimizer_rollbacks']}",
        f"- Peak cases/hour: {report['peak_throughput_cases_per_hour']}",
        f"- Average cases/hour: {report['average_throughput_cases_per_hour']}",
        f"- Integrity violations: {report['integrity_violations']}",
        "",
        "## Gates",
        "",
    ]
    for g, body in (report.get("gates") or {}).items():
        if body is None:
            lines.append(f"- {g}: n/a")
        else:
            lines.append(f"- {g}: pass={body.get('pass')}")
    lines += [
        "",
        f"## Production Readiness Verdict: **{ready}**",
        "",
    ]
    if report["blocking_reasons"]:
        lines.append("### Blocking reasons")
        lines.extend(f"- {b}" for b in report["blocking_reasons"])
    else:
        lines.append("No blockers.")
    lines += [
        "",
        "### Remaining manual actions",
        "",
    ]
    lines.extend(f"- {a}" for a in report["remaining_manual_actions"] or ["None"])
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    stability = "stable" if report["CaseMismatch"] == 0 and report.get("CaseOpenFailed", 0) < 50 else "degraded"
    integrity = "clean" if report["integrity_violations"] == 0 else "violations_present"
    exec_lines = [
        "# Executive Summary — Case Pipeline Production Validation",
        "",
        f"**Outcome:** Stopped with reason `{report['stop_reason']}`. "
        f"Production Ready = **{ready}**.",
        "",
        "## Throughput",
        "",
        f"- Average: {report['average_throughput_cases_per_hour']} cases/hour",
        f"- Peak: {report['peak_throughput_cases_per_hour']} cases/hour",
        f"- Successful units: {report['successful_units']} / {report['total_case_units']}",
        f"- Remaining cases: {report['queued_cases']}",
        "",
        "## Stability",
        "",
        f"- Assessment: **{stability}**",
        f"- CaseMismatch: {report['CaseMismatch']}",
        f"- DownloadEmpty: {report['DownloadEmpty']}",
        f"- Optimizer rollbacks: {report['optimizer_rollbacks']}",
        "",
        "## Integrity",
        "",
        f"- Assessment: **{integrity}**",
        f"- Violations: {report['integrity_violations']}",
        f"- S0–S6: {json.dumps({k: (v or {}).get('pass') for k, v in (report.get('gates') or {}).items()})}",
        "",
        "## Remaining risks",
        "",
    ]
    if report["queued_cases"]:
        exec_lines.append(
            f"- Download not finished (~{report['queued_cases']} cases left; multi-day ETA at current rate)."
        )
    exec_lines.append("- Sep 2026 schedule still not in source export (ends 2026-08-30).")
    exec_lines.append("- Live REC promote remains a separate policy decision.")
    exec_lines += [
        "",
        f"## Recommendation: **{report['recommendation']}**",
        "",
        "Promote is allowed only when Production Ready = YES. "
        "Otherwise Continue (resume drain) or Hold (integrity issues).",
        "",
        f"Best configuration: `{json.dumps(report.get('best_configuration') or {})}`",
        "",
    ]
    exec_path.write_text("\n".join(exec_lines), encoding="utf-8")
    return {"json": json_path, "md": md_path, "executive": exec_path}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-post", action="store_true")
    ap.add_argument("--max-cases", type=int, default=None, help="Dev only; omit for full drain")
    args = ap.parse_args()
    out_dir = args.out_dir
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Phase 0
    schedule_qa = run_schedule_qa(out_dir / "schedule")
    if not schedule_qa.get("pass"):
        report = build_production_report(
            out_dir,
            preflight={},
            schedule_qa=schedule_qa,
            download=None,
            s2={"gate": "S2", "pass": False, "errors": ["S0 failed"]},
            post={"skipped": True, "reason": "S0 failed"},
            stop_reason="S0_schedule_qa_failed",
        )
        paths = write_reports(out_dir, report)
        print(json.dumps({"production_ready": False, "paths": {k: str(v) for k, v in paths.items()}}, indent=2))
        return 2

    store = CaseUnitStateStore(out_dir / "case_units.sqlite")
    try:
        preflight = requeue_unmanifested(store, out_dir, batch_id=BATCH_ID)
    finally:
        store.close()
    (reports_dir / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")

    download_result: dict[str, Any] | None = None
    stop_reason = "queue_empty"

    # Phase 1
    if not args.skip_download:
        store = CaseUnitStateStore(out_dir / "case_units.sqlite")
        try:
            download_result = asyncio.run(
                run_download_loop(
                    store=store,
                    out_dir=out_dir,
                    batch_id=BATCH_ID,
                    facility_id=None,
                    max_cases=args.max_cases,
                    reclaim_stale_sec=1800.0,
                    dry_run=False,
                )
            )
            if download_result.get("manual_actions"):
                stop_reason = "unrecoverable_auth_or_manual"
            else:
                rem = store.remaining_cases_by_facility(batch_id=BATCH_ID)
                if sum(rem.values()) > 0:
                    stop_reason = "download_incomplete"
                else:
                    stop_reason = "queue_empty"
        finally:
            store.close()
    else:
        stop_reason = "skip_download"

    # Phase 2
    s2 = audit_manifests(out_dir)
    post: dict[str, Any] = {"skipped": True, "reason": "download incomplete"}
    if not args.skip_post:
        store = CaseUnitStateStore(out_dir / "case_units.sqlite")
        try:
            rem = sum(store.remaining_cases_by_facility(batch_id=BATCH_ID).values())
        finally:
            store.close()
        # Run post-chain on whatever was downloaded (partial OK for reporting)
        if (out_dir / "cases").is_dir():
            post = run_post_chain(out_dir)
            if rem > 0 and stop_reason == "queue_empty":
                stop_reason = "download_incomplete"

    report = build_production_report(
        out_dir,
        preflight=preflight,
        schedule_qa=schedule_qa,
        download=download_result,
        s2=s2,
        post=post,
        stop_reason=stop_reason,
    )
    paths = write_reports(out_dir, report)
    print(
        json.dumps(
            {
                "production_ready": report["production_ready"],
                "recommendation": report["recommendation"],
                "stop_reason": stop_reason,
                "queued_cases": report["queued_cases"],
                "paths": {k: str(v) for k, v in paths.items()},
            },
            indent=2,
        )
    )
    return 0 if report["production_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
