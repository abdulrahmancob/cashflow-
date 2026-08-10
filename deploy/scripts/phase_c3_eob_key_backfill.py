#!/usr/bin/env python3
"""C3: backfill billing.eob_check.eob_key from RevFlow manifest.selection (or reload path)."""
from __future__ import annotations

import json
from pathlib import Path

from cashflow_db.loaders.load_revflow import _manifest_index

REPORT = Path("/data/exports/side_by_side_case/reports/phase_c3_eob_key_dq.json")
REVFLOW = Path("/data/revflow")


def main() -> int:
    from cashflow_db.db import connect

    manifest = _manifest_index(REVFLOW)
    # filename -> eob_key
    by_file: dict[str, str] = {}
    for key, item in manifest.items():
        if not isinstance(item, dict):
            continue
        eob = item.get("eob_key")
        path = item.get("path") or item.get("filename") or item.get("export_path")
        if eob and path:
            by_file[Path(str(path)).name] = str(eob)
        # index also keyed by eob_key itself — skip those for file map
        if eob and key.endswith(".csv"):
            by_file[key] = str(eob)

    print(f"[c3] manifest file->eob_key entries: {len(by_file)}", flush=True)

    with connect() as conn:
        before = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE eob_key IS NULL) AS nulls
            FROM billing.eob_check
            """
        ).fetchone()
        updated = 0
        unmatched = 0
        rows = conn.execute(
            """
            SELECT eob_check_id::text, source_file, eob_key
            FROM billing.eob_check
            WHERE eob_key IS NULL AND source_file IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            eob = by_file.get(Path(str(row["source_file"])).name)
            if not eob:
                unmatched += 1
                continue
            conn.execute(
                """
                UPDATE billing.eob_check
                SET eob_key = %s
                WHERE eob_check_id = %s::uuid
                  AND eob_key IS NULL
                """,
                (eob, row["eob_check_id"]),
            )
            updated += 1

        after = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE eob_key IS NULL) AS nulls,
              COUNT(DISTINCT eob_key) AS distinct_keys
            FROM billing.eob_check
            """
        ).fetchone()
        alloc = conn.execute(
            "SELECT COUNT(*) AS n FROM billing.deposit_check_allocation"
        ).fetchone()["n"]
        checks = int(after["total"])

    null_rate_before = round(int(before["nulls"]) / max(int(before["total"]), 1), 4)
    null_rate_after = round(int(after["nulls"]) / max(checks, 1), 4)
    gate = {
        "manifest_file_keys": len(by_file),
        "updated": updated,
        "unmatched_source_files": unmatched,
        "eob_check_total": checks,
        "null_rate_before": null_rate_before,
        "null_rate_after": null_rate_after,
        "distinct_eob_keys": int(after["distinct_keys"]),
        "deposit_check_allocation": int(alloc),
        "pass_null_rate_dropped": null_rate_after < null_rate_before,
        "pass_null_rate_lt_0_05": null_rate_after < 0.05,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(gate, indent=2), encoding="utf-8")
    print(json.dumps(gate, indent=2), flush=True)
    ok = gate["pass_null_rate_dropped"] and gate["pass_null_rate_lt_0_05"]
    print(f"[c3] DQ_PASS={ok}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
