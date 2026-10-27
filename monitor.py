"""
Мониторинг свободных окон записи в Посольство Молдовы в РФ (Reservio) → Telegram.

Берёт свободные слоты напрямую из API сайта записи (тем же запросом, что делает сам сайт),
сравнивает с прошлой проверкой и присылает в Telegram только НОВЫЕ окна:
  • освободившиеся после чьей-то отмены;
  • новые дни, которые открываются для записи.

Кнопки бота в Telegram:
  🔍 Проверить сейчас — свежая проверка и все свободные окна прямо сейчас;
  ℹ️ Статус          — работает ли мониторинг и когда была последняя проверка.

Запуск:
  python monitor.py           — одна проверка
  python monitor.py --for 330 — проверять каждые INTERVAL_MIN минут в течение 330 минут
                                и отвечать на кнопки (так запускает GitHub Actions)
  python monitor.py --loop    — то же, но бесконечно (свой сервер / Railway)
Без TG_TOKEN / TG_CHAT_ID делается одна пробная проверка с выводом в лог, состояние не сохраняется.
"""

import html
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ===================== НАСТРОЙКИ =====================

SITE = "https://ambasada-r-moldova-in-f-rusa.reservio.com"
BUSINESS_ID = "09250556-2450-437f-aede-82e78712f114"
# Услуга. По умолчанию «Изготовление паспорта (загран)».
# ID других услуг программа печатает в лог при первом запуске.
SERVICE_ID = os.getenv("SERVICE_ID") or "59c845fa-48c9-4f79-b7b2-1d07f895f2e6"
# Следить только за определёнными сотрудниками (имена через запятую, как на сайте).
# Пусто — за всеми, кто ведёт эту услугу.
STAFF = os.getenv("STAFF") or ""
STAFF_FILTER = [s.strip().lower() for s in STAFF.split(",") if s.strip()]
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD") or "90")
INTERVAL_MIN = float(os.getenv("INTERVAL_MIN") or "1")

TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
# Кому писать: Id через запятую (можно нескольким людям). На кнопки бот отвечает только им.
TG_CHAT_IDS = [c.strip() for c in (os.getenv("TG_CHAT_ID") or "").split(",") if c.strip()]

STATE_FILE = Path(os.getenv("STATE_FILE") or "state.json")
TZ = ZoneInfo("Europe/Moscow")

# =====================================================

API = f"{SITE}/api/v2/businesses/{BUSINESS_ID}"
BOOKING_URL = f"{SITE}/order?what=resource"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/vnd.api+json, application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": f"{SITE}/order?what=time",
}
session = requests.Session()
session.headers.update(HEADERS)

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
TG_ENABLED = bool(TG_TOKEN and TG_CHAT_IDS)
IN_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
tg_session = requests.Session()

BTN_CHECK = "🔍 Проверить сейчас"
BTN_STATUS = "ℹ️ Статус"
# Постоянные кнопки под полем ввода сообщения
MAIN_KEYBOARD = {"keyboard": [[{"text": BTN_CHECK}, {"text": BTN_STATUS}]],
                 "resize_keyboard": True, "is_persistent": True}
# Кнопки под сообщениями
BOOK_BUTTON = {"text": "📝 Записаться", "url": BOOKING_URL}
CHECK_KEYBOARD = {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "check"}, BOOK_BUTTON]]}
NEW_KEYBOARD = {"inline_keyboard": [[{"text": "🔍 Все окна", "callback_data": "check"}, BOOK_BUTTON]]}

STATE = {}  # state.json: уже виденные слоты, счётчик ошибок, позиция в очереди сообщений Telegram
RUNTIME = {"started": datetime.now(TZ), "checks": 0, "last_try": 0.0, "ends_at": None,
           "service_name": None, "last_error": "", "conflict_logged": False}


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def redact(e):
    """Текст ошибки без токена бота (ошибки requests содержат адрес запроса)."""
    s = str(e)
    return s.replace(TG_TOKEN, "***") if TG_TOKEN else s


def esc(s):
    return html.escape(str(s), quote=False)


# ---------------------- Telegram ----------------------

