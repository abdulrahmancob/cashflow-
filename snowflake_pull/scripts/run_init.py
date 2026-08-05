"""Initialize or resume a coverage_fix run workspace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import (  # noqa: E402
    finish_run,
    init_run,
    resume_run,
)
from snowflake_pull.observability import set_global_obs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resume", default="", help="Existing run_id to resume")
    p.add_argument("--operator", default="")
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument("--keep-lock", action="store_true", help="Do not release lock on exit")
    args = p.parse_args(argv)

    if args.resume:
        run = resume_run(
            args.resume,
            root=args.root,
            script="run_init.py",
            allow_input_drift=args.allow_input_drift,
        )
    else:
        run = init_run(root=args.root, operator=args.operator, script="run_init.py")

    set_global_obs(run.obs)
    print(json.dumps({"run_id": run.run_id, "run_dir": str(run.run_dir)}, indent=2))
    # Drill: toy 20-unit enqueue for resume smoke (idempotent)
    run.store.upsert_units(
        [
            {
                "unit_id": f"toy:{i:02d}",
                "priority": i,
                "batch_id": "toy_resume_drill",
                "emr_id": str(1000 + i),
                "dos": "2026-06-01",
                "facility_id": "28029",
            }
            for i in range(20)
        ]
    )
    run.obs.emit(
        "decision",
        operation="toy_units_seeded",
        decision="seed_toy_batch",
        decision_reason="resume_drill_ready",
        extra={"counts": run.store.counts_by_state()},
    )
    run.obs.stage_end("init", counts=run.store.counts_by_state())
    if not args.keep_lock:
        finish_run(run, status="initialized")
        set_global_obs(None)
    else:
        run.obs.stop_heartbeat()
        print("Lock retained; heartbeat stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
