#!/usr/bin/env python3
"""
Сборщик ZLA с официального портала horgoseport.com.

Страница «重车预约查询» (#/public/zcyy) умеет искать только по конкретному
номеру брони, списков нет. Поэтому храним последний подтверждённый прошедший
номер и каждый запуск проверяем небольшое окно номеров за ним, беря максимум
из 已放行. Окно нужно потому, что машины проходят не строго по порядку:
вызванные, но не погруженные номера могут стоять перед уже прошедшим.

Запускается из scrape-and-push.sh до сбора xorgos. Пишет два файла:
  --state  состояние (последний прошедший номер, журнал проверок);
  --out    снимок для scrape.py (last_passed + fetched_utc).

Особенности портала:
  * капча Tencent на каждый запрос, но обычно проходит без слайдера;
  * один и тот же метод нельзя вызывать чаще, чем раз в 5 секунд;
  * «нет такого номера» приходит как HTTP 400 «查询无数据».

Если капча потребовала слайдер, скрипт завершается с кодом 3 и ничего не
перезаписывает — scrape.py продолжит брать ZLA с xorgos.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

PAGE_URL = "https://www.horgoseport.com/#/public/zcyy"
API_MARK = "getHeavyResById"

DELAY = 6.0             # портал не принимает два запроса чаще, чем раз в 5 с
WINDOW = 8              # сколько номеров впереди проверять за цикл
MAX_CHECKS = 12         # жёсткий предел запросов за цикл (догон после простоя)
MAX_SPAN = 40           # насколько далеко вперёд уходить от старого значения
BOOTSTRAP_BACK = 25     # на сколько шагов назад искать при первом запуске

PASSED = "已放行"
QUEUE = {"未放行", "已叫号，未登车"}
REJECTED = {"已退回"}
KNOWN = {PASSED, "无此预约编号"} | QUEUE | REJECTED


class CaptchaRequired(RuntimeError):
    """Портал показал слайдер — без человека запрос не пройдёт."""


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def code(n):
    return "ZLA%06d" % n


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Portal:
    def __init__(self, profile_dir, headless=True, delay=DELAY):
        self.profile_dir = profile_dir
        self.headless = headless
        self.delay = delay
        self._pw = None
        self.ctx = None
        self.page = None
        self.input = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chromium",
            headless=self.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(60000)
        self.page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=90000)
        self.page.wait_for_selector("input", timeout=90000)
        self.page.wait_for_timeout(3000)
        self.input = self.page.locator("input").first
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx is not None:
                self.ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    def _captcha_visible(self):
        try:
            rects = self.page.eval_on_selector_all(
                "iframe",
                "els => els.map(i => { const b = i.getBoundingClientRect();"
                " return [b.x, b.y, b.width, b.height]; })",
            )
        except Exception:
            return False
        return any(x >= 0 and y >= 0 and w > 0 and h > 0 for x, y, w, h in rects)

    def query(self, n, tries=3):
        """Возвращает статус: 已放行 / 未放行 / 已叫号，未登车 / 已退回 / 无数据."""
        c = code(n)
        last_error = None
        for attempt in range(1, tries + 1):
            self.input.fill("")
            self.input.fill(c)
            try:
                with self.page.expect_response(
                    lambda r: API_MARK in r.url and r.request.method == "GET",
                    timeout=45000,
                ) as ri:
                    self.page.get_by_role("button", name="查询").first.click()
                resp = ri.value
                body = resp.text()
            except Exception as e:  # таймаут — скорее всего капча не отдала токен
                last_error = e
                if self._captcha_visible():
                    raise CaptchaRequired("портал показывает слайдер капчи")
                log(f"{c}: нет ответа ({type(e).__name__}), попытка {attempt}/{tries}")
                time.sleep(self.delay)
                continue

            if resp.status == 200:
                try:
                    data = json.loads(body)
                except ValueError:
                    raise RuntimeError(f"{c}: ответ не JSON: {body[:200]}")
                st = (data.get("status") or "").strip()
                if st in KNOWN:
                    return st
                raise RuntimeError(f"{c}: неизвестный статус {st!r}")

            text = body.strip().strip('"')
            if "5s内不能重复调用" in text:
                log(f"{c}: сработал лимит 5 с, пауза")
                time.sleep(self.delay + 1)
                continue
            if "查询无数据" in text:
                return "无数据"
            if "验证未通过" in text:
                # Токен капчи иногда не успевает обновиться — пробуем ещё раз.
                # Если же портал реально показывает слайдер, нужен человек.
                if self._captcha_visible():
                    raise CaptchaRequired(f"{c}: портал показывает слайдер капчи")
                log(f"{c}: капча не принята ({text[:40]}), попытка {attempt}/{tries}")
                time.sleep(self.delay)
                continue
            raise RuntimeError(f"{c}: HTTP {resp.status}: {text[:200]}")

        raise RuntimeError(f"{c}: нет результата за {tries} попыток: {last_error}")


def bootstrap(portal, hint):
    """Первый запуск: подтверждаем подсказку от xorgos и идём назад, пока не найдём 已放行."""
    if not hint:
        raise RuntimeError("нет сохранённого состояния и нет подсказки из data.json")
    pos = hint
    for i in range(BOOTSTRAP_BACK):
        st = portal.query(pos)
        log(f"старт {code(pos)}: {st}")
        if st == PASSED:
            return pos
        pos -= 1
        if i + 1 < BOOTSTRAP_BACK:
            time.sleep(portal.delay)
    raise RuntimeError(f"не нашли прошедший номер за {BOOTSTRAP_BACK} шагов от {code(hint)}")


def main():
    ap = argparse.ArgumentParser(description="Сбор последнего прошедшего ZLA с horgoseport.com")
    ap.add_argument("--state", required=True, help="файл состояния (last_passed)")
    ap.add_argument("--out", required=True, help="снимок для scrape.py")
    ap.add_argument("--hint-file", default="", help="data.json с текущим ZLA (для первого запуска)")
    ap.add_argument("--profile", default=os.environ.get("OFFICIAL_PROFILE", "/opt/q05-official/profile"))
    ap.add_argument("--max-checks", type=int, default=MAX_CHECKS)
    ap.add_argument("--max-span", type=int, default=MAX_SPAN)
    ap.add_argument("--delay", type=float, default=DELAY)
    ap.add_argument("--headful", action="store_true", help="запустить браузер с окном (отладка)")
    a = ap.parse_args()

    state = load_json(a.state, {})
    last = state.get("last_passed")
    hint = None
    if a.hint_file:
        hint = (load_json(a.hint_file, {}).get("types", {}).get("ZLA", {}) or {}).get("current")

    started = time.time()
    results = {}
    checks = 0
    with Portal(a.profile, headless=not a.headful, delay=a.delay) as portal:
        if not last:
            last = bootstrap(portal, hint)
            time.sleep(a.delay)  # последний запрос bootstrap тоже считается лимитом

        start = last
        pos = start + 1
        limit = start + WINDOW
        while checks < a.max_checks and pos <= limit and pos <= start + a.max_span:
            st = portal.query(pos)
            checks += 1
            results[pos] = st
            log(f"{code(pos)}: {st}")
            if st == PASSED:
                last = pos
                # Держим окно впереди найденного прошедшего: так догоняем,
                # если сборщик пропустил несколько циклов.
                limit = max(limit, pos + WINDOW)
            pos += 1
            if checks < a.max_checks and pos <= limit:
                time.sleep(a.delay)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = {"last_passed": last, "fetched_utc": now, "checks": checks}
    write_atomic(a.state, {
        "last_passed": last,
        "fetched_utc": now,
        "checks": checks,
        "last_run": {str(k): v for k, v in results.items()},
    })
    write_atomic(a.out, snapshot)
    log(f"ok last_passed={last} checks={checks} за {time.time() - started:.0f} с")


if __name__ == "__main__":
    try:
        main()
    except CaptchaRequired as e:
        log(f"капча: {e}")
        sys.exit(3)
    except Exception as e:
        log(f"ошибка: {e}")
        sys.exit(1)
