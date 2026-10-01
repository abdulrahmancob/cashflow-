#!/usr/bin/env bash
# Copy only files added or modified in this push onto the live tree.
# Does not delete server-only files and does not replace secrets.
set -euo pipefail

DEST="${CASHFLOW_DEST:-/opt/cashflow}"
SHA="${DEPLOY_SHA:-${GITHUB_SHA:-}}"
BEFORE="${DEPLOY_BEFORE:-}"
ROOT="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

if [[ -z "${SHA}" ]]; then
  echo "DEPLOY_SHA or GITHUB_SHA is required" >&2
  exit 1
fi

cd "${ROOT}"

if [[ -z "${BEFORE}" || "${BEFORE}" =~ ^0+$ ]] || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
  mapfile -t files < <(git diff-tree --no-commit-id --name-only --diff-filter=AMRT -r "${SHA}")
else
  mapfile -t files < <(git diff --name-only --diff-filter=AMRT "${BEFORE}" "${SHA}")
fi

if [[ "${#files[@]}" -eq 0 ]]; then
  echo "no added or modified files in ${SHA}; nothing to copy"
  exit 0
fi

lock_path="${TMPDIR:-/tmp}/cashflow-deploy.lock"
exec 9>"${lock_path}"
flock 9

portal=0
api=0
rebuild=0
migrate=0
nginx=0
copied=0

