#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

h = json.loads(Path("/data/exports/side_by_side_case/reports/health.json").read_text())
print(
    "updated",
    h.get("updated_at"),
    "auth",
    h.get("auth_status"),
    "remaining",
    h.get("cases_remaining"),
    "cph",
    h.get("speed_cases_per_hour"),
    "case",
    h.get("current_case"),
    "throttle",
    h.get("throttle_state"),
)
subprocess.run(["docker", "ps", "--filter", "name=cashflow-case", "--format", "{{.Names}} {{.Status}}"], check=False)
