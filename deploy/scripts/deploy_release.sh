#!/usr/bin/env bash
# Copy only files added or modified in this push onto the live tree.
# Does not delete server-only files and does not replace secrets.
#
# The api, worker and scrapers bind-mount this tree, so a copied file is live for every
# process that starts afterwards. Before anything is copied:
#   busy guard   waits while a worker or scraper that runs this code is up
#   drift guard  refuses to overwrite a server file whose content git never had
#   pre-flight   stages the server tree plus this push and checks it in the real image
# After the copy, any failure puts the previous files back (EXIT trap) and restarts
# what was restarted. Migrations run in one transaction and are not reverted, so they
# must stay additive: the previous code has to run on the new schema.
#
# Commit message tokens: [deploy-overwrite] allows drifted files to be replaced,
# [deploy-now] skips the busy wait.
set -euo pipefail

DEST="${CASHFLOW_DEST:-/opt/cashflow}"
SHA="${DEPLOY_SHA:-${GITHUB_SHA:-}}"
BEFORE="${DEPLOY_BEFORE:-}"
ROOT="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_ROOT="${DEPLOY_STATE_ROOT:-/data}"
PORTAL_DIR="${DEPLOY_PORTAL_DIR:-/data/portal}"
API_IMAGE="${DEPLOY_API_IMAGE:-cashflow-api:local}"
ROLLBACK_IMAGE="${API_IMAGE%:*}:rollback"
BACKUP_KEEP="${DEPLOY_BACKUP_KEEP:-15}"
BUSY_WAIT_SECONDS="${DEPLOY_BUSY_WAIT_SECONDS:-1800}"
BUSY_STEP_SECONDS="${DEPLOY_BUSY_STEP_SECONDS:-120}"
BUSY_IGNORE="${DEPLOY_BUSY_IGNORE:-deploy/scripts/intake_}"
READY_TRIES="${DEPLOY_READY_TRIES:-18}"
READY_SLEEP="${DEPLOY_READY_SLEEP:-5}"

PY_PACKAGES=(cashflow_db cashflow_ops cashflow_forecast cashflow_reconcile)
MOUNTED_OTHER=(snowflake_pull waystar_scraper revflow_scraper)

if [[ -z "${SHA}" ]]; then
  echo "DEPLOY_SHA or GITHUB_SHA is required" >&2
  exit 1
fi

cd "${ROOT}"

if [[ -z "${BEFORE}" || "${BEFORE}" =~ ^0+$ ]] || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
  BEFORE=""
  mapfile -t files < <(git diff-tree --no-commit-id --name-only --diff-filter=AMRT -r "${SHA}")
  push_messages="$(git log -1 --format=%B "${SHA}")"
else
  mapfile -t files < <(git diff --name-only --diff-filter=AMRT "${BEFORE}" "${SHA}")
  push_messages="$(git log --format=%B "${BEFORE}..${SHA}")"
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

copy_started=0
committed=0
api_recreated=0
image_tagged=0
portal_published=0
nginx_recreated=0
failed_step="start"
STAGE=""
BACKUP=""

has_token() {
  grep -qF "$1" <<<"${push_messages}"
}

ensure_dir() {
  local dir="$1"
  mkdir -p "${dir}" 2>/dev/null || sudo mkdir -p "${dir}"
  if [[ ! -w "${dir}" ]]; then
    sudo chown "$(id -u):$(id -g)" "${dir}"
  fi
}

as_root_if_needed() {
  "$@" 2>/dev/null || sudo "$@"
}

normalized_hash() {
  tr -d '\r' | sha256sum | cut -c1-64
}

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

is_mounted_code() {
  local rel="$1" dir
  for dir in "${PY_PACKAGES[@]}" "${MOUNTED_OTHER[@]}"; do
    [[ "${rel}" == "${dir}/"* ]] && return 0
  done
  return 1
}

