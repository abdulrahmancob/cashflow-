"""Requeue CaseUnits marked downloaded that lack a real artifacts_manifest.csv."""

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

from case_paths import manifest_path  # noqa: E402
from snowflake_pull.case_unit_state import CaseUnitStateStore  # noqa: E402


def requeue_unmanifested(
    store: CaseUnitStateStore,
    out_dir: Path,
    *,
    batch_id: str,
) -> dict:
    units = store.units_in_states(["downloaded"])
    if batch_id:
        units = [u for u in units if u.batch_id == batch_id]

    requeued = 0
    kept = 0
    seen_cases: set[tuple[str, str]] = set()
    for u in units:
        key = (u.facility_id, u.case_id)
        path = manifest_path(out_dir, u.facility_id, u.case_id)
        ok = path.is_file() and path.stat().st_size > 50
        if ok:
            kept += 1
            seen_cases.add(key)
            continue
        store.transition(u.unit_id, "queued", force=True)
        requeued += 1

    stale = store.reclaim_stale_in_progress(1800.0, batch_id=batch_id)
    return {
        "downloaded_scanned": len(units),
        "requeued_units": requeued,
        "kept_with_manifest": kept,
        "stale_reclaimed": stale,
        "counts": store.counts_by_state(batch_id=batch_id),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    ap.add_argument("--batch-id", type=str, default="case_schedule_202601_202608")
    args = ap.parse_args()
    db = args.out_dir / "case_units.sqlite"
    store = CaseUnitStateStore(db)
    try:
        result = requeue_unmanifested(store, args.out_dir, batch_id=args.batch_id)
    finally:
        store.close()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
