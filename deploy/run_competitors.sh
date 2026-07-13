#!/usr/bin/env bash
# Daily cron entry point for the competitor scraper. Runs the competitor
# runner inside the venv, logs to a per-day file, flock-guarded.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1
mkdir -p logs
LOG="logs/competitors_$(date +%F).log"

exec 9>"logs/.competitors.lock"
if ! flock -n 9; then
  echo "$(date -Is) previous competitor run still going; skipping" >> "$LOG"
  exit 0
fi

{
  echo "=== competitor run start $(date -Is) ==="
  "$APP_DIR/.venv/bin/python" apify_competitor_runner.py
  echo "=== competitor run end $(date -Is) rc=$? ==="
} >> "$LOG" 2>&1