is_python_package() {
  local rel="$1" dir
  for dir in "${PY_PACKAGES[@]}"; do
    [[ "${rel}" == "${dir}/"* ]] && return 0
  done
  return 1
}

deployable=()
for rel in "${files[@]}"; do
  if skip_path "${rel}"; then
    echo "skip protected ${rel}"
    continue
  fi
  if [[ ! -f "${ROOT}/${rel}" ]]; then
    echo "skip missing ${rel}"
    continue
  fi
  deployable+=("${rel}")
done

if [[ "${#deployable[@]}" -eq 0 ]]; then
  echo "nothing deployable in ${SHA}"
  exit 0
fi

touches_mounted=0
touches_python=0
for rel in "${deployable[@]}"; do
  is_mounted_code "${rel}" && touches_mounted=1
  is_python_package "${rel}" && touches_python=1
done

# --- busy guard ------------------------------------------------------------------
busy_containers() {
  local name cmd
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    cmd="$(docker inspect -f '{{join .Config.Cmd " "}}' "${name}" 2>/dev/null || true)"
    if [[ -n "${BUSY_IGNORE}" && "${cmd}" =~ ${BUSY_IGNORE} ]]; then
      continue
    fi
    echo "${name}: ${cmd}"
  done < <(docker ps --format '{{.Names}}' | grep -E 'worker|scraper|nightly|case_drain' || true)
}

failed_step="busy guard"
if [[ "${touches_mounted}" -eq 1 ]]; then
  if has_token "[deploy-now]"; then
    echo "busy guard: skipped ([deploy-now])"
  else
    waited=0
    while true; do
      busy="$(busy_containers)"
      if [[ -z "${busy}" ]]; then
        echo "busy guard: clear"
        break
      fi
      if (( waited >= BUSY_WAIT_SECONDS )); then
        echo "busy guard: these still run the mounted code after ${waited}s; nothing was copied:" >&2
        echo "${busy}" >&2
        echo "push again later, or put [deploy-now] in the commit message" >&2
        exit 1
      fi
      echo "busy guard: waiting for running jobs (${waited}s):"
      echo "${busy}"
      sleep "${BUSY_STEP_SECONDS}"
      waited=$((waited + BUSY_STEP_SECONDS))
    done
  fi
fi

# --- drift guard -----------------------------------------------------------------
committed_versions() {
  local rel="$1" commit
  git log --format=%H -n 200 "${SHA}" -- "${rel}" | while IFS= read -r commit; do
    if git cat-file -e "${commit}:${rel}" 2>/dev/null; then
      git cat-file blob "${commit}:${rel}" | normalized_hash
    fi
  done
}

failed_step="drift guard"
drifted=()
for rel in "${deployable[@]}"; do
  live="${DEST}/${rel}"
  [[ -f "${live}" ]] || continue
  live_hash="$(normalized_hash <"${live}")"
  new_hash="$(normalized_hash <"${ROOT}/${rel}")"
  [[ "${live_hash}" == "${new_hash}" ]] && continue
  if [[ -n "${BEFORE}" ]] && git cat-file -e "${BEFORE}:${rel}" 2>/dev/null; then
    [[ "${live_hash}" == "$(git cat-file blob "${BEFORE}:${rel}" | normalized_hash)" ]] && continue
  fi
  # Read into a variable first: `producer | grep -q` fails under pipefail when grep stops early.
  versions="$(committed_versions "${rel}")"
  if grep -qx "${live_hash}" <<<"${versions}"; then
    continue
  fi
  drifted+=("${rel} server=${live_hash:0:12} new=${new_hash:0:12}")
done

if [[ "${#drifted[@]}" -gt 0 ]]; then
  if has_token "[deploy-overwrite]"; then
    echo "drift guard: overwriting server copies git never had ([deploy-overwrite]):"
    printf '  %s\n' "${drifted[@]}"
  else
    echo "drift guard: these server files hold changes git never had; nothing was copied:" >&2
    printf '  %s\n' "${drifted[@]}" >&2
    echo "commit the server copies first, or put [deploy-overwrite] in the commit message" >&2
    exit 1
  fi