skip_path() {
  local rel="$1"
  local base
  base="$(basename "${rel}")"
  case "${rel}" in
    *..*) return 0 ;;
    deploy/.env | .env | */.env | snowflake_pull/keys/*) return 0 ;;
    *.pem | *.key | *.p8 | *.pfx) return 0 ;;
  esac
  case "${base}" in
    .env | credentials.json | storage_state.json) return 0 ;;
    client_secret*.json) return 0 ;;
  esac
  return 1
}

classify() {
  local rel="$1"
  case "${rel}" in
    rcm_portal/src/*|rcm_portal/public/*|rcm_portal/index.html|rcm_portal/package.json|rcm_portal/package-lock.json|rcm_portal/vite.config.*|rcm_portal/tsconfig*.json|rcm_portal/postcss.config.*|rcm_portal/*.css)
      portal=1
      ;;
  esac
  case "${rel}" in
    cashflow_db/sql/*)
      migrate=1
      api=1
      ;;
    deploy/Dockerfile|cashflow_db/requirements.txt|cashflow_ops/requirements.txt|cashflow_forecast/requirements.txt|cashflow_reconcile/requirements.txt)
      api=1
      rebuild=1
      ;;
    cashflow_db/*|cashflow_ops/*|cashflow_forecast/*|cashflow_reconcile/*)
      case "${rel}" in
        */tests/*) ;;
        *) api=1 ;;
      esac
      ;;
    deploy/docker-compose.yml)
      api=1
      ;;
    deploy/nginx/*)
      nginx=1
      ;;
  esac
}

copy_file() {
  local rel="$1"
  local src="${ROOT}/${rel}"
  local dest="${DEST}/${rel}"
  local mode=644
  local gitmode
  if [[ ! -f "${src}" ]]; then
    echo "skip missing ${rel}"
    return
  fi
  gitmode="$(git ls-files -s -- "${rel}" | awk 'NR==1 { print $1 }')"
  if [[ "${gitmode}" == "100755" ]]; then
    mode=755
  fi
  if ! install -D -m "${mode}" "${src}" "${dest}" 2>/dev/null; then
    sudo install -D -m "${mode}" "${src}" "${dest}"
  fi
  copied=$((copied + 1))
  echo "copied ${rel}"
}

for rel in "${files[@]}"; do
  if skip_path "${rel}"; then
    echo "skip protected ${rel}"
    continue
  fi
  copy_file "${rel}"
  if [[ -f "${DEST}/${rel}" ]]; then
    classify "${rel}"
  fi
done

echo "copied ${copied} file(s); portal=${portal} api=${api} rebuild=${rebuild} migrate=${migrate} nginx=${nginx}"

if [[ "${migrate}" -eq 1 ]]; then
  echo "==> apply SQL migrations"
  (
    cd "${DEST}/deploy"
    docker compose --env-file .env --profile tools run --rm worker \
      python -m cashflow_db migrate
  )
fi

if [[ "${rebuild}" -eq 1 ]]; then
  echo "==> rebuild api image"
  (
    cd "${DEST}/deploy"
    GIT_SHA="${SHA}" docker compose --env-file .env build api
  )
fi

if [[ "${api}" -eq 1 ]]; then
  echo "==> recreate api"
  (
    cd "${DEST}/deploy"
    GIT_SHA="${SHA}" docker compose --env-file .env up -d --no-deps --force-recreate api
  )
  ready=0
  for _ in $(seq 1 18); do
    if curl -fsS http://127.0.0.1/ready >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 5
  done
  if [[ "${ready}" -ne 1 ]]; then
    echo "API did not become ready" >&2
    docker ps --format '{{.Names}} {{.Status}}' || true
    exit 1
  fi
  curl -fsS http://127.0.0.1/alive
  echo
  curl -fsS http://127.0.0.1/ready
  echo
fi

if [[ "${portal}" -eq 1 ]]; then
  echo "==> build portal from server tree"
  docker run --rm \
    -v "${DEST}/rcm_portal:/src" \
    -w /src \
    -e npm_config_update_notifier=false \
    node:22-bookworm \
    bash -lc "npm ci && npm run build"
  sudo mkdir -p /data/portal
  sudo rm -rf /data/portal/assets
  sudo cp -a "${DEST}/rcm_portal/dist/index.html" /data/portal/index.html
  sudo cp -a "${DEST}/rcm_portal/dist/assets" /data/portal/assets
  shopt -s nullglob
  for extra in "${DEST}/rcm_portal/dist"/*; do
    if [[ -f "${extra}" ]]; then
      sudo cp -a "${extra}" "/data/portal/$(basename "${extra}")"
    fi
  done
  shopt -u nullglob
  sudo chmod -R a+rX /data/portal
fi

if [[ "${nginx}" -eq 1 ]]; then
  echo "==> recreate nginx"
  compose_file="${DEST}/deploy/docker-compose.yml"
  guard_mount='      - ./nginx/export-guard.js:/etc/nginx/export-guard.js:ro'
  if ! grep -qF 'export-guard.js:' "${compose_file}"; then
    tmp="$(mktemp)"
    awk -v line="${guard_mount}" '
      /cashflow-ssl.conf:\/etc\/nginx\/conf.d\/ssl.conf:ro/ && !done {
        print
        print line
        done=1
        next
      }
      { print }
    ' "${compose_file}" > "${tmp}"
    if ! grep -qF 'export-guard.js:' "${tmp}"; then
      echo "nginx volume anchor missing in ${compose_file}" >&2
      rm -f "${tmp}"
      exit 1
    fi
    if ! mv "${tmp}" "${compose_file}" 2>/dev/null; then
      sudo mv "${tmp}" "${compose_file}"
    fi
  fi
  (
    cd "${DEST}/deploy"
    docker compose --env-file .env up -d --no-deps nginx
    reloaded=0
    for _ in $(seq 1 15); do
      if docker compose --env-file .env exec -T nginx nginx -s reload; then
        reloaded=1
        break
      fi
      sleep 1
    done
    if [[ "${reloaded}" -ne 1 ]]; then
      echo "nginx did not reload" >&2
      exit 1
    fi
  )
  echo "==> export guard"
  guard_ok=0
  for _ in $(seq 1 15); do
    if curl -fsS http://127.0.0.1/export-guard.js >/dev/null && curl -fsS http://127.0.0.1/ | grep -q '/export-guard.js'; then
      guard_ok=1
      break
    fi
    sleep 1
  done
  if [[ "${guard_ok}" -ne 1 ]]; then
    echo "export guard was not injected" >&2
    exit 1
  fi
  code=""
  for _ in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST --max-time 15 'http://127.0.0.1/api/eligibility/items/export-job')"
    if [[ "${code}" == "401" ]]; then
      break
    fi
    sleep 1
  done
  echo "export-job status=${code}"
  if [[ "${code}" != "401" ]]; then
    echo "export-job did not answer 401" >&2
    exit 1
  fi
fi

echo "DEPLOY_RELEASE_DONE sha=${SHA} api_recreated=${api}"