def tg(method, **params):
    """Вызов Telegram Bot API. Возвращает result или None, если не получилось."""
    if not TG_TOKEN:
        return None
    for attempt in range(3):
        try:
            r = tg_session.post(f"{TG_API}/{method}", json=params, timeout=30)
            data = r.json()
        except Exception as e:
            log(f"Telegram {method}: {redact(e)}")
            time.sleep(3)
            continue
        if data.get("ok"):
            return data.get("result")
        if r.status_code == 429:  # слишком часто — Telegram сам говорит, сколько подождать
            time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        log(f"Telegram {method}: {r.status_code} {redact(data.get('description'))[:300]}")
        return None
    return None


def send(text, chat_ids=None, markup=None):
    """Сообщение в Telegram (HTML). Длинное режется на части, кнопки — у последней части."""
    log("СООБЩЕНИЕ:\n" + html.unescape(re.sub(r"<[^>]+>", "", text)))
    if chat_ids is None:
        chat_ids = TG_CHAT_IDS
    if not (TG_ENABLED and chat_ids):
        return
    # Telegram ограничивает сообщение 4096 символами — режем по строкам
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for cid in chat_ids:
        for i, chunk in enumerate(chunks):
            params = {"chat_id": cid, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True}
            if markup and i == len(chunks) - 1:
                params["reply_markup"] = markup
            tg("sendMessage", **params)


def parse_update(u):
    """(chat_id, команда) из обновления Telegram; команда: check / status / help. None — пропустить."""
    if "callback_query" in u:  # кнопка под сообщением
        q = u["callback_query"]
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Проверяю…")
        chat = (q.get("message") or {}).get("chat") or {}
        cmd = q.get("data") if q.get("data") in ("check", "status") else None
    else:
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        text = (m.get("text") or "").strip().lower()
        word = text.split()[0].split("@")[0] if text else ""
        if word == "/check" or "провер" in text:
            cmd = "check"
        elif word == "/status" or "статус" in text:
            cmd = "status"
        elif chat.get("type") == "private":  # в группе на посторонние сообщения не отвечаем
            cmd = "help"
        else:
            cmd = None
    cid = str(chat.get("id", ""))
    if not cmd:
        return None
    if cid not in TG_CHAT_IDS:
        log(f"Команда из чужого чата {cid} — не отвечаю (если это свой — добавьте Id в TG_CHAT_ID).")
        return None
    log(f"Команда из Telegram: {cmd}")
    return cid, cmd


def poll_telegram(wait):
    """Ждать нажатий кнопок до wait секунд и выполнить их. True — была проверка по кнопке."""
    if not TG_ENABLED:
        time.sleep(wait)
        return False
    try:
        r = tg_session.post(f"{TG_API}/getUpdates", timeout=wait + 30, json={
            "offset": STATE.get("tg_offset", 0), "timeout": int(wait),
            "allowed_updates": ["message", "callback_query"]})
        data = r.json()
    except Exception as e:
        log(f"Telegram getUpdates: {redact(e)}")
        time.sleep(min(wait, 10))
        return False
    if not data.get("ok"):
        if r.status_code != 409:
            log(f"Telegram getUpdates: {r.status_code} {redact(data.get('description'))[:300]}")
        elif not RUNTIME["conflict_logged"]:
            RUNTIME["conflict_logged"] = True
            log("Telegram: этот бот запущен где-то ещё (409 Conflict) — кнопки могут отвечать "
                "через раз, а уведомления дублироваться. Оставьте одну копию (GitHub ИЛИ Railway).")
        time.sleep(min(wait, 10))
        return False
    updates = data.get("result") or []
    if not updates:
        return False
    STATE["tg_offset"] = updates[-1]["update_id"] + 1
    save_state()
    # Повторные нажатия подряд выполняем один раз
    todo = list(dict.fromkeys(p for p in map(parse_update, updates) if p))
    for chat, cmd in todo:
        if cmd == "status":
            send(status_text(), [chat])
        elif cmd == "help":
            send(help_text(), [chat], MAIN_KEYBOARD)
    check_chats = [chat for chat, cmd in todo if cmd == "check"]
    if check_chats:
        run_once(report_to=check_chats)
        return True
    return False


# ---------------------- Состояние ----------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state():
    if not TG_ENABLED:  # пробный запуск без Telegram ничего не запоминает
        return
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------- API ----------------------

