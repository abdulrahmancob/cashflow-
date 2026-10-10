#!/usr/bin/env bash
# Read-only: list tracked files whose server copy matches no version git ever committed.
# These are what the deploy drift guard stops on. Sync them into git before a push that
# touches them, or push with [deploy-overwrite] when replacing them is the intent.
#
# Usage: drift_report.sh [repo checkout] [live tree] [ref]
# The ref defaults to the checkout's HEAD (the last deployed commit). Nothing is fetched,
# so it never touches the runner's working copy.
set -euo pipefail

REPO="${1:-/opt/actions-runner/_work/cashflow-/cashflow-}"
DEST="${2:-/opt/cashflow}"
ref="${3:-HEAD}"
AREAS=(cashflow_db cashflow_ops cashflow_forecast cashflow_reconcile snowflake_pull waystar_scraper revflow_scraper webpt_edco_scraper rcm_portal/src deploy)

normalized_hash() {
  tr -d '\r' | sha256sum | cut -c1-64
}

cd "${REPO}"

drift=0
checked=0
while IFS= read -r rel; do
  live="${DEST}/${rel}"
  [[ -f "${live}" ]] || continue
  checked=$((checked + 1))
  live_hash="$(normalized_hash <"${live}")"
  [[ "${live_hash}" == "$(git cat-file blob "${ref}:${rel}" | normalized_hash)" ]] && continue
  versions="$(git log --format=%H -n 200 "${ref}" -- "${rel}" | while IFS= read -r commit; do
    git cat-file -e "${commit}:${rel}" 2>/dev/null && git cat-file blob "${commit}:${rel}" | normalized_hash
  done)"
  if grep -qx "${live_hash}" <<<"${versions}"; then
    echo "older   ${rel}"
    continue
  fi
  echo "DRIFT   ${rel}"
  drift=$((drift + 1))
done < <(git ls-tree -r --name-only "${ref}" -- "${AREAS[@]}")

echo "checked ${checked} file(s) against ${ref}: ${drift} drifted (older = a committed version, fine)"
