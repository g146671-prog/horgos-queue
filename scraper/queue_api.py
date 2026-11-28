#!/usr/bin/env python3
"""Онлайн-запрос реального места в очереди ZLA на официальном портале.

Страница на GitHub Pages не может сама дёргать API таможни: нужна капча
Tencent и нет гарантии CORS. Поэтому здесь живёт один headless-браузер,
который по запросу вводит номер и возвращает `ranking` — реальное число
машин впереди.

    GET /queue?num=ZLA005292
    → {"num":"ZLA005292","status":"未放行","ahead":774,"ranking":774,"cached":false}

    GET /health
    → {"ok":true,"browser":true,"pending":0,...}

Особенности:
  * один воркер и один браузер: портал не любит чаще раза в 5 секунд,
    а Playwright sync API нельзя дёргать из разных потоков;
  * кэш только в памяти (10 минут), номер не пишется на диск;
  * в лог попадает маска ZLA***292, без полного номера;
  * отдельный профиль браузера, чтобы не мешать сборщику ZLA.
"""
import json
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from official_zla import CaptchaRequired, Portal, log  # noqa: E402

HOST = os.environ.get("Q05_QUEUE_HOST", "127.0.0.1")
PORT = int(os.environ.get("Q05_QUEUE_PORT", "8092"))
PROFILE = os.environ.get("Q05_QUEUE_PROFILE", "/opt/q05-official/profile-api")

CACHE_TTL = 600          # 10 минут
CACHE_MAX = 500
MAX_WAIT = 30            # сколько HTTP-запрос ждёт результат
MAX_PENDING = 4          # больше в очередь не берём
MIN_GAP = 6.0            # портал: не чаще раза в 5 с
PER_IP_GAP = 10          # один посетитель — не чаще раза в 10 с
PER_IP_HOUR = 30

NUM_RE = re.compile(r"^ZLA\d{6}$")

CACHE = {}
CACHE_LOCK = threading.Lock()
JOBS = queue.Queue()
HITS = {}
HITS_LOCK = threading.Lock()
STATE = {
    "browser": False,
    "last_ok": None,
    "cooldown_until": 0,
    "started": time.time(),
}


def mask(num):
    return num[:3] + "***" + num[-3:]


def result_for(num, data):
    st = data["status"]
    ranking = data.get("ranking")
    if st == "已放行":
        ahead = 0
    elif ranking is not None:
        ahead = ranking
    else:
        ahead = None
    return {
        "num": num,
        "status": st,
        "ahead": ahead,
        "ranking": ranking,
        "cached": False,
    }


def close_portal(portal):
    try:
        if portal is not None:
            portal.__exit__(None, None, None)
    except Exception:
        pass
    return None


def open_portal():
    portal = Portal(PROFILE, headless=True).__enter__()
    STATE["browser"] = True
    return portal