def api_get(path, params=None):
    for attempt in range(3):
        try:
            r = session.get(API + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            err = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Сайт не ответил ({path}): {err}")


_names_cache = {"at": 0.0, "value": None}


def get_names():
    """id сотрудника -> имя; id услуги -> название (обновляется раз в час)."""
    if _names_cache["value"] and time.monotonic() - _names_cache["at"] < 3600:
        return _names_cache["value"]
    staff = {d["id"]: d["attributes"]["name"].strip() for d in api_get("/resources")["data"]}
    services = {d["id"]: d["attributes"]["name"].strip() for d in api_get("/services")["data"]}
    _names_cache.update(at=time.monotonic(), value=(staff, services))
    return staff, services


def get_free_slots(staff_names):
    """Множество строк 'Имя|YYYY-MM-DDTHH:MM' со всеми свободными окнами."""
    today = datetime.now(TZ).date()
    data = api_get("/availability/booking-slots", {
        "filter[from]": today.isoformat(),
        "filter[to]": (today + timedelta(days=DAYS_AHEAD)).isoformat(),
        "filter[serviceId]": SERVICE_ID,
        "ignoreBookingBoundaries": "0",
    })
    slots = set()
    for d in data.get("data", []):
        start = d.get("attributes", {}).get("start")
        if not start:
            continue
        rid = (((d.get("relationships") or {}).get("resource") or {}).get("data") or {}).get("id")
        name = staff_names.get(rid, "Сотрудник")
        if STAFF_FILTER and name.lower() not in STAFF_FILTER:
            continue
        dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
        slots.add(f"{name}|{dt:%Y-%m-%dT%H:%M}")
    return slots


# ---------------------- Форматирование ----------------------

def group(keys):
    by = {}
    for k in sorted(keys):
        name, ts = k.split("|", 1)
        d, t = ts.split("T")
        by.setdefault(name, {}).setdefault(d, []).append(t)
    return by


def day_label(d):
    dd = date.fromisoformat(d)
    return f"{WEEKDAYS[dd.weekday()]} {dd:%d.%m}"


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def okon(n):
    return f"{n} {plural(n, 'окно', 'окна', 'окон')}"


def service_name():
    return RUNTIME["service_name"] or "выбранная услуга"


def fmt_summary(keys):
    lines = []
    for name, days in group(keys).items():
        lines.append(f"👤 <b>{esc(name)}</b>")
        for d, times in days.items():
            lines.append(f"  {day_label(d)}: {okon(len(times))} ({times[0]}–{times[-1]})")
    return "\n".join(lines)


def fmt_new(new, prev):
    prev_days = {(k.split("|")[0], k.split("|")[1][:10]) for k in prev}
    lines = []
    for name, days in group(new).items():
        lines.append(f"👤 <b>{esc(name)}</b>")
        for d, times in days.items():
            mark = " 🆕 новый день" if (name, d) not in prev_days else ""
            if len(times) <= 12:
                lines.append(f"  {day_label(d)}{mark}: {', '.join(times)}")
            else:
                lines.append(f"  {day_label(d)}{mark}: {okon(len(times))} ({times[0]}–{times[-1]})")
    return "\n".join(lines)


def fmt_full(keys, new):
    """Все свободные окна по дням; появившиеся с прошлой проверки помечены 🆕."""
    lines = []
    for name, days in group(keys).items():
        lines += ["", f"👤 <b>{esc(name)}</b> — {okon(sum(map(len, days.values())))}"]
        for d, times in days.items():
            shown = " ".join(t + "🆕" if f"{name}|{d}T{t}" in new else t for t in times)
            lines.append(f"<b>{day_label(d)}</b> ({len(times)}): {shown}")
    return "\n".join(lines)


def ago(iso):
    try:
        t = datetime.fromisoformat(iso).astimezone(TZ)
    except Exception:
        return str(iso)
    sec = max(0, int((datetime.now(TZ) - t).total_seconds()))
    if sec < 90:
        a = f"{sec} сек назад"
    elif sec < 90 * 60:
        a = f"{sec // 60} мин назад"
    else:
        a = f"{sec // 3600} ч {sec % 3600 // 60} мин назад"
    return f"{t:%d.%m %H:%M:%S} МСК ({a})"


def help_text():
    return (f"👋 Слежу за свободными окнами записи в посольство Молдовы "
            f"(<b>{esc(service_name())}</b>) и сразу пишу сюда, когда появляются новые: "
            f"после чьей-то отмены или когда открывается новый день.\n\n"
            f"{BTN_CHECK} — все свободные окна прямо сейчас\n"
            f"{BTN_STATUS} — работает ли мониторинг")


def status_text():
    fails = STATE.get("fails", 0)
    lines = ["ℹ️ <b>Статус мониторинга</b>", ""]
    if fails:
        lines.append(f"⚠️ Сайт записи не отвечает: {fails} {plural(fails, 'ошибка', 'ошибки', 'ошибок')} подряд")
        if RUNTIME["last_error"]:
            lines.append(f"<i>{esc(RUNTIME['last_error'][:300])}</i>")
    else:
        lines.append("✅ Работает, сайт записи отвечает")
    if STATE.get("last_ok"):
        lines.append(f"Последняя успешная проверка: {ago(STATE['last_ok'])}")
    lines.append(f"Свободно сейчас: {okon(len(STATE.get('slots', [])))}")
    lines += ["",
              f"Услуга: {esc(service_name())}",
              f"Сотрудники: {esc(STAFF) if STAFF_FILTER else 'все'}",
              f"Проверка каждые {INTERVAL_MIN:g} мин",
              f"Где работает: {'GitHub Actions' if IN_GITHUB else 'сервер'}, "
              f"запущен {RUNTIME['started']:%d.%m %H:%M} МСК, проверок: {RUNTIME['checks']}"]
    if RUNTIME["ends_at"]:
        lines.append(f"Плановый перезапуск ≈ в {RUNTIME['ends_at']:%H:%M} МСК "
                     f"(около минуты без проверок — это нормально)")
    return "\n".join(lines)


# ---------------------- Проверка ----------------------

def run_once(report_to=None):
    """Одна проверка сайта. report_to — чаты, где нажали «Проверить сейчас»: им уходит полный список.
    Возвращает True, если сайт ответил."""
    RUNTIME["checks"] += 1
    RUNTIME["last_try"] = time.monotonic()
    first_run = "slots" not in STATE
    prev = set(STATE.get("slots", []))
    try:
        staff_names, services = get_names()
        RUNTIME["service_name"] = services.get(SERVICE_ID, SERVICE_ID)
        if first_run:
            log("Услуги на сайте (для настройки SERVICE_ID):\n" +
                "\n".join(f"  {sid}  {n}" for sid, n in services.items()))
        current = get_free_slots(staff_names)
    except Exception as e:
        traceback.print_exc()
        fails = STATE["fails"] = STATE.get("fails", 0) + 1
        RUNTIME["last_error"] = redact(e)
        if report_to:
            send(f"⚠️ Сайт записи сейчас не отвечает, попробуйте через минуту.\n"
                 f"<i>{esc(RUNTIME['last_error'][:300])}</i>", report_to, CHECK_KEYBOARD)
        if fails == 3 or fails % 100 == 0:
            send(f"⚠️ Мониторинг записи: сайт не отвечает "
                 f"({fails} {plural(fails, 'ошибка', 'ошибки', 'ошибок')} подряд).\n"
                 f"<i>{esc(RUNTIME['last_error'][:300])}</i>\n\nКак только заработает — напишу.")
            STATE["alerted"] = True
        save_state()
        return False

    # Было много окон и вдруг ноль — скорее всего сбой сайта. Если поверить сразу, то после сбоя
    # все окна придут как «новые». Поэтому ждём, пока ноль подтвердится 3 проверки подряд.
    empty = STATE.get("empty_count", 0)
    if not current and len(prev) >= 10 and empty < 2:
        STATE["empty_count"] = empty + 1
        save_state()
        log(f"Сайт вернул 0 окон, хотя было {len(prev)}, — похоже на сбой, перепроверю.")
        if report_to:
            send(f"🔍 Сайт сейчас показывает 0 свободных окон, хотя при прошлой проверке было "
                 f"{len(prev)}. Похоже на сбой сайта — перепроверю через минуту.",
                 report_to, CHECK_KEYBOARD)
        return True

    new = current - prev
    was_alerted = STATE.get("alerted")
    STATE.update(slots=sorted(current), fails=0, empty_count=0, alerted=False,
                 last_ok=datetime.now(TZ).isoformat(timespec="seconds"))
    log(f"{service_name()}: свободно {len(current)}, новых {len(new)}")

    if was_alerted:
        send("✅ Сайт записи снова отвечает, мониторинг продолжается.")
    if first_run:
        msg = f"✅ <b>Мониторинг запущен</b>: {esc(service_name())}\nСейчас свободно: {len(current)}\n"
        if current:
            msg += "\n" + fmt_summary(current) + "\n"
        msg += (f"\nДальше буду присылать только новые окна.\n"
                f"Кнопка «{BTN_CHECK}» внизу — все свободные окна в любой момент.")
        send(msg, markup=MAIN_KEYBOARD)
    elif new:
        others = [c for c in TG_CHAT_IDS if c not in (report_to or [])]
        if others:
            send(f"🟢 <b>Новые свободные окна ({len(new)})</b> — {esc(service_name())}\n\n"
                 f"{fmt_new(new, prev)}", others, NEW_KEYBOARD)
    if report_to:
        marked = set() if first_run else new
        msg = (f"🔍 <b>Свободно сейчас: {okon(len(current))}</b> — {esc(service_name())}\n"
               f"<i>проверено {datetime.now(TZ):%d.%m %H:%M} МСК</i>")
        if marked:
            msg += f"\n🆕 — новые с прошлой проверки ({len(marked)})"
        if current:
            msg += "\n" + fmt_full(current, marked)
        else:
            msg += "\n\nСвободных окон сейчас нет. Как только появятся — сразу напишу."
        send(msg, report_to, CHECK_KEYBOARD)
    save_state()
    return True


def git_save():
    """Сохранить state.json в репозиторий (только внутри GitHub Actions)."""
    if not (IN_GITHUB and TG_ENABLED):
        return
    cmds = [
        "git add state.json",
        "git diff --cached --quiet || git commit -q -m state",
        # подтянуть чужие правки (например, README); при конфликте по state.json побеждает наш, свежий
        "git pull -q --rebase -X theirs || git rebase --abort",
        "git push -q",
    ]
    for c in cmds:
        if subprocess.run(c, shell=True).returncode != 0 and c.startswith("git push"):
            log("Не удалось сохранить state.json в репозиторий (продолжаю работу).")


def safely(fn, *args):
    try:
        return fn(*args)
    except Exception:
        traceback.print_exc()
        time.sleep(5)


def main():
    STATE.update(load_state())
    args = sys.argv[1:]

    if not TG_ENABLED:
        log("Не заданы TG_TOKEN / TG_CHAT_ID — одна пробная проверка без Telegram, результат ниже.")
        if IN_GITHUB:
            print("::warning::Не заданы секреты TG_TOKEN и TG_CHAT_ID (Settings → Secrets and "
                  "variables → Actions). Сделана одна пробная проверка без Telegram — результат в логе.")
        sys.exit(0 if run_once(report_to=["log"]) else 1)

    # --for N  : проверять каждые INTERVAL_MIN минут в течение N минут (для GitHub Actions)
    # --loop   : проверять бесконечно (для своего сервера)
    # без ключей: одна проверка
    if "--for" in args:
        duration = float(args[args.index("--for") + 1]) * 60
    elif "--loop" in args:
        duration = None
    else:
        run_once()
        poll_telegram(0)  # ответить на нажатия, накопившиеся с прошлого запуска
        return

    tg("deleteWebhook")  # с включённым webhook бот не получает нажатия через getUpdates
    tg("setMyCommands", commands=[{"command": "check", "description": "Свободные окна прямо сейчас"},
                                  {"command": "status", "description": "Работает ли мониторинг"}])
    started = last_save = time.monotonic()
    deadline = started + duration if duration else None
    if duration:
        RUNTIME["ends_at"] = datetime.now(TZ) + timedelta(seconds=duration)
    next_check = started
    while True:
        if deadline and time.monotonic() >= deadline:
            break
        if time.monotonic() >= next_check:
            safely(run_once)
            next_check = time.monotonic() + INTERVAL_MIN * 60 + random.uniform(0, 10)
        if IN_GITHUB and time.monotonic() - last_save > 30 * 60:  # раз в 30 минут сохраняем состояние
            git_save()
            last_save = time.monotonic()
        # Между проверками ждём нажатий кнопок в Telegram — бот отвечает сразу
        until = min(next_check, deadline) if deadline else next_check
        if safely(poll_telegram, max(1, min(50, until - time.monotonic()))):
            next_check = time.monotonic() + INTERVAL_MIN * 60 + random.uniform(0, 10)

    log("Время работы вышло — завершаюсь (GitHub сразу запускает следующую смену).")
    tg("getUpdates", offset=STATE.get("tg_offset", 0), timeout=0)  # отметить обработанные нажатия
    save_state()
    git_save()


if __name__ == "__main__":
    main()
