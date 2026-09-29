# RCM Platform — Production Deployment (Minimal & Secure)

**Pushing to `main` deploys the changed files onto the live server.**  
The runbook further down is the one-time host setup. It is not how day-to-day code ships.

## Deploy from GitHub

A push to `main` starts [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) on the self-hosted runner `cashflow-forecast` (`i-0f96ccadee3d5df9e`). [`deploy/scripts/deploy_release.sh`](scripts/deploy_release.sh) copies only files added or modified in that push onto `/opt/cashflow`.

| Change | What runs |
|--------|-----------|
| Python under `cashflow_db`, `cashflow_ops`, `cashflow_forecast`, or `cashflow_reconcile` | Recreate the `api` container, then check `http://127.0.0.1/ready` |
| `deploy/Dockerfile` or an API `requirements.txt` | Rebuild the API image, then recreate `api` |
| `rcm_portal` source, `package.json`, or the Vite config | `npm ci && npm run build` on the server tree, then copy `dist` to `/data/portal` |
| `cashflow_db/sql/` | `python -m cashflow_db migrate`, then recreate `api` |
| `deploy/nginx/` | Reload nginx |
| Scripts, docs, or tests only | Copy the files. Leave the running stack alone |

The portal build uses the server's `rcm_portal` tree after the new files are copied, so pages that exist only on the server stay in the build.

What a push does not do:

- It does not delete a file that exists only on the server.
- It does not replace `deploy/.env`, Snowflake keys, or session files.
- It does not restart Postgres, the worker, the scraper, or case drain.

Other branches are not deployed. Watch the result under the repo's Actions tab. The job log ends with `DEPLOY_RELEASE_DONE` and `api_recreated=0` or `1`.

## Architecture

```text
Internet → nginx :80/:443 → api :8787 → postgres (internal only)

Host cron 02:00 Africa/Cairo
  → docker compose --profile tools run --rm scraper
  → docker compose --profile tools run --rm worker
Host cron 18:00 Africa/Cairo
  → docker compose --profile tools run --rm scraper  (note-catchup, last 7 days)
```

| Service | Image | Role |
|---------|-------|------|
| `postgres` | `postgres:16` | DB — **no published port** |
| `api` | `deploy/Dockerfile` | uvicorn Forecast + Ops API (non-root, read-only FS) |
| `worker` | same as api | `cashflow_ops` / migrate / reconcile / forecast (no browsers) |
| `scraper` | `deploy/Dockerfile.scraper` | Playwright + Tesseract + scrapers |
| `nginx` | `nginx:1.27` | Reverse proxy — only public ports **80/443** |

Orchestration stays in `cashflow_ops`. Host cron only triggers containers.

## Files

```text
deploy/
  Dockerfile
  Dockerfile.scraper
  docker-compose.yml
  nginx/nginx.conf
  nginx/cashflow.conf
  nginx/cashflow-ssl.conf.example
  .env.example
  bootstrap_host.sh          # host prep — run only in Deployment phase
  scripts/backup.sh
  scripts/nightly_pipeline.sh
  scripts/note_catchup.sh
  scripts/deploy_release.sh   # copies a main push onto /opt/cashflow
  README.md
```

## Security (PHI)

- Containers run as UID **10001**
- API: `read_only`, tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`
- Postgres never binds host `:5432`
- Secrets only in server-side `deploy/.env` (never commit)
- Docker json-file logs rotated (`50m` × 5)
- UFW / SSH harden steps documented in `bootstrap_host.sh` (not executed here)

## Health endpoints

| Path | Meaning |
|------|---------|
| `GET /alive` | Process up |
| `GET /ready` | Postgres + repository (HTTP 503 if not ready) |
| `GET /api/v1/platform` | Platform status |

Compose healthcheck uses `/ready`.

## Storage mapping

Host layout (created by bootstrap later):

```text
/data/postgres
/data/backups
/data/webpt
/data/revflow
/data/waystar
/data/ocr
/data/exports
/data/logs
/data/certs
```

Env vars inside containers point at `/data/...` (see `docker-compose.yml`).

---

## Deployment runbook (execute later — not now)

Target reference host: Ubuntu 24.04 (12 vCPU / 47GB / 348GB). Open a shell with:

```bash
aws ssm start-session --profile cashflow --target i-0f96ccadee3d5df9e
```

### 1) Bootstrap host

```bash
sudo bash deploy/bootstrap_host.sh
# sets TZ Africa/Cairo, /data/*, Docker, UFW 22/80/443, swap
```

### 2) Place code + secrets

```bash
# e.g. /opt/cashflow = git clone / rsync of this repo
cd /opt/cashflow/deploy
cp .env.example .env
# edit .env — strong POSTGRES_PASSWORD, scraper credentials, webhook
```

### 3) Start always-on stack

```bash
docker compose --env-file .env up -d --build postgres api nginx
```

### 4) Migrate

```bash
docker compose --env-file .env --profile tools run --rm worker \
  python -m cashflow_db migrate
```

### 5) Verify

```bash
curl -fsS http://127.0.0.1/alive
curl -fsS http://127.0.0.1/ready
curl -fsS http://127.0.0.1/api/v1/platform
```

### 6) Dry-run pipeline

```bash
docker compose --env-file .env --profile tools run --rm worker \
  python -m cashflow_ops run --dry-run --skip-scrapers --trigger manual
```

### 7) Nightly scheduler (host cron)

Ensure host TZ is `Africa/Cairo`, then:

```cron
0 2 * * * bash /opt/cashflow/deploy/scripts/nightly_pipeline.sh >> /data/logs/nightly.log 2>&1
30 3 * * * bash /opt/cashflow/deploy/scripts/backup.sh >> /data/logs/backup.log 2>&1
0 18 * * * /opt/cashflow/deploy/scripts/note_catchup.sh >> /data/logs/note_catchup.log 2>&1
```

Install the nightly job once (abdu crontab only — do not also install it as root):

```bash
chmod +x /opt/cashflow/deploy/scripts/nightly_pipeline.sh
sed -i 's/\r$//' /opt/cashflow/deploy/scripts/nightly_pipeline.sh
```

### 8) TLS (optional)

1. Put `fullchain.pem` + `privkey.pem` in `/data/certs/`
2. Enable `nginx/cashflow-ssl.conf.example` (see comments in file)
3. `docker compose restart nginx`

### 9) SSH hardening (after key login confirmed)

See notes printed by `bootstrap_host.sh`: disable password auth, disable root login.

---

## Local build smoke (developer machine — optional)

```bash
cd deploy
docker compose build api
# do not require a live server
```

## Out of scope

Kubernetes, Swarm, Prometheus, Grafana, ELK, Loki, Redis, RabbitMQ, multi-node, autoscaling.
