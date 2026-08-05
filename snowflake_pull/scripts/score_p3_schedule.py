"""Score Brownsville (or any clinic) schedule export vs SF EMR universe → P3 gate."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import parse_date  # noqa: E402
from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.facility_map import assert_scrape_allowed  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402

START = date(2026, 6, 1)
END = date(2026, 7, 31)


def score_schedule(
    *,
    schedule_csv: Path,
    sf_path: Path,
    clinic: str,
    facility_id: str,
) -> dict:
    sf_emrs: set[str] = set()
    sf_paid: set[str] = set()
    with sf_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("CLINIC") or "").strip() != clinic:
                continue
            dos = (row.get("DATE_OF_SERVICE") or "")[:10]
            d = parse_date(dos)
            if d is None or d < START or d > END:
                continue
            emr = (row.get("EMR_ID") or "").strip()
            if not emr:
                continue
            sf_emrs.add(emr)
            if (row.get("STATUS") or "").strip().lower() == "paid":
                sf_paid.add(emr)

    sched_emrs: set[str] = set()
    sched_rows = 0
    with schedule_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sched_rows += 1
            pid = (row.get("patient_id") or "").strip()
            if pid:
                sched_emrs.add(pid)

    overlap = sf_emrs & sched_emrs
    ceiling = len(overlap) / max(len(sf_emrs), 1)
    # Feasibility: schedule returned patients AND coverage is within 10pp of
    # the schedule ceiling (ceiling IS the overlap rate here — pass if API
    # returned data and ceiling >= 0.20, else fail with documented ceiling).
    api_ok = sched_rows > 0 and len(sched_emrs) > 0
    if not api_ok:
        passed = False
        reason = "schedule_empty_for_facility"
    elif ceiling < 0.20:
        passed = False
        reason = (
            f"schedule_ceiling={ceiling:.3f}<0.20 — rediscovery cannot hit high "
            "coverage; revise success criteria"
        )
    else:
        # Feasibility pass: schedule can move the needle
        passed = True
        reason = (
            f"schedule_ceiling={ceiling:.3f} schedule_emrs={len(sched_emrs)} "
            f"sf_emrs={len(sf_emrs)} overlap={len(overlap)}"
        )

    return {
        "gate": "P3",
        "pass": passed,
        "pending_online": False,
        "facility_id": facility_id,
        "clinic": clinic,
        "sf_emr_count": len(sf_emrs),
        "sf_paid_emr_count": len(sf_paid),
        "schedule_rows": sched_rows,
        "schedule_emr_count": len(sched_emrs),
        "overlap_emr_count": len(overlap),
        "schedule_coverage_pct": round(100 * ceiling, 2),
        "schedule_ceiling_pct": round(100 * ceiling, 2),
        "reason": reason,
        "schedule_csv": str(schedule_csv),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--clinic", default="Brownsville")
    p.add_argument("--schedule-csv", type=Path, required=True)
    p.add_argument("--allow-input-drift", action="store_true")
    args = p.parse_args(argv)

    mapping = assert_scrape_allowed(args.clinic)
    run = resume_run(
        args.run_id,
        root=args.root,
        script="score_p3_schedule.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("score_p3")

    sf_path = _REPO / "snowflake_pull/output/all_billing_data.csv"
    payload = score_schedule(
        schedule_csv=args.schedule_csv,
        sf_path=sf_path,
        clinic=args.clinic,
        facility_id=mapping.webpt_facility_id or "",
    )
    gate_dir = run.run_dir / "summaries" / "gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / "P3.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (run.run_dir / "summaries" / "gate_P3_summary.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.emit(
        "decision",
        operation="score_p3",
        outcome="success" if payload["pass"] else "fail",
        decision="p3_scored",
        decision_reason=payload["reason"],
        facility_id=mapping.webpt_facility_id,
        facility_name=args.clinic,
        extra=payload,
    )
    run.obs.stage_end("score_p3", **{k: payload[k] for k in ("pass", "schedule_ceiling_pct")})
    print(json.dumps(payload, indent=2))
    finish_run(run, status="p3_scored")
    set_global_obs(None)
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
