#!/usr/bin/env bash
# If an IP registration URL is dropped into /data/revflow/pending_ip_link.txt,
# open it from Contabo (egress IP) via Playwright.
set -uo pipefail
FILE=/data/revflow/pending_ip_link.txt
[[ -f "${FILE}" ]] || exit 0
URL="$(python3 - <<'PY'
from pathlib import Path
import re
text=Path('/data/revflow/pending_ip_link.txt').read_text(encoding='utf-8', errors='replace')
m=re.search(r'https://billing\.revflow\.com/ipRegistration\?\?[a-f0-9]+', text, re.I)
print(m.group(0) if m else '')
PY
)"
[[ -n "${URL}" ]] || exit 0
LOCK=/data/logs/revflow_ip_link.lock
exec 9>"${LOCK}"
flock -n 9 || exit 0
echo "[ip-watch] $(date -Is) opening ${URL}"
cd /opt/cashflow/deploy
docker compose --env-file .env --profile tools run --rm --no-deps \
  -e REVFLOW_HEADLESS=true \
  -v /opt/cashflow/deploy/scripts/revflow_open_ip_link.py:/tmp/revflow_open_ip_link.py:ro \
  scraper python /tmp/revflow_open_ip_link.py "${URL}" \
  && rm -f "${FILE}" \
  && echo "[ip-watch] $(date -Is) done"
