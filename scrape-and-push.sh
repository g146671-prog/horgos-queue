#!/bin/bash
# Запуск скрейпера + пуш data.json в GitHub Pages.
# Запускается по cron каждые 15 минут на Beelink.
# Использование: */15 * * * * /opt/q05-tracker/scrape-and-push.sh >> /opt/q05-tracker/logs/cron.log 2>&1
set -euo pipefail

REPO_DIR=/opt/q05-tracker
DATA_DIR="$REPO_DIR/data"
SCRAPER="$REPO_DIR/scraper/scrape.py"
LOG_PREFIX="$(date -u +%Y-%m-%d\ %H:%M:%S)"

cd "$REPO_DIR"

# 1. Запустить скрейпер (однократно)
python3 "$SCRAPER" --once
echo "$LOG_PREFIX scraper ok"

# 2. Если data.json изменился — коммит и пуш
if git -C "$REPO_DIR" diff --quiet -- data/data.json 2>/dev/null; then
    echo "$LOG_PREFIX data.json не изменился, пуш не нужен"
    exit 0
fi

git -C "$REPO_DIR" add data/data.json
git -C "$REPO_DIR" commit -m "data: $(date -u +%Y-%m-%dT%H:%M:%SZ)" --no-gpg-sign
git -C "$REPO_DIR" push origin main
echo "$LOG_PREFIX push ok"
