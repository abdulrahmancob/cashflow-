"""Orchestrate offline enrich stages (+ optional deferred payments). Never touches PDF wave."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENRICH = ROOT / "snowflake_pull" / "scripts" / "run_case_enrich_from_raw.py"
MASTER = ROOT / "snowflake_pull" / "scripts" / "build_webpt_master_report.py"
DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    ap.add_argument("--batch-id", default="case_schedule_202601_202608")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument(
        "--stage",
        default="all",
        help="parse_html|parse_json|payments|ocr|merge|export|all",
    )
    ap.add_argument("--skip-ocr", action="store_true")
    ap.add_argument("--facility-id", default=None)
    ap.add_argument("--refresh-master", action="store_true")
    args = ap.parse_args()

    cmd = [
        sys.executable,
        str(ENRICH),
        "--artifacts",
        str(args.artifacts),
        "--batch-id",
        args.batch_id,
        "--limit",
        str(args.limit),
        "--stage",
        args.stage,
    ]
    if args.skip_ocr:
        cmd.append("--skip-ocr")
    if args.facility_id:
        cmd.extend(["--facility-id", args.facility_id])
    print("RUN", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc
    if args.refresh_master and MASTER.is_file():
        print("RUN", sys.executable, MASTER, flush=True)
        rc = subprocess.call([sys.executable, str(MASTER)])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
