#!/usr/bin/env bash
set -uo pipefail
echo -n "exports="; find /data/revflow/exports -name '*.csv' 2>/dev/null | wc -l
sudo -n docker ps --format '{{.Names}} {{.Status}}' | head -n 12
tail -n 6 /data/logs/resume_revflow.log 2>/dev/null || true
python3 - <<'PY'
import json
from pathlib import Path
d=json.load(open('/data/exports/side_by_side_case/reports/health.json'))
print('drain', d.get('cases_remaining'), d.get('auth_status'), d.get('speed_cases_per_hour'))
for p in ['/data/revflow/credentials.json','/data/revflow/gmail_token.json','/data/revflow/storage_state.json']:
    print(p, Path(p).is_file())
PY
sudo -n crontab -l | grep -E 'revflow|nightly|sep_' || true
