#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu/Debian VPS. Run as root:
#   curl -fsSL <raw url>/deploy/bootstrap-vps.sh | bash -s -- https://github.com/WAMAEDougl/fxbot
# Then: cd /opt/fxbot && cp .env.example .env && nano .env   (paper creds + IB_USER/IB_PASSWORD)
#       docker compose up -d gateway   # approve the 2FA prompt in IBKR Mobile
#       docker compose --profile engine up -d --build
set -euo pipefail
REPO="${1:?usage: bootstrap-vps.sh <git repo url>}"

apt-get update -y
apt-get install -y ca-certificates curl git ufw restic
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

# Firewall: SSH only. The IB API ports are bound to 127.0.0.1 by compose.
ufw allow OpenSSH
ufw --force enable

id trader >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker trader
git clone "$REPO" /opt/fxbot || git -C /opt/fxbot pull
mkdir -p /opt/fxbot/{state,audit,data_lake}
chown -R trader:trader /opt/fxbot
# The container runs as uid 1000 (see Dockerfile); the bind-mounted dirs must be writable by it.
chown -R 1000:1000 /opt/fxbot/{state,audit,data_lake}

# Daily off-host backup (needs /etc/fxbot-backup.env, see deploy/backup.sh)
echo "15 2 * * * trader /opt/fxbot/deploy/backup.sh >> /var/log/fxbot-backup.log 2>&1" > /etc/cron.d/fxbot-backup
echo "Done. Next: edit /opt/fxbot/.env, then start the gateway."
