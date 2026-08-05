#!/usr/bin/env bash
# Host bootstrap for RCM Platform (Ubuntu 24.04).
#
# DOCUMENTATION / PREPARED SCRIPT ONLY during Deployment Preparation.
# Do NOT run this against a server until the Deployment phase is approved.
#
# Intended later usage (on server, as sudo-capable user):
#   sudo bash deploy/bootstrap_host.sh
#
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/data}"
SWAP_GB="${SWAP_GB:-8}"
TZ_NAME="${TZ_NAME:-Africa/Cairo}"

echo "==> Timezone → ${TZ_NAME}"
timedatectl set-timezone "${TZ_NAME}"

echo "==> Create ${DATA_ROOT} layout"
mkdir -p \
  "${DATA_ROOT}/postgres" \
  "${DATA_ROOT}/backups" \
  "${DATA_ROOT}/webpt/legacy" \
  "${DATA_ROOT}/revflow" \
  "${DATA_ROOT}/waystar" \
  "${DATA_ROOT}/ocr" \
  "${DATA_ROOT}/exports/side_by_side_case" \
  "${DATA_ROOT}/exports/snowflake" \
  "${DATA_ROOT}/exports/ops" \
  "${DATA_ROOT}/logs/nginx" \
  "${DATA_ROOT}/certs"
# App containers run as UID 10001
chown -R 10001:10001 \
  "${DATA_ROOT}/webpt" \
  "${DATA_ROOT}/revflow" \
  "${DATA_ROOT}/waystar" \
  "${DATA_ROOT}/ocr" \
  "${DATA_ROOT}/exports" \
  "${DATA_ROOT}/logs" \
  "${DATA_ROOT}/backups" || true

echo "==> Swap (${SWAP_GB}G) if missing"
if ! swapon --show | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024))
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
else
  echo "    swap already active"
fi

echo "==> Install Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# Allow deploy user to run docker (adjust USERNAME when running)
DEPLOY_USER="${SUDO_USER:-${DEPLOY_USER:-abdu}}"
if id "${DEPLOY_USER}" >/dev/null 2>&1; then
  usermod -aG docker "${DEPLOY_USER}" || true
  echo "    added ${DEPLOY_USER} to docker group (re-login required)"
fi

echo "==> UFW: allow 22/80/443, default deny incoming"
if command -v ufw >/dev/null 2>&1; then
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
  ufw status verbose
else
  apt-get install -y ufw
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

cat <<'EOF'

==> SSH hardening (DO MANUALLY after confirming key login works):
  1. Ensure your SSH public key is in ~/.ssh/authorized_keys
  2. Edit /etc/ssh/sshd_config:
       PasswordAuthentication no
       KbdInteractiveAuthentication no
       PermitRootLogin no
       PubkeyAuthentication yes
  3. systemctl reload ssh

==> Next steps (Deployment phase — not this script):
  cd /opt/cashflow/deploy
  cp .env.example .env   # fill secrets
  docker compose up -d postgres api nginx
  docker compose --profile tools run --rm worker python -m cashflow_db migrate
  curl -fsS http://127.0.0.1/ready
  # register cron: scripts/nightly_pipeline.sh + scripts/backup.sh

Bootstrap host preparation complete.
EOF
