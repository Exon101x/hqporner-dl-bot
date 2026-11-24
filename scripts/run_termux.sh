#!/usr/bin/env bash
# Termux (Android aarch64) setup & runner for HQPorner Telegram Bot (2 GB MTProto)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Created .env — edit it with your credentials first: nano .env"
  exit 1
fi

pkg update -y
pkg install -y python git clang libffi openssl libxml2 libxslt python-lxml ffmpeg

if [[ ! -d ".venv" ]]; then
  python -m venv --system-site-packages .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
fi

python -u bot.py
