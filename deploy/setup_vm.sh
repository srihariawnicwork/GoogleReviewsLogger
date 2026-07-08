#!/usr/bin/env bash
# One-time setup on the Ubuntu VM for the Apify review runner.
# Usage:  bash deploy/setup_vm.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
echo "App dir: $APP_DIR"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

# Isolated Python env (the runner only needs requests + python-dotenv — no browser)
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install requests python-dotenv

mkdir -p logs
chmod +x deploy/run_reviews.sh

echo
echo "Setup complete."
echo "Next:"
echo "  1) Create $APP_DIR/.env  (APIFY_TOKEN, N8N_WEBHOOK_URL, APP_TZ_OFFSET_HOURS=4, etc.)"
echo "  2) Test once:  .venv/bin/python apify_runner.py"
echo "  3) Install cron:  crontab -e   (see deploy/awnic.cron)"
