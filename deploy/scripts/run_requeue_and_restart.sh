#!/usr/bin/env bash
set -euo pipefail
sudo -n docker stop cashflow-case_drain-1 || true
sudo -n python3 /tmp/requeue_missing_pdfs.py
sudo -n docker start cashflow-case_drain-1
sleep 25
sudo -n python3 - <<'PY'
import json
h=json.load(open("/data/exports/side_by_side_case/reports/health.json"))
print(
    "updated", h.get("updated_at"),
    "auth", h.get("auth_status"),
    "remaining", h.get("cases_remaining"),
    "cph", h.get("speed_cases_per_hour"),
    "case", h.get("current_case"),
)
PY
sudo -n docker ps --filter name=case_drain --format '{{.Names}} {{.Status}}'
