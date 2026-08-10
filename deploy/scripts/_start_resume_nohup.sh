#!/usr/bin/env bash
set -euo pipefail
sed -i 's/\r$//' /tmp/run_sf_audit_forecast_resume.sh /tmp/_db_snapshot_sf.py || true
sudo cp /tmp/run_sf_audit_forecast_resume.sh /opt/cashflow/deploy/scripts/
sudo cp /tmp/_db_snapshot_sf.py /opt/cashflow/deploy/scripts/
sudo chmod +x /opt/cashflow/deploy/scripts/run_sf_audit_forecast_resume.sh

cd /opt/cashflow/deploy
docker compose --env-file .env --profile tools run --rm -T -e PYTHONPATH=/app -w /app \
  -v /opt/cashflow/deploy/scripts/_db_snapshot_sf.py:/tmp/_db_snapshot_sf.py:ro \
  worker python -c "exec(open('/tmp/_db_snapshot_sf.py').read())" || true

# Kill stale wait loops
pkill -f '_poll_sf_audit_forecast' 2>/dev/null || true
pkill -f 'run_sf_audit_forecast.sh' 2>/dev/null || true

nohup bash /opt/cashflow/deploy/scripts/run_sf_audit_forecast_resume.sh \
  >/data/logs/sf_audit_forecast_resume_nohup.out 2>&1 &
echo "STARTED_PID=$!"
sleep 2
tail -20 /data/logs/sf_audit_forecast_2026-08-10_resume.log 2>/dev/null || \
  tail -20 /data/logs/sf_audit_forecast_resume_nohup.out
