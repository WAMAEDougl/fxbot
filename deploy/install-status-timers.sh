#!/usr/bin/env bash
# Install the daily-status and hourly-watchdog timers. Run as root on the VPS:
#   /opt/fxbot/deploy/install-status-timers.sh
set -euo pipefail
cd "$(dirname "$0")"
install -m 644 fxbot-status.service fxbot-status.timer fxbot-watchdog.service fxbot-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fxbot-status.timer fxbot-watchdog.timer
systemctl list-timers 'fxbot-*' --no-pager
