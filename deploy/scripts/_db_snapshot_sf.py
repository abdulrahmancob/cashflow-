#!/usr/bin/env python3
from cashflow_db.repository import connection

with connection() as conn:
    kpi = conn.execute("SELECT COUNT(*) AS n FROM analytics.snowflake_visit_kpi").fetchone()
    print("kpi", kpi["n"])
    runs = conn.execute(
        """
        SELECT reconciliation_run_id::text AS id, status, created_at, finished_at, row_count, notes
        FROM billing.reconciliation_run
        ORDER BY created_at DESC
        LIMIT 5
        """
    ).fetchall()
    print("recon_runs")
    for r in runs:
        print(dict(r))
    lines = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM billing.reconciliation_line rl
        JOIN billing.reconciliation_run rr ON rr.reconciliation_run_id = rl.reconciliation_run_id
        WHERE rr.status = 'success'
          AND rr.created_at = (SELECT MAX(created_at) FROM billing.reconciliation_run WHERE status = 'success')
        """
    ).fetchone()
    print("latest_success_lines", lines["n"])
    fc = conn.execute(
        """
        SELECT forecast_run_id::text AS id, status, created_at, as_of_date
        FROM analytics.forecast_run
        ORDER BY created_at DESC
        LIMIT 5
        """
    ).fetchall()
    print("forecast_runs")
    for r in fc:
        print(dict(r))
