#!/usr/bin/env bash
# Load test the live portal API with k6, one stage per user count, at a quiet hour.
# Run on the server as a user that can use docker:
#   bash /opt/cashflow/deploy/loadtest/run.sh [stages...]     (default: 50 100 200 400)
# Load test accounts are switched on for the run and off at the end (also on Ctrl-C).
# A stage that fails (errors over 5% or p95 over 2 s) ends the run; Postgres connections
# over 85 also stop it.
set -euo pipefail

DEST="${CASHFLOW_DEST:-/opt/cashflow}"
STAGES=("${@:-50 100 200 400}")
read -r -a STAGES <<<"${STAGES[*]}"
DURATION="${LOADTEST_DURATION:-3m}"
K6_IMAGE="${LOADTEST_K6_IMAGE:-grafana/k6:0.54.0}"
PG_CONN_STOP="${LOADTEST_PG_CONN_STOP:-85}"
OUT="${LOADTEST_OUT:-/data/logs/loadtest}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${OUT}"
chmod 700 "${OUT}"
cd "${DEST}/deploy"

compose() {
  docker compose --env-file .env "$@"
}

pg_connections() {
  docker exec cashflow-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from pg_stat_activity"' </dev/null 2>/dev/null || echo "?"
}

cleanup() {
  [[ -n "${monitor_pid:-}" ]] && kill "${monitor_pid}" 2>/dev/null || true
  compose exec -T api python -m cashflow_ops.loadtest_users deactivate </dev/null || true
  rm -f "${OUT}/tokens.json"
}
trap cleanup EXIT

echo "==> activating load test accounts"
compose exec -T api python -m cashflow_ops.loadtest_users activate --count 50 </dev/null >"${OUT}/tokens.json"
chmod 600 "${OUT}/tokens.json"

network="$(docker inspect cashflow-nginx-1 -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' | awk '{print $1}')"

(
  while true; do
    echo "$(date -u +%H:%M:%S) pg_connections=$(pg_connections) api=$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' cashflow-api-1 2>/dev/null)"
    sleep 10
  done
) >"${OUT}/monitor.log" 2>&1 &
monitor_pid=$!

result=0
for vus in "${STAGES[@]}"; do
  echo "==> stage ${vus} users for ${DURATION}"
  set +e
  docker run --rm --network "${network}" -u "$(id -u):$(id -g)" \
    -v "${DEST}/deploy/loadtest:/scripts:ro" -v "${OUT}:/out" \
    -e BASE=http://nginx -e VUS="${vus}" -e DURATION="${DURATION}" \
    "${K6_IMAGE}" run --quiet --summary-export "/out/stage-${vus}.json" /scripts/k6_portal.js \
    >"${OUT}/stage-${vus}.log" 2>&1 </dev/null
  code=$?
  set -e
  tail -n 30 "${OUT}/stage-${vus}.log" | grep -E 'http_req_duration|http_req_failed|http_reqs|checks|thresholds' || true
  peak="$(grep -o 'pg_connections=[0-9]*' "${OUT}/monitor.log" | cut -d= -f2 | sort -n | tail -1)"
  echo "stage ${vus}: k6 exit=${code} peak_pg_connections=${peak:-?}"
  if [[ "${code}" -ne 0 ]]; then
    echo "stage ${vus} failed its thresholds; stopping"
    result=1
    break
  fi
  if [[ -n "${peak}" && "${peak}" -ge "${PG_CONN_STOP}" ]]; then
    echo "Postgres connections reached ${peak}; stopping"
    result=1
    break
  fi
done

echo "results: ${OUT}"
exit "${result}"
