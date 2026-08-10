#!/usr/bin/env bash
set -euo pipefail
sudo -n docker stop cashflow-case_drain-1 cashflow-case_ocr-1 || true
echo '=== container integrity ==='
sudo -n docker run --rm -u 10001:10001 -v /data/exports:/data/exports:rw cashflow-scraper:local \
  python -c "import sqlite3; c=sqlite3.connect('/data/exports/side_by_side_case/case_units.sqlite'); print('ctr', c.execute('pragma integrity_check').fetchone()); print('count', c.execute('select count(*) from case_units').fetchone()[0]); c.execute(\"update case_units set updated_at=updated_at where state='in_progress' limit 1\"); c.commit(); print('update_ok')"
echo '=== dump reclaim query test ==='
sudo -n docker run --rm -u 10001:10001 -v /data/exports:/data/exports:rw cashflow-scraper:local \
  python -c "import sqlite3; from datetime import datetime,timezone; c=sqlite3.connect('/data/exports/side_by_side_case/case_units.sqlite'); now=datetime.now(timezone.utc).isoformat(); rows=c.execute(\"select unit_id from case_units where state='in_progress' limit 5\").fetchall(); print('in_progress', rows); 
for (uid,) in rows:
  c.execute('update case_units set state=?, prev_state=?, updated_at=?, in_progress_since=? where unit_id=?', ('queued','in_progress',now,'',uid));
c.commit(); print('reclaim_ok')"
