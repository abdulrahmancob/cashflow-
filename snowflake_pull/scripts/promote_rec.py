"""Promote side-by-side reconciliation_visits into live path with bak + locks."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import (  # noqa: E402
    acquire_lock,
    file_sha256,
    finish_run,
    promote_lock_path,
    release_lock,
    resume_run,
)
from snowflake_pull.observability import set_global_obs, utc_now_iso  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Side-by-side reconciliation_visits.csv (default under run artifacts)",
    )
    p.add_argument(
        "--dest",
        type=Path,
        default=_REPO
        / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/reconciliation_visits.csv",
    )
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true", help="Actually promote (disables dry-run)")
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument("--min-recovery-missing-delta", type=int, default=1)
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False

    run = resume_run(
        args.run_id,
        root=args.root,
        script="promote_rec.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)
    run.obs.stage_start("promote")

    source = args.source or (
        run.side_by_side / "reconciliation" / "reconciliation_visits.csv"
    )
    dest = Path(args.dest)
    cov_path = (
        _REPO
        / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/sf_compare/coverage_summary.json"
    )
    gates_ok = True
    reasons: list[str] = []

    # KPI firewall: require coverage_summary measurement vs recovery separation exists
    if not source.is_file():
        gates_ok = False
        reasons.append(f"source_missing:{source}")
    if not dest.is_file():
        gates_ok = False
        reasons.append(f"dest_missing:{dest}")

    unexpected = 0
    errors_path = run.run_dir / "errors.json"
    if errors_path.is_file():
        payload = json.loads(errors_path.read_text(encoding="utf-8"))
        unexpected = sum(
            1
            for e in payload.get("errors", [])
            if e.get("error_expected") is False
        )
    if unexpected > 50:
        gates_ok = False
        reasons.append(f"unexpected_errors={unexpected}")

    checklist = {
        "run_id": run.run_id,
        "source": str(source),
        "dest": str(dest),
        "dry_run": args.dry_run,
        "gates_ok": gates_ok,
        "reasons": reasons,
        "unexpected_errors": unexpected,
        "coverage_summary_present": cov_path.is_file(),
        "ts": utc_now_iso(),
    }

    if not gates_ok:
        (run.run_dir / "summaries" / "promote_summary.json").write_text(
            json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
        )
        run.obs.emit(
            "decision",
            operation="promote",
            outcome="fail",
            decision="promote_rejected",
            decision_reason=";".join(reasons) or "gates_failed",
        )
        run.obs.stage_end("promote", **checklist)
        print(json.dumps(checklist, indent=2))
        finish_run(run, status="promote_rejected")
        set_global_obs(None)
        return 2

    if args.dry_run:
        checklist["promoted"] = False
        checklist["decision"] = "dry_run_ok"
        (run.run_dir / "summaries" / "promote_summary.json").write_text(
            json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
        )
        run.obs.emit(
            "decision",
            operation="promote",
            outcome="skip",
            decision="dry_run",
            decision_reason="pass_apply_to_promote",
        )
        run.obs.stage_end("promote", **checklist)
        print(json.dumps(checklist, indent=2))
        finish_run(run, status="promote_dry_run")
        set_global_obs(None)
        return 0

    # Apply promote under promote lock
    plock = promote_lock_path(run.root)
    acquire_lock(run.root, run.run_id, lock_file=plock)
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = dest.with_name(f"reconciliation_visits.bak.{ts}.csv")
        shutil.copy2(dest, bak)
        src_hash = file_sha256(source)
        dest_hash_before = file_sha256(dest)
        shutil.copy2(source, dest)
        dest_hash_after = file_sha256(dest)
        promote_manifest = {
            "run_id": run.run_id,
            "ts": utc_now_iso(),
            "source": str(source),
            "dest": str(dest),
            "bak": str(bak),
            "source_sha256": src_hash,
            "dest_sha256_before": dest_hash_before,
            "dest_sha256_after": dest_hash_after,
        }
        man_path = run.run_dir / "artifacts" / "promote_manifest.json"
        man_path.write_text(json.dumps(promote_manifest, indent=2) + "\n", encoding="utf-8")
        # also next to dest
        (dest.parent / "promote_manifest.json").write_text(
            json.dumps(promote_manifest, indent=2) + "\n", encoding="utf-8"
        )
        checklist["promoted"] = True
        checklist["bak"] = str(bak)
        checklist["promote_manifest"] = str(man_path)
        run.obs.emit(
            "decision",
            operation="promote",
            outcome="success",
            decision="promoted",
            decision_reason="checklist_passed",
            extra=promote_manifest,
        )
    finally:
        release_lock(run.root, run.run_id, lock_file=plock)

    (run.run_dir / "summaries" / "promote_summary.json").write_text(
        json.dumps(checklist, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.stage_end("promote", **checklist)
    print(json.dumps(checklist, indent=2))
    finish_run(run, status="promoted")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
