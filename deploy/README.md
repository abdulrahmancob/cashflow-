# RCM Platform — Production Deployment (Minimal & Secure)

**This directory prepares the repository for deployment.**  
Deployment against a live server is a **separate phase**.

Do **not** treat the scripts here as “already ran on the server.”

## Architecture

```text
Internet → nginx :80/:443 → api :8787 → postgres (internal only)

Host cron 02:00 Africa/Cairo
  → docker compose --profile tools run --rm scraper
  → docker compose --profile tools run --rm worker
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

Target reference host: Ubuntu 24.04, e.g. `147.93.138.73` (12 vCPU / 47GB / 348GB).

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
0 2 * * * /opt/cashflow/deploy/scripts/nightly_pipeline.sh >> /data/logs/nightly.log 2>&1
30 3 * * * /opt/cashflow/deploy/scripts/backup.sh >> /data/logs/backup.log 2>&1
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

Kubernetes, Swarm, CI/CD, Prometheus, Grafana, ELK, Loki, Redis, RabbitMQ, multi-node, autoscaling.
