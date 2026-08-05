"""Phase 3: clinic rediscovery orchestration (Brownsville → Inwood), gate-aware.

Does not scrape unmapped clinics. Online schedule/download is invoked only with
--execute and when P3 pass is true (or --force-after-fail for explicit override).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import finish_run, load_gate, resume_run  # noqa: E402
from snowflake_pull.facility_map import assert_scrape_allowed  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402

CLINIC_ORDER = ("Brownsville", "Inwood")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--clinic", default="Brownsville", choices=list(CLINIC_ORDER))
    p.add_argument("--execute", action="store_true", help="Run export-schedule subprocess")
    p.add_argument("--force-after-fail", action="store_true")
    p.add_argument("--allow-input-drift", action="store_true")
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_clinic_rediscover.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("clinic_rediscover")
    run.obs.online = bool(args.execute)

    mapping = assert_scrape_allowed(args.clinic)
    p3 = load_gate(run.run_dir, "P3")
    p4 = load_gate(run.run_dir, "P4")
    if p4 and p4.get("scrape_allowed") is False and args.clinic in {"Home Care"}:
        raise SystemExit("P4 blocks this clinic")

    if p3 is None:
        raise SystemExit("P3 gate missing; run validate_coverage_hypotheses")
    if p3.get("pass") is not True and not args.force_after_fail:
        plan = {
            "blocked": True,
            "reason": "P3_not_passed",
            "p3": p3,
            "next": p3.get("command_hint"),
            "facility_id": mapping.webpt_facility_id,
            "clinic": args.clinic,
        }
        (run.run_dir / "summaries" / "clinic_rediscover_summary.json").write_text(
            json.dumps(plan, indent=2) + "\n", encoding="utf-8"
        )
        run.obs.emit(
            "decision",
            operation="clinic_rediscover",
            outcome="skip",
            decision="blocked_on_p3",
            decision_reason="P3_not_passed",
            facility_id=mapping.webpt_facility_id,
            facility_name=args.clinic,
        )
        run.obs.stage_end("clinic_rediscover", **plan)
        print(json.dumps(plan, indent=2))
        finish_run(run, status="clinic_rediscover_blocked")
        set_global_obs(None)
        return 2

    out_dir = (
        run.artifacts
        / "clinic_rediscover"
        / f"{mapping.webpt_facility_id}_{args.clinic.replace(' ', '_')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(_REPO / "webpt_edco_scraper" / "scraper.py"),
        "export-schedule",
        "--start-date",
        "2026-06-01",
        "--end-date",
        "2026-07-31",
        "--facility-id",
        str(mapping.webpt_facility_id),
        "--output",
        str(out_dir),
        "--skip-chart",
    ]
    result = {
        "clinic": args.clinic,
        "facility_id": mapping.webpt_facility_id,
        "command": cmd,
        "executed": False,
        "returncode": None,
    }
    if args.execute:
        run.obs.emit(
            "decision",
            operation="export_schedule",
            decision="start_schedule_export",
            facility_id=mapping.webpt_facility_id,
            facility_name=args.clinic,
        )
        proc = subprocess.run(cmd, cwd=str(_REPO / "webpt_edco_scraper"))
        result["executed"] = True
        result["returncode"] = proc.returncode
        if proc.returncode != 0:
            run.obs.emit(
                "error",
                level="ERROR",
                operation="export_schedule",
                outcome="fail",
                error_type="Unexpected",
                error_expected=False,
                decision_reason="schedule_export_failed",
                facility_id=mapping.webpt_facility_id,
            )
        else:
            run.obs.mark_success(
                operation="export_schedule",
                facility_id=mapping.webpt_facility_id,
            )
    else:
        run.obs.emit(
            "decision",
            operation="export_schedule",
            outcome="skip",
            decision="dry_run_command_prepared",
            decision_reason="pass_execute_to_run",
            extra={"command": cmd},
        )

    (run.run_dir / "summaries" / "clinic_rediscover_summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.stage_end("clinic_rediscover", **{k: result[k] for k in result if k != "command"})
    print(json.dumps(result, indent=2))
    finish_run(run, status="clinic_rediscover_done")
    set_global_obs(None)
    return 0 if result.get("returncode") in (None, 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
