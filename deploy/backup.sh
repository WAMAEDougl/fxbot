#!/usr/bin/env bash
# Daily off-host backup of state DB + audit log (design 8).
# Cron:  15 2 * * *  /opt/fxbot/deploy/backup.sh
# Needs restic (https://restic.net) and RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE
# (e.g. an S3/B2/Hetzner Storage Box repo) in /etc/fxbot-backup.env.
set -euo pipefail
source /etc/fxbot-backup.env
cd "$(dirname "$0")/.."
restic backup state audit configs --tag fxbot
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
