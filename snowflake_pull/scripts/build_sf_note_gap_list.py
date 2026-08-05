"""Build subtype-aware SF note gap queues (dry-run by default)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.facility_map import assert_scrape_allowed, map_sf_clinic  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402

PRIORITY = {
    "interior_gap": 10,
    "dos_after_last_note": 20,
    "dos_before_first_note": 30,
    "note_exists_cpt_missing": 5,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument(
        "--enqueue",
        action="store_true",
        help="Enqueue units into sqlite (default: write CSV only)",
    )
    p.add_argument("--include-blank", action="store_true")
    p.add_argument("--max-units", type=int, default=0)
    p.add_argument(
        "--subtype",
        action="append",
        default=[],
        help="Limit to subtype(s); repeatable. Default: all PRIORITY subtypes.",
    )
    args = p.parse_args(argv)
    allowed_subtypes = set(args.subtype) if args.subtype else set(PRIORITY)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="build_sf_note_gap_list.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("gap_list")

    class_csv = run.artifacts / "missing_classification.csv"
    if not class_csv.is_file():
        raise SystemExit("missing_classification.csv not found; run rebuild_root_cause")

    rows_out: list[dict[str, str]] = []
    with class_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("classification") != "patient_in_rec_but_dos_missing":
                continue
            subtype = row.get("subtype") or ""
            status = (row.get("sf_status") or "").strip()
            if status.lower() in {"blank", ""} and not args.include_blank:
                run.obs.emit(
                    "decision",
                    operation="gap_list",
                    outcome="skip",
                    decision="skip_blank_status_deferred",
                    decision_reason="blank_deferred_separate_run",
                    emr_id=(row.get("emr_ids") or "").split(";")[0],
                    dos=row.get("date_of_service"),
                    visit_status=status,
                )
                continue
            if subtype not in PRIORITY or subtype not in allowed_subtypes:
                continue
            clinic = row.get("sf_clinic") or ""
            m = map_sf_clinic(clinic)
            if m.status in {"unmapped", "out_of_scope"}:
                run.obs.emit(
                    "decision",
                    operation="gap_list",
                    outcome="skip",
                    decision="ClinicUnmapped",
                    decision_reason=m.status,
                    facility_name=clinic,
                    error_type="ClinicUnmapped",
                    error_expected=True,
                )
                continue
            emr = (row.get("emr_ids") or "").split(";")[0]
            dos = row.get("date_of_service") or ""
            unit_id = f"{m.webpt_facility_id}:{emr}:{dos}"
            rows_out.append(
                {
                    "unit_id": unit_id,
                    "priority": str(PRIORITY[subtype]),
                    "batch_id": f"gap_{subtype}",
                    "facility_id": m.webpt_facility_id or "",
                    "facility_name": m.webpt_facility_name or clinic,
                    "webpt_patient_id": emr,
                    "emr_id": emr,
                    "dos": dos,
                    "visit_status": status,
                    "patient_name": row.get("sf_patient") or "",
                    "subtype": subtype,
                }
            )

    rows_out.sort(key=lambda r: (int(r["priority"]), r["unit_id"]))
    if args.max_units and args.max_units > 0:
        rows_out = rows_out[: args.max_units]

    out_csv = run.artifacts / "gap_queue.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()) if rows_out else ["unit_id"])
        w.writeheader()
        w.writerows(rows_out)

    enqueued = 0
    if args.enqueue:
        enqueued = run.store.upsert_units(rows_out)

    summary = {
        "queue_rows": len(rows_out),
        "enqueued_new": enqueued,
        "enqueue": bool(args.enqueue),
        "artifact": str(out_csv),
        "by_batch": {},
    }
    from collections import Counter

    summary["by_batch"] = dict(Counter(r["batch_id"] for r in rows_out))
    (run.run_dir / "summaries" / "gap_list_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.stage_end("gap_list", **summary)
    print(json.dumps({"run_id": run.run_id, **summary}, indent=2))
    finish_run(run, status="gap_list_done")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
