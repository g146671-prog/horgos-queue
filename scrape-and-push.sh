#!/bin/bash
# Сборка данных на Beelink и публикация на GitHub Pages.
# Запускается по cron каждые 15 минут:
#   */15 * * * * bash /opt/q05-tracker/scrape-and-push.sh >> /opt/q05-tracker/logs/cron.log 2>&1
set -euo pipefail

REPO_DIR=/opt/q05-tracker
DATA_DIR="$REPO_DIR/data"
OFFICIAL_PY=/opt/q05-official/venv/bin/python
export DATA_DIR
export SEED_FILE="$REPO_DIR/scraper/seed.json"
export OFFICIAL_ZLA_FILE="$DATA_DIR/official-zla.json"
LOG=$(date -u +"%Y-%m-%d %H:%M:%S")

# Если предыдущий запуск ещё не закончился — тихо выходим.
exec 9>"$REPO_DIR/logs/.scrape.lock"
flock -n 9 || { echo "$LOG previous run still active"; exit 0; }

# ZLA — с официального портала (headless Chromium). Если не получилось,
# scrape.py возьмёт ZLA с xorgos, как раньше.
if [ -x "$OFFICIAL_PY" ]; then
  if "$OFFICIAL_PY" "$REPO_DIR/scraper/official_zla.py" \
      --state "$DATA_DIR/official-zla-state.json" \
      --out "$OFFICIAL_ZLA_FILE" \
      --hint-file "$DATA_DIR/data.json" \
      >> "$REPO_DIR/logs/official.log" 2>&1; then
    echo "$LOG official zla ok"
  else
    echo "$LOG official zla failed (see logs/official.log)"
  fi
else
  echo "$LOG official python missing, ZLA from xorgos"
fi

python3 "$REPO_DIR/scraper/scrape.py" --once
echo "$LOG scraper ok"

cp "$DATA_DIR/data.json" "$REPO_DIR/web/data/data.json"
cd "$REPO_DIR"

# Подхватить правки, сделанные в репозитории (workflow, документация).
# Локальный data.json при необходимости уходит в autostash и возвращается после rebase.
git pull --rebase --autostash --quiet origin main

if git diff --quiet -- web/data/data.json; then
  echo "$LOG no change"
  exit 0
fi

git add web/data/data.json
git commit -m "data: $(date -u +%Y-%m-%dT%H:%M:%SZ)" --no-gpg-sign
git push --quiet origin main || {
  # Кто-то успел запушить между pull и push — ещё раз подтянуть и повторить.
  git pull --rebase --autostash --quiet origin main
  git push --quiet origin main
}
echo "$LOG push ok"
