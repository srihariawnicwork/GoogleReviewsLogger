#!/usr/bin/env bash
# Cron entry point. Runs the Apify review runner inside the venv and logs
# output to a per-day file. Safe to run concurrently-guarded via flock.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1
mkdir -p logs
LOG="logs/reviews_$(date +%F).log"

# flock prevents overlapping runs if a run outlives the cron interval.
exec 9>"logs/.reviews.lock"
if ! flock -n 9; then
  echo "$(date -Is) previous run still going; skipping" >> "$LOG"
  exit 0
fi

{
  echo "=== run start $(date -Is) ==="
  "$APP_DIR/.venv/bin/python" apify_runner.py
  echo "=== run end $(date -Is) rc=$? ==="
} >> "$LOG" 2>&1
