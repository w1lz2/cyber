"""Проверка логики monitor.py без сети: API Reservio и Telegram подменяются.

Запуск: python test_monitor.py
"""
import os, sys, json, time, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(TG_TOKEN="123:SECRET", TG_CHAT_ID="111, 222", STATE_FILE=str(tmp / "state.json"),
                  INTERVAL_MIN="0.005")
os.environ.pop("GITHUB_ACTIONS", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import monitor as m

R1, R2 = "b6eae6ed-86ac-4ed7-9fe5-2482f0d0bcb9", "eb4e43fe-9b6d-4dfd-99ad-451ca81bb630"
API = {"slots": [], "fail": False}


def slot(rid, day, hm):
    return {"type": "timeSlot", "attributes": {"start": f"2026-11-{day}T{hm}:00+03:00"},
            "relationships": {"resource": {"data": {"id": rid, "type": "resource"}}}}


class Resp:
    def __init__(self, code, data):
        self.status_code, self._d, self.text, self.ok = code, data, json.dumps(data), code == 200

    def json(self):
        return self._d


def fake_get(url, params=None, timeout=None):
    if API["fail"]:
        raise ConnectionError("boom")
    if url.endswith("/resources"):
        return Resp(200, {"data": [{"id": R1, "attributes": {"name": "Владимир Руснак "}},
                                   {"id": R2, "attributes": {"name": "Дойна <Гайтур>"}}]})
    if url.endswith("/services"):
        return Resp(200, {"data": [{"id": m.SERVICE_ID, "attributes": {"name": "Изготовление паспорта (загран)"}}]})
    assert params["filter[serviceId]"] == m.SERVICE_ID
    return Resp(200, {"data": API["slots"]})


SENT, UPDATES, CALLS = [], [], []


def fake_post(url, json=None, timeout=None):
    method = url.rsplit("/", 1)[1]
    assert "/bot123:SECRET/" in url
    CALLS.append((method, json))
    if method == "sendMessage":
        assert len(json["text"]) <= 4096, len(json["text"])
        SENT.append(json)
        return Resp(200, {"ok": True, "result": {}})
    if method == "getUpdates":
        ups = [u for u in UPDATES if u["update_id"] >= json.get("offset", 0)]
        return Resp(200, {"ok": True, "result": ups})
    return Resp(200, {"ok": True, "result": True})


m.session.get = fake_get
m.tg_session.post = fake_post
m.time.sleep = lambda s: None
m.random.uniform = lambda a, b: 0


def reset():
    SENT.clear(); CALLS.clear()


def texts():
    return [(s["chat_id"], s["text"]) for s in SENT]


# 1. Первый запуск — сводка всем + клавиатура
API["slots"] = [slot(R1, "17", f"{h:02d}:{mm:02d}") for h in range(8, 16) for mm in (0, 15, 30, 45)] + \
               [slot(R2, "18", "09:00"), slot(R2, "18", "09:15")]
m.STATE.update(m.load_state())
assert m.run_once()
assert len(SENT) == 2 and {s["chat_id"] for s in SENT} == {"111", "222"}, texts()
assert "Мониторинг запущен" in SENT[0]["text"] and SENT[0]["reply_markup"] == m.MAIN_KEYBOARD
assert "Дойна &lt;Гайтур&gt;" in SENT[0]["text"]  # HTML экранирован
assert "Владимир Руснак</b>" in SENT[0]["text"]   # имя обрезано от пробелов
st = json.loads(Path(os.environ["STATE_FILE"]).read_text())
assert len(st["slots"]) == 34 and st["fails"] == 0
print("1 OK первый запуск")

# 2. Без изменений — тишина
reset(); assert m.run_once(); assert SENT == []
print("2 OK без изменений")

# 3. Новое окно (отмена) + новый день
reset()
API["slots"].append(slot(R2, "18", "10:00"))
API["slots"].append(slot(R2, "27", "08:00"))
assert m.run_once()
assert len(SENT) == 2
t = SENT[0]["text"]
assert "Новые свободные окна (2)" in t and "ср 18.11: 10:00" in t and "пт 27.11 🆕 новый день: 08:00" in t, t
assert SENT[0]["reply_markup"] == m.NEW_KEYBOARD
print("3 OK новые окна:\n" + t)

# 4. Окно заняли — тишина; освободилось снова — уведомление
reset(); API["slots"].pop(); assert m.run_once(); assert SENT == []
API["slots"].append(slot(R2, "27", "08:00")); assert m.run_once(); assert len(SENT) == 2
print("4 OK повторная отмена")

# 5. Кнопка «Проверить сейчас» из чата 111 + новое окно: 111 получает полный список с 🆕, 222 — уведомление
reset()
API["slots"].append(slot(R1, "19", "11:30"))
UPDATES[:] = [
    {"update_id": 500, "message": {"chat": {"id": 111, "type": "private"}, "text": m.BTN_CHECK}},
    {"update_id": 501, "message": {"chat": {"id": 111, "type": "private"}, "text": m.BTN_CHECK}},  # повтор
    {"update_id": 502, "message": {"chat": {"id": 999, "type": "private"}, "text": "/check"}},       # чужой
]
assert m.poll_telegram(5) is True
assert m.STATE["tg_offset"] == 503
by = {}
for cid, tx in texts():
    by.setdefault(cid, []).append(tx)
assert "999" not in by and len(by["111"]) == 1 and len(by["222"]) == 1, by
full = by["111"][0]
assert "Свободно сейчас: 37 окон" in full and "11:30🆕" in full and "новые с прошлой проверки (1)" in full, full
assert "Новые свободные окна (1)" in by["222"][0]
assert SENT[[s["chat_id"] for s in SENT].index("111")]["reply_markup"] == m.CHECK_KEYBOARD
print("5 OK кнопка проверки:\n" + full)

# 6. Повторный getUpdates не выполняет старые команды
reset(); assert m.poll_telegram(5) is False; assert SENT == []
print("6 OK offset")

# 7. Статус, inline-кнопка «Обновить», /check@bot, произвольный текст, сообщение в группе
reset()
UPDATES[:] = [
    {"update_id": 600, "message": {"chat": {"id": 222, "type": "private"}, "text": m.BTN_STATUS}},
    {"update_id": 601, "callback_query": {"id": "cb1", "data": "check", "message": {"chat": {"id": 222}}}},
    {"update_id": 602, "message": {"chat": {"id": 111, "type": "private"}, "text": "привет"}},
    {"update_id": 603, "message": {"chat": {"id": 111, "type": "private"}, "text": "/check@my_bot"}},
]
assert m.poll_telegram(5) is True
by = {}
for cid, tx in texts():
    by.setdefault(cid, []).append(tx)
assert any("Статус мониторинга" in x and "✅ Работает" in x for x in by["222"]), by
assert any("Свободно сейчас" in x for x in by["222"])
assert any("Слежу за свободными окнами" in x for x in by["111"])
assert any("Свободно сейчас" in x for x in by["111"])
assert sum(x.startswith("🔍") for x in by["111"] + by["222"]) == 2  # одна проверка на обоих
assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Проверяю…"}) in CALLS
print("7 OK статус/inline/help:\n" + [x for x in by["222"] if "Статус" in x][0])

# 8. Сбой сайта: 3 ошибки — предупреждение, кнопка во время сбоя, восстановление
reset(); API["fail"] = True
for _ in range(2):
    assert m.run_once() is False
assert SENT == []
assert m.run_once() is False
assert len(SENT) == 2 and "3 ошибки подряд" in SENT[0]["text"]
reset()
assert m.run_once(report_to=["111"]) is False
assert len(SENT) == 1 and "не отвечает" in SENT[0]["text"] and SENT[0]["chat_id"] == "111"
assert "SECRET" not in json.dumps(SENT)
reset(); API["fail"] = False; assert m.run_once()
assert len(SENT) == 2 and "снова отвечает" in SENT[0]["text"], texts()
print("8 OK сбои")

# 9. Сайт вдруг вернул 0 окон: 2 раза не верим, на 3-й принимаем; потом окна возвращаются
reset(); saved = API["slots"]; API["slots"] = []
assert m.run_once() and m.run_once()
assert len(m.STATE["slots"]) == 37 and SENT == []
API["slots"] = saved; assert m.run_once(); assert SENT == []  # сбой прошёл — без лавины уведомлений
API["slots"] = []
for _ in range(3):
    m.run_once()
assert m.STATE["slots"] == []
reset(); m.run_once(report_to=["111"])
assert "Свободных окон сейчас нет" in SENT[0]["text"]
print("9 OK защита от пустого ответа")

# 10. Длинное сообщение режется на части < 4096, кнопки только у последней
reset()
API["slots"] = [slot(R1 if i % 2 else R2, f"{17 + i // 60 % 13}", f"{8 + i % 60 // 6:02d}:{i % 6 * 10:02d}")
                for i in range(700)]
m.run_once(report_to=["111"])
parts = [s for s in SENT if s["chat_id"] == "111"]
assert len(parts) >= 2 and "reply_markup" in parts[-1] and all("reply_markup" not in p for p in parts[:-1])
print(f"10 OK длинный список: {len(parts)} части")

# 11. Цикл --for: проверки идут, кнопки обрабатываются, в конце подтверждается offset
reset(); UPDATES[:] = []
m.sys.argv = ["monitor.py", "--for", "0.02"]  # 1.2 сек
real_sleep = time.sleep
orig_post = m.tg_session.post
def slow_post(url, json=None, timeout=None):
    if url.endswith("getUpdates") and json.get("timeout"):
        real_sleep(0.05)
    return orig_post(url, json=json, timeout=timeout)
m.tg_session.post = slow_post
checks0 = m.RUNTIME["checks"]
t0 = time.monotonic(); m.main(); dt = time.monotonic() - t0
methods = [c[0] for c in CALLS]
assert methods[:2] == ["deleteWebhook", "setMyCommands"] and methods[-1] == "getUpdates"
assert 1.0 < dt < 3, dt
assert m.RUNTIME["checks"] - checks0 >= 2, m.RUNTIME["checks"] - checks0
print(f"11 OK цикл: {m.RUNTIME['checks'] - checks0} проверок за {dt:.1f} c")
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