else
  echo "drift guard: none"
fi

# --- cleanup and rollback (registered before anything changes) -------------------
restore_file() {
  local rel="$1"
  as_root_if_needed mkdir -p "$(dirname "${DEST}/${rel}")"
  as_root_if_needed cp -a "${BACKUP}/files/${rel}" "${DEST}/${rel}"
}

wait_ready() {
  local _
  for _ in $(seq 1 "${READY_TRIES}"); do
    if curl -fsS http://127.0.0.1/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep "${READY_SLEEP}"
  done
  return 1
}

rollback() {
  set +e
  local problems=() rel
  echo "==> rolling back (failed at: ${failed_step})"
  if [[ -n "${BACKUP}" && -f "${BACKUP}/existing.txt" ]]; then
    while IFS= read -r rel; do
      [[ -z "${rel}" ]] && continue
      restore_file "${rel}" || problems+=("restore ${rel}")
    done <"${BACKUP}/existing.txt"
  fi
  if [[ -n "${BACKUP}" && -f "${BACKUP}/created.txt" ]]; then
    while IFS= read -r rel; do
      [[ -z "${rel}" ]] && continue
      as_root_if_needed rm -f "${DEST}/${rel}" || problems+=("remove ${rel}")
    done <"${BACKUP}/created.txt"
  fi
  if [[ "${image_tagged}" -eq 1 ]]; then
    docker tag "${ROLLBACK_IMAGE}" "${API_IMAGE}" || problems+=("retag ${API_IMAGE}")
  fi
  if [[ "${api_recreated}" -eq 1 ]]; then
    (
      cd "${DEST}/deploy"
      GIT_SHA="${BEFORE:-rollback}" docker compose --env-file .env up -d --no-deps --force-recreate api </dev/null
    ) || problems+=("recreate api")
    wait_ready || problems+=("api ready after rollback")
  fi
  if [[ "${portal_published}" -eq 1 && -d "${PORTAL_DIR}.prev" ]]; then
    as_root_if_needed rm -rf "${PORTAL_DIR}" && as_root_if_needed cp -a "${PORTAL_DIR}.prev" "${PORTAL_DIR}" \
      || problems+=("restore portal")
  fi
  if [[ "${nginx_recreated}" -eq 1 ]]; then
    (
      cd "${DEST}/deploy"
      docker compose --env-file .env up -d --no-deps --force-recreate nginx </dev/null
    ) || problems+=("recreate nginx")
  fi
  if [[ "${#problems[@]}" -gt 0 ]]; then
    echo "ROLLBACK_INCOMPLETE sha=${SHA} backup=${BACKUP}" >&2
    printf '  failed: %s\n' "${problems[@]}" >&2
    echo "  restore by hand: cp -a ${BACKUP}/files/. ${DEST}/ and recreate api/nginx" >&2
  else
    echo "DEPLOY_ROLLED_BACK sha=${SHA} step=${failed_step}" >&2
  fi
}

finish() {
  local code=$?
  if [[ -n "${STAGE}" && -d "${STAGE}" ]]; then
    rm -rf "${STAGE}" 2>/dev/null || sudo rm -rf "${STAGE}"
  fi
  if [[ "${committed}" -eq 1 || "${copy_started}" -eq 0 ]]; then
    exit "${code}"
  fi
  rollback
  exit 1
}
trap finish EXIT

