# Трекер очереди Хоргоса: где что живёт

Публичный сайт: **https://g146671-prog.github.io/horgos-queue/**

```
GitHub (main)
  web/                 страница и данные, отсюда собирается GitHub Pages
  .github/workflows/   деплой web/ на Pages при каждом изменении web/**
  scraper/             сборщик табло xorgos.com (Python, только stdlib)
  scrape-and-push.sh   запуск сборщика + коммит data.json на Beelink
```

## Как это работает

1. На домашнем сервере **Beelink** (`ssh beelink`, каталог `/opt/q05-tracker`)
   cron каждые 15 минут запускает `scrape-and-push.sh`.
2. Скрипт читает табло, обновляет `data/history.json`, кладёт свежий
   `web/data/data.json` и коммитит его в `main`.
3. GitHub Actions по изменению `web/**` публикует каталог `web/` на GitHub Pages.
4. Страница сама раз в 5 минут перечитывает `data/data.json`; номер брони
   считается только в браузере и на сервер не уходит.

Проверить, что цепочка жива:

```bash
ssh beelink "tail -5 /opt/q05-tracker/logs/cron.log"
curl -s https://g146671-prog.github.io/horgos-queue/data/data.json | head -c 200
```

В логе должны быть строки `ok board=... ZLA=...`, `push ok`, а в `data.json` —
свежий `board_time` (пекинское время, UTC+8).

## Beelink: первичная настройка

```bash
ssh beelink
sudo mkdir -p /opt/q05-tracker && sudo chown anton:anton /opt/q05-tracker
git clone https://github.com/g146671-prog/horgos-queue.git /opt/q05-tracker
cd /opt/q05-tracker
mkdir -p data logs
python3 scraper/scrape.py --once      # проверить, что табло читается
crontab -e
```

Строка cron:

```
*/15 * * * * bash /opt/q05-tracker/scrape-and-push.sh >> /opt/q05-tracker/logs/cron.log 2>&1
```

Git в клоне должен быть настроен на коммиты (`user.name`, `user.email`) и иметь
право пушить в `main`. `data/history.json` — рабочая история, в git не входит;
не удалять: из неё считаются средние скорости.

## GitHub Pages

- Источник: **GitHub Actions** (Settings → Pages → Build and deployment).
- Workflow `.github/workflows/pages.yml` публикует только `web/`.
- Деплой иногда падал с `Found 0 artifact(s)`: артефакт не успевал появиться в
  API. Перед `deploy-pages` теперь стоит шаг ожидания; если деплой всё же упал,
  достаточно перезапустить упавший job (Actions → Re-run failed jobs).
- Свежесть данных проверяется по `fetched_utc` в `data/data.json`. Pages кэширует
  файл на 10 минут, поэтому сразу после пуша он может быть ещё старым.

## Старый VPS

На `nl-vps` в `/opt/q05-tracker` ещё работает прежняя копия (nginx + сборщик в
Docker) и отдаёт `https://horgos-queue.pogovorim-chat.ru`. На странице сверху
висит красное предупреждение со ссылкой на github.io.

Когда старый адрес решено будет закрыть:

```bash
ssh nl-vps
cd /opt/q05-tracker && docker compose down
# убрать блок horgos-queue из /opt/familychat-entry/Caddyfile и перечитать:
cd /opt/familychat-entry && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

`docker compose down` в `/opt/q05-tracker` мессенджер не затрагивает. Не выполнять
`docker system prune --volumes` — это уничтожит данные мессенджера.

## Обслуживание

```bash
ssh beelink "cd /opt/q05-tracker && git log --oneline -3 && tail -20 logs/cron.log"
ssh beelink "cd /opt/q05-tracker && bash scrape-and-push.sh"   # запуск вручную
ssh beelink "cd /opt/q05-tracker && git pull --rebase"          # после правок в GitHub
```

Если на табло поменяют разметку, в логе появится
`не найдено ... сайт поменял разметку?` — сайт продолжит показывать последние
данные с предупреждением об устаревании, пока парсер не поправят.

Сайт также периодически подменяет числа строкой `xorgos.com` (защита от
скрейпинга). Сборщик делает до пяти попыток, снимает суффикс, а если часть
плиток всё же пришла неполной — берёт прошлые значения и пишет в лог
`внимание: ... плиток пришли неполными`. Отдельных действий это не требует.