def worker():
    portal = None
    last_query = 0.0
    retry_at = 0.0
    while True:
        try:
            num, box = JOBS.get(timeout=30)
        except queue.Empty:
            # Простой: заранее поднимаем браузер, чтобы первый посетитель
            # не ждал 20–30 секунд. Если не вышло — пробуем раз в минуту.
            now = time.time()
            if (portal is None and now >= retry_at
                    and STATE["cooldown_until"] <= now):
                try:
                    portal = open_portal()
                    retry_at = 0.0
                    log("queue: браузер прогрет")
                except Exception as e:
                    log(f"queue: прогрев не удался: {str(e)[:160]}")
                    retry_at = time.time() + 60
            continue

        now = time.time()
        with CACHE_LOCK:
            hit = CACHE.get(num)
        if hit and hit[0] > now:
            box["result"] = dict(hit[1], cached=True)
            box["event"].set()
            continue
        if STATE["cooldown_until"] > now:
            box["result"] = {
                "error": "captcha",
                "retry_after": int(STATE["cooldown_until"] - now),
            }
            box["event"].set()
            continue

        try:
            if portal is None and now >= retry_at:
                try:
                    portal = open_portal()
                    retry_at = 0.0
                except Exception as e:
                    log(f"queue: браузер не поднялся: {str(e)[:160]}")
                    retry_at = time.time() + 60
            if portal is None:
                box["result"] = {"error": "portal", "detail": "браузер ещё не готов"}
                box["event"].set()
                continue

            wait = MIN_GAP - (time.time() - last_query)
            if wait > 0:
                time.sleep(wait)

            try:
                res = result_for(num, portal.query_data(int(num[3:])))
            except CaptchaRequired:
                raise
            except Exception:
                # Браузер мог умереть или страница «залипла» — один раз
                # перезапускаем и повторяем запрос.
                portal = close_portal(portal)
                STATE["browser"] = False
                time.sleep(2)
                portal = open_portal()
                time.sleep(MIN_GAP)
                res = result_for(num, portal.query_data(int(num[3:])))
            last_query = time.time()

            with CACHE_LOCK:
                if len(CACHE) >= CACHE_MAX:
                    CACHE.clear()
                CACHE[num] = (time.time() + CACHE_TTL, res)
            STATE["last_ok"] = time.time()
            log(f"queue {mask(num)}: {res['status']} ahead={res['ahead']}")
            box["result"] = res
        except CaptchaRequired as e:
            log(f"queue: капча-слайдер: {str(e)[:160]}")
            STATE["cooldown_until"] = time.time() + 300
            portal = close_portal(portal)
            STATE["browser"] = False
            box["result"] = {"error": "captcha", "retry_after": 300}
        except Exception as e:
            log(f"queue {mask(num)}: ошибка {str(e)[:200]}")
            portal = close_portal(portal)
            STATE["browser"] = False
            box["result"] = {"error": "portal", "detail": str(e)[:200]}
        finally:
            box["event"].set()


def allowed(ip):
    now = time.time()
    with HITS_LOCK:
        arr = [t for t in HITS.get(ip, []) if now - t < 3600]
        if arr and now - arr[-1] < PER_IP_GAP:
            HITS[ip] = arr
            return False
        if len(arr) >= PER_IP_HOUR:
            HITS[ip] = arr
            return False
        arr.append(now)
        HITS[ip] = arr
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "q05-queue/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Не пишем в лог query-строку: там номер брони.
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "600")

    def reply(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"

        if path == "/health":
            with CACHE_LOCK:
                cached = len(CACHE)
            return self.reply(200, {
                "ok": True,
                "browser": STATE["browser"],
                "pending": JOBS.qsize(),
                "cached": cached,
                "last_ok": STATE["last_ok"],
                "cooldown_until": STATE["cooldown_until"],
                "uptime": int(time.time() - STATE["started"]),
            })

        if path != "/queue":
            return self.reply(404, {"error": "not_found"})

        num = (parse_qs(u.query).get("num") or [""])[0].upper().strip()
        if not NUM_RE.match(num):
            return self.reply(400, {"error": "bad_num"})

        ip = (self.headers.get("X-Forwarded-For") or
              self.client_address[0]).split(",")[0].strip()
        if not allowed(ip):
            return self.reply(429, {"error": "rate_limited"})
        if JOBS.qsize() >= MAX_PENDING:
            return self.reply(429, {"error": "busy"})

        box = {"event": threading.Event(), "result": None}
        JOBS.put((num, box))
        if not box["event"].wait(MAX_WAIT):
            return self.reply(504, {"error": "timeout"})

        res = box["result"] or {"error": "portal"}
        if res.get("error") == "captcha":
            return self.reply(503, res)
        if res.get("error"):
            return self.reply(502, res)
        return self.reply(200, res)


def main():
    threading.Thread(target=worker, name="portal", daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log(f"queue api: слушаю http://{HOST}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