# --- stage and pre-flight ----------------------------------------------------------
if [[ "${touches_python}" -eq 1 ]]; then
  failed_step="pre-flight"
  echo "==> pre-flight on a staged copy"
  ensure_dir "${STATE_ROOT}/deploy-stage"
  STAGE="$(mktemp -d "${STATE_ROOT}/deploy-stage/run.XXXXXX")"
  for dir in "${PY_PACKAGES[@]}"; do
    if [[ -d "${DEST}/${dir}" ]]; then
      tar -C "${DEST}" --exclude=venv --exclude=.venv --exclude=__pycache__ --exclude=node_modules \
        -cf - "${dir}" | tar -C "${STAGE}" -xf -
    fi
  done
  for dir in "${MOUNTED_OTHER[@]}"; do
    [[ -d "${DEST}/${dir}" ]] && ln -s "${DEST}/${dir}" "${STAGE}/${dir}"
  done
  for rel in "${deployable[@]}"; do
    if is_python_package "${rel}"; then
      install -D -m 644 "${ROOT}/${rel}" "${STAGE}/${rel}"
    fi
  done
  chmod -R a+rX "${STAGE}"
  (
    cd "${DEST}/deploy"
    CASHFLOW_SRC="${STAGE}" docker compose --env-file .env --profile tools run --rm --no-deps \
      -e PYTHONDONTWRITEBYTECODE=1 worker python -m cashflow_ops.deploy_preflight --root /app </dev/null
  )
  echo "pre-flight: ok"
  rm -rf "${STAGE}"
  STAGE=""
fi

# --- backup, then copy -----------------------------------------------------------
failed_step="backup"
ensure_dir "${STATE_ROOT}/deploy-backups"
BACKUP="${STATE_ROOT}/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-${SHA:0:7}"
mkdir -p "${BACKUP}/files"
: >"${BACKUP}/existing.txt"
: >"${BACKUP}/created.txt"
for rel in "${deployable[@]}"; do
  if [[ -f "${DEST}/${rel}" ]]; then
    (cd "${DEST}" && cp -a --parents "${rel}" "${BACKUP}/files/")
    echo "${rel}" >>"${BACKUP}/existing.txt"
  else
    echo "${rel}" >>"${BACKUP}/created.txt"
  fi
done
echo "backup: ${BACKUP}"

copy_file() {
  local rel="$1"
  local src="${ROOT}/${rel}"
  local dest="${DEST}/${rel}"
  local mode=644
  local gitmode
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

failed_step="copy"
copy_started=1
for rel in "${deployable[@]}"; do
  copy_file "${rel}"
  classify "${rel}"
done

echo "copied ${copied} file(s); portal=${portal} api=${api} rebuild=${rebuild} migrate=${migrate} nginx=${nginx}"

if [[ "${migrate}" -eq 1 ]]; then
  failed_step="migrations"
  echo "==> apply SQL migrations"
  # migrate() gives up on a lock after a few seconds (a long nightly transaction can hold
  # one), so retry for a while instead of queueing behind it.
  attempts="${MIGRATE_ATTEMPTS:-12}"
  for attempt in $(seq 1 "${attempts}"); do
    if (
      cd "${DEST}/deploy"
      docker compose --env-file .env --profile tools run --rm worker \
        python -m cashflow_db migrate </dev/null
    ); then
      break
    fi
    if [[ "${attempt}" -eq "${attempts}" ]]; then
      echo "migrations still blocked after ${attempts} attempts" >&2
      exit 1
    fi
    echo "migrations blocked (attempt ${attempt}/${attempts}); retrying in 60s"
    sleep 60
  done
fi

if [[ "${rebuild}" -eq 1 ]]; then
  failed_step="image rebuild"
  echo "==> rebuild api image"
  if docker image inspect "${API_IMAGE}" >/dev/null 2>&1; then
    docker tag "${API_IMAGE}" "${ROLLBACK_IMAGE}"
    image_tagged=1
  fi
  (
    cd "${DEST}/deploy"
    GIT_SHA="${SHA}" docker compose --env-file .env build api </dev/null
  )
fi

if [[ "${api}" -eq 1 ]]; then
  failed_step="api"
  echo "==> recreate api"
  api_recreated=1
  (
    cd "${DEST}/deploy"
    GIT_SHA="${SHA}" docker compose --env-file .env up -d --no-deps --force-recreate api </dev/null
  )
  if ! wait_ready; then
    echo "API did not become ready" >&2
    docker ps --format '{{.Names}} {{.Status}}' || true
    exit 1
  fi
  curl -fsS http://127.0.0.1/alive
  echo
  curl -fsS http://127.0.0.1/ready
  echo
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/auth/login)"
  echo "auth login status=${code}"
  if [[ "${code}" == "404" ]]; then
    echo "auth router is not registered (login would 404)" >&2
    exit 1
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/auth/me)"
  echo "auth me without a session status=${code}"
  if [[ "${code}" != "401" ]]; then
    echo "a signed-out /api/auth/me should answer 401" >&2
    exit 1
  fi
