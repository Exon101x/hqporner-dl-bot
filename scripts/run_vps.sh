#!/usr/bin/env bash
# Native Ubuntu/Debian VPS setup for HQPorner Telegram Bot (Telethon MTProto 2 GB, no Docker)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"
BOT_USER="${SUDO_USER:-$(whoami)}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/run_vps.sh"
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env and fill in your credentials first:"
  echo "  cp .env.example .env && nano .env"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg

sudo -u "${BOT_USER}" python3 -m venv "${REPO_DIR}/.venv"
sudo -u "${BOT_USER}" "${REPO_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${BOT_USER}" "${REPO_DIR}/.venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"

cat >/etc/systemd/system/hqporner-bot.service <<EOF
[Unit]
Description=HQPorner Telegram Bot (Telethon MTProto 2 GB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${REPO_DIR}/.venv/bin/python -u ${REPO_DIR}/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now hqporner-bot.service
echo "Bot installed and started! View logs with: sudo journalctl -u hqporner-bot -f"
