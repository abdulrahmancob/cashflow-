"""Offline staged enrich from cases/{f}/{c}/raw/ (+ PDFs). No WebPT network."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_artifact_contract import load_audit, update_audit  # noqa: E402
from case_enrich_parse import STAGES, run_stage  # noqa: E402
from case_paths import case_root  # noqa: E402
from case_raw_capture import raw_coverage, raw_dir  # noqa: E402
from snowflake_pull.case_export_aggregate import (  # noqa: E402
    evaluate_promote_gate,
    iter_enrich_rows,
    write_case_export_csv,
    write_promote_gate,
)
from snowflake_pull.webpt_coverage import (  # noqa: E402
    build_coverage_report,
    write_coverage_report,
)

DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"


def _targets(cases_dir: Path, limit: int, facility_id: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in sorted(cases_dir.glob("*/*/raw")):
        if not raw.is_dir():
            continue
        fac = raw.parts[-3]
        case = raw.parts[-2]
        if facility_id and fac != facility_id:
            continue
        out.append((fac, case))
        if len(out) >= limit:
            break
    if out:
        return out
    # Fall back to any case with PDFs / layout
    for manifest in sorted(cases_dir.glob("*/*/manifests/artifacts_manifest.csv")):
        fac = manifest.parts[-4]
        case = manifest.parts[-3]
        if facility_id and fac != facility_id:
            continue
        out.append((fac, case))
        if len(out) >= limit:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    ap.add_argument("--batch-id", default="case_schedule_202601_202608")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--stage", choices=STAGES, default="all")
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--facility-id", default=None)
    ap.add_argument("--case-id", default=None)
    args = ap.parse_args()

    cases_dir = args.artifacts / "cases"
    reports = args.artifacts / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    if args.facility_id and args.case_id:
        targets = [(args.facility_id, args.case_id)]
    else:
        targets = _targets(cases_dir, args.limit, args.facility_id)

    if not targets:
        print("No cases found for enrich.")
        return 0

    sample_rows: list[dict] = []
    raw_pcts: list[float] = []
    for i, (fac, case) in enumerate(targets, 1):
        meta_path = cases_dir / fac / case / "meta.json"
        patient_id = ""
        patient_name = ""
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                patient_id = str(meta.get("patient_id") or "")
                patient_name = str(meta.get("patient_name") or "")
            except json.JSONDecodeError:
                pass
        result = run_stage(
            args.stage,
            args.artifacts,
            facility_id=fac,
            case_id=case,
            patient_id=patient_id,
            patient_name=patient_name,
            run_ocr=not args.skip_ocr,
        )
        enrich = (
            case_root(args.artifacts, fac, case) / "manifests" / "case_enrich.json"
        )
        row = {}
        if enrich.is_file():
            try:
                row = json.loads(enrich.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                row = {}
        if row:
            sample_rows.append(row)
        cov = raw_coverage(raw_dir(args.artifacts, fac, case))
        raw_pcts.append(float(cov.get("coverage_pct") or 0))
        audit = load_audit(case_root(args.artifacts, fac, case))
        print(
            f"[{i}/{len(targets)}] {fac}/{case} stage={args.stage} "
            f"diag={str(row.get('diagnosis', ''))[:40]!r} "
            f"raw={cov.get('coverage_pct')}% "
            f"merge={audit.get('merge_complete')}"
        )
        _ = result

    if args.stage in {"export", "all", "merge"}:
        rows = iter_enrich_rows(cases_dir)
        export_path = reports / f"case_export_{args.batch_id}.csv"
        write_case_export_csv(rows, export_path)
        for fac, case in targets:
            update_audit(
                case_root(args.artifacts, fac, case),
                flag="export_complete",
                value=True,
            )
        print(f"Wrote {export_path} ({len(rows)} rows)")

        mean_raw = sum(raw_pcts) / len(raw_pcts) if raw_pcts else 0.0
        raw_stats = {
            "cases_with_raw": len(raw_pcts),
            "mean_raw_coverage_pct": round(mean_raw, 1),
        }
        best = max(
            sample_rows,
            key=lambda r: (
                1 if r.get("diagnosis") else 0,
                1 if r.get("payments_txn_count") else 0,
                1 if r.get("physician") else 0,
            ),
            default=None,
        )
        cov_report = build_coverage_report(
            inventory_path=reports / "webpt_inventory.json",
            sample_enrich_rows=[best] if best else None,
            raw_sample_stats=raw_stats,
        )
        write_coverage_report(cov_report, reports)

        store_summary = {"queued": 1, "retry_1": 0, "retry_2": 0, "retry_3": 0}
        sqlite_path = args.artifacts / "case_units.sqlite"
        if sqlite_path.is_file():
            import sqlite3

            con = sqlite3.connect(str(sqlite_path))
            for st in ("queued", "retry_1", "retry_2", "retry_3", "downloaded"):
                n = con.execute(
                    "SELECT COUNT(1) FROM case_units WHERE batch_id=? AND state=?",
                    (args.batch_id, st),
                ).fetchone()[0]
                store_summary[st] = int(n)
            con.close()

        gate = evaluate_promote_gate(
            store_summary=store_summary,
            coverage_report=cov_report,
            raw_stats=raw_stats,
        )
        write_promote_gate(gate, reports)
        print(
            f"Coverage {cov_report['coverage_pct']}% | "
            f"promote_allowed={gate['promote_allowed']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
