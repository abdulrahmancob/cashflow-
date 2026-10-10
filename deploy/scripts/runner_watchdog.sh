#!/usr/bin/env bash
# Restart the GitHub Actions runner when a Deploy run has sat queued while the runner is idle.
# The runner can stay connected yet stop taking jobs (broker socket errors); on 2026-10-09 a
# deploy waited 3.5 hours that way. A runner that is running a job is never restarted, and
# restarts are at least COOLDOWN minutes apart. Runs from runner-watchdog.timer as root.
set -euo pipefail

REPO="${WATCHDOG_REPO:-abdulrahmancob/cashflow-}"
WORKFLOW="${WATCHDOG_WORKFLOW:-Deploy}"
QUEUED_MINUTES="${WATCHDOG_QUEUED_MINUTES:-10}"
COOLDOWN_MINUTES="${WATCHDOG_COOLDOWN_MINUTES:-30}"
STATE="${WATCHDOG_STATE:-/var/tmp/runner_watchdog.last}"
LOG="${WATCHDOG_LOG:-/data/logs/runner_watchdog.log}"

log() {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"${LOG}"
}

# The repository is public, so the runs API answers without a token (60 calls an hour per address).
if ! body="$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/${REPO}/actions/runs?status=queued&per_page=30")"; then
  log "github api unreachable"
  exit 0
fi

age="$(WORKFLOW="${WORKFLOW}" python3 -c '
import json, os, sys
from datetime import datetime, timezone
runs = json.load(sys.stdin).get("workflow_runs", [])
now = datetime.now(timezone.utc)
ages = [
    (now - datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
    for run in runs
    if run.get("name") == os.environ["WORKFLOW"] and run.get("status") == "queued"
]
print(int(max(ages)) if ages else -1)
' <<<"${body}")"

if (( age < QUEUED_MINUTES )); then
  exit 0
fi

if pgrep -f 'Runner.Worker' >/dev/null 2>&1; then
  log "${WORKFLOW} queued ${age}m; the runner is busy with another job, not restarting"
  exit 0
fi

now="$(date +%s)"
last="$(cat "${STATE}" 2>/dev/null || echo 0)"
if (( now - last < COOLDOWN_MINUTES * 60 )); then
  log "${WORKFLOW} queued ${age}m; restarted $(( (now - last) / 60 ))m ago, waiting"
  exit 0
fi

units="$(systemctl list-units --all --plain --no-legend 'actions.runner.*' | awk '{print $1}')"
if [[ -z "${units}" ]]; then
  log "${WORKFLOW} queued ${age}m but no actions.runner unit was found"
  exit 0
fi

echo "${now}" >"${STATE}"
for unit in ${units}; do
  systemctl restart "${unit}"
  log "${WORKFLOW} queued ${age}m with the runner idle; restarted ${unit}"
done
