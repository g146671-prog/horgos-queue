#!/usr/bin/env python3
"""Приёмник анонимной статистики q05-tracker.

Слушает только localhost; наружу его отдаёт Caddy на VPS через SSH-туннель.
Пишет те же события, что раньше писал nginx:

    время(UTC) /open|/hit|/share n d p h

Без IP, User-Agent и номера брони. Страница /stats/ — та же, что в web/stats/.
"""
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(os.environ.get("Q05_STATS_DIR", "/opt/q05-stats"))
LOG = BASE / "logs" / "events.log"
STATS = BASE / "data" / "stats.json"
KEY_FILE = BASE / "stats-key.conf"
PAGE = Path(os.environ.get("Q05_STATS_PAGE", "/opt/q05-tracker/web/stats/index.html"))
HOST = os.environ.get("Q05_STATS_HOST", "127.0.0.1")
PORT = int(os.environ.get("Q05_STATS_PORT", "8091"))

PREFIX_RE = re.compile(r"^[A-Z]{3}$")
LOCK = threading.Lock()
KEY = ""


def load_key():
    try:
        text = KEY_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'"([^"]+)"\s+1', text)
    return m.group(1) if m else text.strip()


def flag(qs, name):
    return "1" if qs.get(name, [""])[0] == "1" else "0"


def category(qs):
    p = qs.get("p", [""])[0].upper()
    return p if PREFIX_RE.match(p) else "-"


def log_event(path, qs):
    line = "%s %s %s %s %s %s\n" % (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        path,
        flag(qs, "n"),
        flag(qs, "d"),
        category(qs),
        flag(qs, "h"),
    )
    with LOCK:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)


class Handler(BaseHTTPRequestHandler):
    server_version = "q05-stats/1.0"

    def log_message(self, fmt, *args):
        pass  # не пишем IP и пути в журнал

    def send_headers(self, code, ctype=None, extra=None):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path in ("/open", "/hit", "/share"):
            log_event(path, parse_qs(u.query))
            self.send_headers(204, extra={"Access-Control-Allow-Origin": "*"})
            return

        if path in ("/stats", "/stats/"):
            try:
                body = PAGE.read_bytes()
            except OSError:
                self.send_headers(404)
                return
            self.send_headers(200, "text/html; charset=utf-8")
            self.wfile.write(body)
            return

        if path == "/stats/stats.json":
            if not KEY or self.headers.get("X-Key", "") != KEY:
                self.send_headers(401)
                return
            try:
                body = STATS.read_bytes()
            except OSError:
                self.send_headers(404)
                return
            self.send_headers(200, "application/json; charset=utf-8")
            self.wfile.write(body)
            return

        self.send_headers(404)


def main():
    global KEY
    (BASE / "logs").mkdir(parents=True, exist_ok=True)
    (BASE / "data").mkdir(parents=True, exist_ok=True)
    LOG.touch(exist_ok=True)
    KEY = load_key()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
