#!/usr/bin/env bash
set -euo pipefail
DATA=/data/exports/side_by_side_case
sudo -n docker stop cashflow-case_drain-1 cashflow-case_ocr-1 || true
if [[ -f /tmp/case_units.sqlite ]]; then
  sudo -n cp -a "$DATA/case_units.sqlite" "$DATA/case_units.sqlite.corrupt.$(date +%s)" || true
  sudo -n rm -f "$DATA/case_units.sqlite-wal" "$DATA/case_units.sqlite-shm"
  sudo -n mv /tmp/case_units.sqlite "$DATA/case_units.sqlite"
  sudo -n chown 10001:10001 "$DATA/case_units.sqlite"
fi
sudo -n python3 - <<'PY'
import sqlite3
c=sqlite3.connect("/data/exports/side_by_side_case/case_units.sqlite")
print("integrity", c.execute("PRAGMA integrity_check").fetchone())
print("states", c.execute("SELECT state, COUNT(*) FROM case_units GROUP BY 1 ORDER BY 2 DESC").fetchall())
PY
sudo -n python3 /tmp/requeue_missing_pdfs.py
sudo -n docker start cashflow-case_drain-1 cashflow-case_ocr-1
sleep 25
sudo -n python3 - <<'PY'
import json
h=json.load(open("/data/exports/side_by_side_case/reports/health.json"))
print("auth", h.get("auth_status"), "remaining", h.get("cases_remaining"), "cph", h.get("speed_cases_per_hour"), "case", h.get("current_case"))
PY
sudo -n docker ps --filter name=cashflow-case --format '{{.Names}} {{.Status}}'
