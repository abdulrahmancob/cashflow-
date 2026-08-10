#!/usr/bin/env bash
set -euo pipefail
sudo -n docker stop cashflow-case_drain-1 cashflow-case_ocr-1 || true
sleep 2
echo '=== host files ==='
sudo -n ls -la /data/exports/side_by_side_case/case_units.sqlite*
echo '=== host integrity ==='
sudo -n python3 - <<'PY'
import sqlite3
c=sqlite3.connect('/data/exports/side_by_side_case/case_units.sqlite')
print(c.execute('pragma integrity_check').fetchone())
print('queued', c.execute("select count(distinct case_id) from case_units where state='queued'").fetchone()[0])
PY
echo '=== container integrity ==='
sudo -n docker run --rm -u 10001:10001 -v /data/exports:/data/exports:rw cashflow-scraper:local \
  python - <<'PY'
import sqlite3
c=sqlite3.connect('/data/exports/side_by_side_case/case_units.sqlite')
print('ctr', c.execute('pragma integrity_check').fetchone())
print('count', c.execute('select count(*) from case_units').fetchone()[0])
# try reclaim-like update
from datetime import datetime, timezone
now=datetime.now(timezone.utc).isoformat()
try:
    c.execute("UPDATE case_units SET updated_at=? WHERE state='in_progress' LIMIT 1", (now,))
    c.commit()
    print('update_ok')
except Exception as e:
    print('update_fail', type(e), e)
PY
