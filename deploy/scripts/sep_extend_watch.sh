#!/usr/bin/env bash
# Hourly watch: when case_drain remaining <= 500, run Sep schedule extension once.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLAG=/data/logs/sep_extend.done
LOCK=/data/logs/sep_extend.lock
HEALTH=/data/exports/side_by_side_case/reports/health.json

[[ -f "${FLAG}" ]] && exit 0
remaining="$(python3 - <<PY
import json
from pathlib import Path
p=Path("${HEALTH}")
print(json.load(p.open()).get("cases_remaining", 999999) if p.exists() else 999999)
PY
)"
if [[ "${remaining}" -gt 500 ]]; then
  exit 0
fi

exec 9>"${LOCK}"
flock -n 9 || exit 0
echo "[sep-watch] $(date -Is) remaining=${remaining} starting extend"
bash "${ROOT}/scripts/extend_schedule_sep.sh" && date -Is > "${FLAG}"
echo "[sep-watch] $(date -Is) done"