fi

if [[ "${portal}" -eq 1 ]]; then
  failed_step="portal"
  echo "==> build portal from server tree"
  docker run --rm \
    -v "${DEST}/rcm_portal:/src" \
    -w /src \
    -e npm_config_update_notifier=false \
    node:22-bookworm \
    bash -lc "npm ci && npm run build" </dev/null
  if [[ -d "${PORTAL_DIR}" ]]; then
    as_root_if_needed rm -rf "${PORTAL_DIR}.prev"
    as_root_if_needed cp -a "${PORTAL_DIR}" "${PORTAL_DIR}.prev"
  fi
  portal_published=1
  sudo mkdir -p "${PORTAL_DIR}"
  sudo rm -rf "${PORTAL_DIR}/assets"
  sudo cp -a "${DEST}/rcm_portal/dist/index.html" "${PORTAL_DIR}/index.html"
  sudo cp -a "${DEST}/rcm_portal/dist/assets" "${PORTAL_DIR}/assets"
  shopt -s nullglob
  for extra in "${DEST}/rcm_portal/dist"/*; do
    if [[ -f "${extra}" ]]; then
      sudo cp -a "${extra}" "${PORTAL_DIR}/$(basename "${extra}")"
    fi
  done
  shopt -u nullglob
  sudo chmod -R a+rX "${PORTAL_DIR}"
  page="$(curl -fsS http://127.0.0.1/)"
  asset="$(grep -oE 'assets/[^"]+\.js' <<<"${page}" | head -1 || true)"
  if [[ -z "${asset}" ]] || ! curl -fsS -o /dev/null "http://127.0.0.1/${asset}"; then
    echo "the published portal does not serve its script (${asset:-none found})" >&2
    exit 1
  fi
  echo "portal: serves ${asset}"
fi

if [[ "${nginx}" -eq 1 ]]; then
  failed_step="nginx"
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
    echo "nginx -t on the new config"
    docker compose --env-file .env run --rm --no-deps --entrypoint nginx nginx -t </dev/null
  )
  nginx_recreated=1
  (
    cd "${DEST}/deploy"
    docker compose --env-file .env up -d --no-deps --force-recreate nginx </dev/null
    reloaded=0
    for _ in $(seq 1 15); do
      if docker compose --env-file .env exec -T nginx nginx -s reload </dev/null; then
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
    if curl -fsS http://127.0.0.1/export-guard.js >/dev/null \
      && home="$(curl -fsS http://127.0.0.1/)" && grep -q '/export-guard.js' <<<"${home}"; then
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

committed=1
mapfile -t old_backups < <(ls -1d "${STATE_ROOT}/deploy-backups"/*/ 2>/dev/null | sort -r | tail -n +"$((BACKUP_KEEP + 1))")
for old in "${old_backups[@]}"; do
  rm -rf "${old}" 2>/dev/null || sudo rm -rf "${old}"
done

echo "DEPLOY_RELEASE_DONE sha=${SHA} api_recreated=${api_recreated} backup=${BACKUP}"
