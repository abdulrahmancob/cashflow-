# API + worker image (no Playwright / Chromium).
# Runs as non-root UID 10001.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app \
    HOME=/home/cashflow

RUN groupadd --gid 10001 cashflow \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/cashflow cashflow \
    && mkdir -p /app /data /tmp \
    && chown -R cashflow:cashflow /app /data /home/cashflow

WORKDIR /app

# System deps for psycopg / openpyxl / yaml builds (minimal)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Dependency layers
COPY cashflow_db/requirements.txt /tmp/req-db.txt
COPY cashflow_ops/requirements.txt /tmp/req-ops.txt
COPY cashflow_forecast/requirements.txt /tmp/req-fc.txt
COPY cashflow_reconcile/requirements.txt /tmp/req-rc.txt

RUN pip install --upgrade pip \
    && pip install \
        -r /tmp/req-db.txt \
        -r /tmp/req-ops.txt \
        -r /tmp/req-fc.txt \
        -r /tmp/req-rc.txt \
        psycopg[binary]>=3.2 \
        fastapi>=0.110 \
        uvicorn>=0.27 \
        python-multipart>=0.0.9

# Application source (packages only — no secrets)
COPY cashflow_db ./cashflow_db
COPY cashflow_ops ./cashflow_ops
COPY cashflow_forecast ./cashflow_forecast
COPY cashflow_reconcile ./cashflow_reconcile
COPY schemas ./schemas
COPY docs ./docs
COPY pyproject.toml ./pyproject.toml

RUN chown -R cashflow:cashflow /app

USER 10001:10001

EXPOSE 8787

# Default: API. Worker overrides command in compose.
CMD ["uvicorn", "cashflow_forecast.api:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]
