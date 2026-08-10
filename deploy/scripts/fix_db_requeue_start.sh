#!/usr/bin/env bash
set -euo pipefail
DATA=/data/exports/side_by_side_case
sudo -n docker stop cashflow-case_drain-1 cashflow-case_ocr-1 || true
sleep 2
sudo -n rm -f "$DATA/case_units.sqlite-wal" "$DATA/case_units.sqlite-shm"
if [[ -f /tmp/case_units.sqlite ]]; then
  sudo -n cp -a "$DATA/case_units.sqlite" "$DATA/case_units.sqlite.bak.$(date +%s)" || true
  sudo -n mv /tmp/case_units.sqlite "$DATA/case_units.sqlite"
fi
sudo -n chown 10001:10001 "$DATA/case_units.sqlite"
sudo -n python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/data/exports/side_by_side_case/case_units.sqlite")
c.execute("PRAGMA journal_mode=DELETE")
print("integrity", c.execute("PRAGMA integrity_check").fetchone())
print("states", c.execute("SELECT state, COUNT(*) FROM case_units GROUP BY 1 ORDER BY 2 DESC").fetchall())
c.close()
PY
sed -i 's/\r$//' /tmp/requeue_missing_pdfs.py
sudo -n python3 /tmp/requeue_missing_pdfs.py
sudo -n python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/data/exports/side_by_side_case/case_units.sqlite")
print("post_integrity", c.execute("PRAGMA integrity_check").fetchone())
print("queued_cases", c.execute("SELECT COUNT(DISTINCT case_id) FROM case_units WHERE state='queued'").fetchone()[0])
print("states", c.execute("SELECT state, COUNT(*) FROM case_units GROUP BY 1 ORDER BY 2 DESC").fetchall())
c.close()
PY
sudo -n chown 10001:10001 "$DATA/case_units.sqlite"
# start drain only first
sudo -n docker start cashflow-case_drain-1
sleep 35
sudo -n docker logs --tail 25 cashflow-case_drain-1
sudo -n python3 /tmp/check_drain_status.py
# OCR later after drain is stable
sudo -n docker start cashflow-case_ocr-1 || true
