"""Rollback a promoted reconciliation_visits.csv using promote_manifest bak."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from snowflake_pull.coverage_run import acquire_lock, promote_lock_path, release_lock  # noqa: E402
from snowflake_pull.observability import utc_now_iso  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--manifest",
        type=Path,
        default=_REPO
        / "webpt_edco_scraper/output/jun_jul_2026/reconciliation/promote_manifest.json",
    )
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument(
        "--root",
        type=Path,
        default=_REPO / "webpt_edco_scraper/output/jun_jul_2026/coverage_fix",
    )
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False

    if not args.manifest.is_file():
        plan = {
            "ts": utc_now_iso(),
            "dry_run": args.dry_run,
            "rolled_back": False,
            "ok": False,
            "reason": f"manifest not found: {args.manifest}",
        }
        print(json.dumps(plan, indent=2))
        return 0 if args.dry_run else 1
    man = json.loads(args.manifest.read_text(encoding="utf-8"))
    bak = Path(man["bak"])
    dest = Path(man["dest"])
    if not bak.is_file():
        plan = {
            "ts": utc_now_iso(),
            "bak": str(bak),
            "dest": str(dest),
            "dry_run": args.dry_run,
            "rolled_back": False,
            "ok": False,
            "reason": f"bak missing: {bak}",
        }
        print(json.dumps(plan, indent=2))
        return 0 if args.dry_run else 1

    plan = {
        "ts": utc_now_iso(),
        "bak": str(bak),
        "dest": str(dest),
        "dry_run": args.dry_run,
        "ok": True,
    }
    if args.dry_run:
        plan["rolled_back"] = False
        print(json.dumps(plan, indent=2))
        return 0

    run_id = man.get("run_id") or "rollback"
    plock = promote_lock_path(args.root)
    acquire_lock(args.root, run_id, lock_file=plock)
    try:
        shutil.copy2(bak, dest)
        plan["rolled_back"] = True
    finally:
        release_lock(args.root, run_id, lock_file=plock)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
