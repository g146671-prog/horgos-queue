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
# Официальный ZLA (horgoseport.com) собирается отдельно, браузером; здесь только
# подхватываем его снимок и подменяем номер, остальные поля остаются с xorgos.
OFFICIAL_ZLA = os.environ.get("OFFICIAL_ZLA_FILE", "")
OFFICIAL_MAX_AGE = 60 * 60  # снимок старше часа не используем

HISTORY = os.path.join(DATA_DIR, "history.json")
PUBLIC = os.path.join(DATA_DIR, "data.json")

TIME_RE = re.compile(r'id="board-time">\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*<')
TILE_RE = re.compile(
    r'<div class="tn">(?P<name>.*?)</div>\s*'
    r'<div class="tnum" id="num-(?P<prefix>[A-Z]{3})">(?P<num>[^<]*)</div>\s*'
    r'<div class="trange"[^>]*>(?P<left>[^<]*?)\s*[—-]\s*(?P<right>[^<]*?)</div>\s*'
    r'<div class="tstats">\s*'
    r'<div>[^<]*<b>(?P<today>[^<]*)</b></div>\s*'
    r'<div>[^<]*<b>(?P<yday>[^<]*)</b></div>\s*'
    r'<div>[^<]*<b>(?P<dbyday>[^<]*)</b></div>',
    re.S,
)
NAME_TAIL_RE = re.compile(r"\s*\([A-Z]{3}\)\s*$")
# Сайт иногда подмешивает к числам свою строку "xorgos.com" (защита от скрейпинга),
# а иногда заменяет ею число целиком. Первое снимаем, второе считаем неполными данными.
MARK_RE = re.compile(r"xorgos\.com", re.I)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read(2_000_000).decode("utf-8", "replace")


def digits(code):
    s = MARK_RE.sub("", html.unescape(code or ""))
    s = re.sub(r"\D", "", s)
    return int(s) if s else None


def count(raw):
    s = MARK_RE.sub("", html.unescape(raw or ""))
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def parse(page):
    m = TIME_RE.search(page)
    if not m:
        raise ValueError("не найдено время табло (board-time) — сайт поменял разметку?")
    board_time = m.group(1)
    types = {}
    incomplete = 0
    for t in TILE_RE.finditer(page):
        p = t.group("prefix")
        vals = {
            "current": digits(t.group("num")),
            "left": digits(t.group("left")),
            "right": digits(t.group("right")),
            "today": count(t.group("today")),
            "yesterday": count(t.group("yday")),
            "day_before": count(t.group("dbyday")),
        }
        if any(v is None for v in vals.values()):
            incomplete += 1
            continue
        types[p] = {
            "name": NAME_TAIL_RE.sub("", html.unescape(t.group("name")).strip()),
            **vals,
        }
    if not types:
        raise ValueError("не найдено ни одной полной плитки категорий — сайт поменял разметку?")
    return board_time, types, incomplete


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


def official_zla():
    """Последний прошедший ZLA с официального портала, если снимок свежий."""
    if not OFFICIAL_ZLA:
        return None
    try:
        with open(OFFICIAL_ZLA, encoding="utf-8") as f:
            snap = json.load(f)
        ts = dt.datetime.strptime(snap["fetched_utc"], "%Y-%m-%dT%H:%M:%SZ")
        ts = ts.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
        if age > OFFICIAL_MAX_AGE:
            log(f"официальный ZLA устарел ({age / 60:.0f} мин), берём xorgos")
            return None
        return int(snap["last_passed"])
    except Exception as e:
        log(f"официальный ZLA недоступен ({e}), берём xorgos")
        return None


def seed(history):
    s = load_json(SEED_FILE, {})
    for p, row in s.get("daily", {}).items():
        dst = history.setdefault("daily", {}).setdefault(p, {})
        for d, n in row.items():
            dst.setdefault(d, n)


ATTEMPTS = 5


def read_board():
    """Читает табло, повторяя попытку, если сайт подменил числа защитной строкой."""
    best = None
    last_error = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            page = fetch(SOURCE_URL)
            board_time, types, incomplete = parse(page)
        except Exception as e:
            last_error = e
            log(f"попытка {attempt}/{ATTEMPTS}: {e}")
        else:
            if not incomplete:
                return board_time, types, 0
            last_error = f"неполные данные ({incomplete} плиток)"
            log(f"попытка {attempt}/{ATTEMPTS}: {last_error}")
            if best is None or len(types) > len(best[1]):
                best = (board_time, types, incomplete)
        if attempt < ATTEMPTS:
            time.sleep(3 * attempt)
    if best is not None:
        return best
    raise RuntimeError(f"табло не отдало данные за {ATTEMPTS} попыток: {last_error}")


def run_once():
    history = load_json(HISTORY, {})
    if not history.get("seeded"):
        seed(history)
        history["seeded"] = True
    board_time, types, incomplete = read_board()
    if incomplete:
        # Неполные плитки не публикуем: берём прошлые значения, чтобы категория не пропала.
        for p, t in history.get("last", {}).get("types", {}).items():
            types.setdefault(p, t)
        log(f"внимание: {incomplete} плиток пришли неполными, взяты прошлые значения")
    official = official_zla()
    if official is not None and "ZLA" in types:
        before = types["ZLA"]["current"]
        types["ZLA"]["current"] = official
        types["ZLA"]["official"] = True
        if before != official:
            log(f"ZLA официально: {before} -> {official}")
        if before - official > 2:
            # Официальный сборщик мог упереться в пропуск номеров — стоит проверить.
            log(f"внимание: официальный ZLA {official} отстаёт от xorgos {before}")
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
