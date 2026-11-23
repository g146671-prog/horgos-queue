#!/usr/bin/env python3
"""
Сборщик табло очереди Хоргоса (xorgos.com).

Раз в INTERVAL секунд читает главную страницу, вынимает по каждой категории
текущий номер, диапазон и счётчики «сегодня / вчера / позавчера», копит
суточные итоги в history.json и публикует для сайта data.json.

Только стандартная библиотека Python. Ничего не принимает извне,
никуда не пишет, кроме каталога DATA_DIR.
"""
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
import urllib.request

SOURCE_URL = os.environ.get("SOURCE_URL", "https://xorgos.com/")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEED_FILE = os.environ.get("SEED_FILE", "/app/seed.json")
INTERVAL = int(os.environ.get("INTERVAL", "900"))  # 15 минут
USER_AGENT = os.environ.get("USER_AGENT", "q05-tracker/1.0 (community queue forecast)")
KEEP_DAYS = 60          # сколько суток истории держать
KEEP_SNAPSHOTS = 400    # сколько последних снимков держать (~4 суток при 15 мин)

HISTORY = os.path.join(DATA_DIR, "history.json")
PUBLIC = os.path.join(DATA_DIR, "data.json")

TIME_RE = re.compile(r'id="board-time">\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*<')
TILE_RE = re.compile(
    r'<div class="tn">(?P<name>.*?)</div>\s*'
    r'<div class="tnum" id="num-(?P<prefix>[A-Z]{3})">(?P<num>[A-Z]{3}\d+)</div>\s*'
    r'<div class="trange"[^>]*>(?P<left>[A-Z]{3}\d+)\s*[—-]\s*(?P<right>[A-Z]{3}\d+)</div>\s*'
    r'<div class="tstats">\s*'
    r'<div>[^<]*<b>(?P<today>\d+)</b></div>\s*'
    r'<div>[^<]*<b>(?P<yday>\d+)</b></div>\s*'
    r'<div>[^<]*<b>(?P<dbyday>\d+)</b></div>',
    re.S,
)
NAME_TAIL_RE = re.compile(r"\s*\([A-Z]{3}\)\s*$")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read(2_000_000).decode("utf-8", "replace")


def digits(code):
    return int(re.sub(r"\D", "", code))


def parse(page):
    m = TIME_RE.search(page)
    if not m:
        raise ValueError("не найдено время табло (board-time) — сайт поменял разметку?")
    board_time = m.group(1)
    types = {}
    for t in TILE_RE.finditer(page):
        p = t.group("prefix")
        types[p] = {
            "name": NAME_TAIL_RE.sub("", html.unescape(t.group("name")).strip()),
            "current": digits(t.group("num")),
            "left": digits(t.group("left")),
            "right": digits(t.group("right")),
            "today": int(t.group("today")),
            "yesterday": int(t.group("yday")),
            "day_before": int(t.group("dbyday")),
        }
    if not types:
        raise ValueError("не найдено ни одной плитки категорий — сайт поменял разметку?")
    return board_time, types


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def update(history, board_time, types, fetched_utc):
    """Вчера/позавчера с табло — готовые итоги полных пекинских суток."""
    day = dt.date.fromisoformat(board_time[:10])
    d1 = (day - dt.timedelta(days=1)).isoformat()
    d2 = (day - dt.timedelta(days=2)).isoformat()
    daily = history.setdefault("daily", {})
    for p, t in types.items():
        row = daily.setdefault(p, {})
        row[d1] = t["yesterday"]
        row[d2] = t["day_before"]
        cutoff = (day - dt.timedelta(days=KEEP_DAYS)).isoformat()
        for k in [k for k in row if k < cutoff]:
            del row[k]
    snaps = history.setdefault("snapshots", [])
    if not snaps or snaps[-1]["board_time"] != board_time:
        snaps.append({
            "board_time": board_time,
            "fetched_utc": fetched_utc,
            "current": {p: t["current"] for p, t in types.items()},
        })
    del snaps[:-KEEP_SNAPSHOTS]
    history["last"] = {"board_time": board_time, "fetched_utc": fetched_utc, "types": types}


def public_view(history):
    last = history["last"]
    out = {
        "source": SOURCE_URL,
        "board_time": last["board_time"],        # пекинское время (UTC+8)
        "fetched_utc": last["fetched_utc"],
        "types": {},
    }
    today = last["board_time"][:10]
    for p, t in last["types"].items():
        daily = history.get("daily", {}).get(p, {})
        full = sorted((d, n) for d, n in daily.items() if d < today)
        out["types"][p] = dict(t, daily=[{"date": d, "count": n} for d, n in full[-30:]])
    return out


def seed(history):
    s = load_json(SEED_FILE, {})
    for p, row in s.get("daily", {}).items():
        dst = history.setdefault("daily", {}).setdefault(p, {})
        for d, n in row.items():
            dst.setdefault(d, n)


def run_once():
    history = load_json(HISTORY, {})
    if not history.get("seeded"):
        seed(history)
        history["seeded"] = True
    page = fetch(SOURCE_URL)
    board_time, types = parse(page)
    fetched = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update(history, board_time, types, fetched)
    write_atomic(HISTORY, history)
    write_atomic(PUBLIC, public_view(history))
    zla = types.get("ZLA")
    log(f"ok board={board_time} types={len(types)}" + (f" ZLA={zla['current']}" if zla else ""))


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    once = "--once" in sys.argv
    fails = 0
    while True:
        try:
            run_once()
            fails = 0
        except Exception as e:  # сеть, разметка — пробуем позже, старые данные остаются
            fails += 1
            log(f"ошибка ({fails} подряд): {e}")
            if once:
                sys.exit(1)
        if once:
            return
        # небольшой разброс, чтобы не стучаться секунда в секунду; при сбоях — реже
        delay = INTERVAL * min(4, 1 + fails // 3) + random.randint(0, 60)
        time.sleep(delay)


if __name__ == "__main__":
    main()
