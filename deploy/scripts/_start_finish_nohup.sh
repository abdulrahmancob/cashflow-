#!/usr/bin/env bash
set -euo pipefail
sed -i 's/\r$//' /tmp/run_sf_finish.sh /tmp/insurance.py 2>/dev/null || sed -i 's/\r$//' /tmp/run_sf_finish.sh
sudo cp /tmp/run_sf_finish.sh /opt/cashflow/deploy/scripts/
sudo cp /tmp/insurance.py /opt/cashflow/cashflow_db/repository/insurance.py
sudo chmod +x /opt/cashflow/deploy/scripts/run_sf_finish.sh
# stop leftover waiters
pkill -f '_wait_tail_then_audit|run_sf_audit_forecast_tail|run_audit_billing_mounted' 2>/dev/null || true
nohup bash /opt/cashflow/deploy/scripts/run_sf_finish.sh >/data/logs/sf_finish_nohup.out 2>&1 &
echo "STARTED_PID=$!"
sleep 3
tail -20 /data/logs/sf_audit_forecast_2026-08-10_finish.log || cat /data/logs/sf_finish_nohup.out
