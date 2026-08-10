#!/usr/bin/env python3
"""Merge sample500_extracted into extracted/ (idempotent upsert by case keys)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("/data/exports/side_by_side_case")


def main() -> int:
    sys.path.insert(0, "/app")
    sys.path.insert(0, "/app/snowflake_pull")
    sys.path.insert(0, "/app/webpt_edco_scraper")
    from snowflake_pull.case_merge import merge_case_extracted

    stats = merge_case_extracted(
        ROOT / "extracted", ROOT / "sample500_extracted", seed="side"
    )
    print(json.dumps(stats, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
