#!/usr/bin/env python3
"""Deploy smoke: verify soft-match then run reconcile_from_db."""
from __future__ import annotations

import json
import sys


def main() -> int:
    from cashflow_reconcile.normalize import name_keys_compatible

    ok = name_keys_compatible("ALMONTEREYESIRIS", "ALMONTEIRIS")
    print(f"[softmatch] ALMONTEREYESIRIS~ALMONTEIRIS => {ok}", flush=True)
    if not ok:
        print("[softmatch] FAIL: compound rule not active in image", flush=True)
        return 2

    from cashflow_ops.adapters.reconcile import reconcile_from_db

    print("[reconcile] starting reconcile_from_db...", flush=True)
    result = reconcile_from_db()
    summary = result.get("summary") or {}
    print(json.dumps(summary, indent=2, default=str), flush=True)
    run_id = summary.get("reconciliation_run_id")
    if not run_id:
        print("[reconcile] FAIL: no run_id", flush=True)
        return 1
    print(f"[reconcile] OK run_id={run_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
