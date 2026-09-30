#!/usr/bin/env python3
"""
Мониторинг сессий TEF/TCF на сайте Alliance Française d'Edmonton.

Шлёт сообщения в Telegram, когда:
  • в таблице появилась новая сессия (с окном регистрации);
  • до открытия окна регистрации осталось ~30 минут;
  • окно регистрации открылось;
  • в распроданной сессии снова появились места;
  • страница с расписанием изменилась непонятным образом (на случай смены вёрстки).

Все события пишутся в events.csv — по нему потом видно, когда центр публикует даты.
Скрипт читает только публичные страницы с расписанием и систему записи не трогает.
"""
import csv
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Edmonton")
PAGES = {
    "TEF": "https://www.afedmonton.com/en/exams/tef/",
    "TCF": "https://www.afedmonton.com/en/exams/tcf/",
}
STATE_FILE = Path("state.json")
EVENTS_FILE = Path("events.csv")
REMIND_BEFORE = timedelta(minutes=35)
HEADERS = {"User-Agent": "Mozilla/5.0 (personal exam-date monitor, one request per 5 min)"}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

TITLE_RE = re.compile(r"\b(T(?:EF|CF)\b[^|]{0,100}?)\s+Written\b")
SCHED_RE = re.compile(r"Written\s+(.+?)\s+Oral\b")
DT_RE = re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4}),? (\d{1,2}):(\d{2}) ?([ap]m)", re.I)
SPOTS_RE = re.compile(r"(\d+)\s+\$\s?\d")


# ---------- Telegram ----------

def tg(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[Telegram не настроен, сообщение]\n" + text + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


# ---------- разбор страницы ----------

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").splitlines()]
    return [l for l in lines if l]


def cut_section(lines):
    """Блок между 'Step 2' (даты) и 'Step 3' (подтверждение)."""
    s = next((i for i, l in enumerate(lines) if "Step 2" in l), None)
    e = next((i for i, l in enumerate(lines) if "Step 3" in l and s is not None and i > s), None)
    if s is None or e is None:
        return lines, False
    return lines[s:e], True


def to_dt(m):
    mon, day, year, hh, mm, ap = m
    month = MONTHS.get(mon[:3].lower())
    if not month:
        return None
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    return datetime(int(year), month, int(day), hour, int(mm), tzinfo=TZ)


def parse_sessions(text):
    sessions = {}
    matches = list(TITLE_RE.finditer(text))
    for i, m in enumerate(matches):
        block = text[m.start(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        title = m.group(1).strip()
        sched = SCHED_RE.search(block)
        schedule = sched.group(1).strip(" •") if sched else ""
        dts = [d for d in (to_dt(x) for x in DT_RE.findall(block)) if d]
        if "SOLD OUT" in block.upper():
            status = "sold_out"
        elif re.search(r"\bClosed\b", block):
            status = "closed"
        else:
            status = "available"
        spots = SPOTS_RE.search(block)
        sessions[f"{title} | {schedule}"] = {
            "title": title,
            "schedule": schedule,
            "start": dts[0].isoformat() if len(dts) > 0 else None,
            "end": dts[1].isoformat() if len(dts) > 1 else None,
            "status": status,
            "spots": int(spots.group(1)) if spots else None,
        }
    return sessions


# ---------- вспомогательное ----------

def iso(s):
    return datetime.fromisoformat(s) if s else None


def fmt(dt):
    return f"{WEEKDAYS[dt.weekday()]} {dt:%d.%m %H:%M}"


def window_str(s):
    start, end = iso(s["start"]), iso(s["end"])
    if not start:
        return "окно регистрации не указано"
    return fmt(start) + (f"–{end:%H:%M}" if end else "") + " (время как на сайте)"


def human(delta):
    mins = int(delta.total_seconds() // 60)
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    parts = [f"{d} д" if d else "", f"{h} ч" if h else "", f"{m} мин" if m and not d else ""]
    return " ".join(p for p in parts if p) or "меньше минуты"


def window_state(s, now):
    start, end = iso(s["start"]), iso(s["end"])
    if not start:
        return "unknown"
    if now < start:
        return "future"
    if now <= (end or start + timedelta(hours=3)):
        return "open"
    return "past"


def log_event(now, exam, event, s):
    new_file = not EVENTS_FILE.exists()
    with EVENTS_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["logged_at", "exam", "event", "title", "schedule",
                        "window_start", "window_end", "status", "spots"])
        w.writerow([now.strftime("%Y-%m-%d %H:%M"), exam, event, s["title"], s["schedule"],
                    s["start"] or "", s["end"] or "", s["status"],
                    "" if s["spots"] is None else s["spots"]])


# ---------- основная проверка ----------

def check_page(exam, url, ps, now, first_run):
    msgs = []
    lines = fetch(url)
    sec, found = cut_section(lines)
    if not found:
        # не трогаем сохранённые сессии, иначе после починки сайта всё придёт как «новое»
        if not ps.get("warned_layout"):
            ps["warned_layout"] = True
            msgs.append(f"⚠️ {exam}: не нашёл на странице блок с датами (Step 2 … Step 3). "
                        f"Похоже, сайт поменял вёрстку — проверь вручную:\n{url}")
        return msgs
    sessions = parse_sessions(" ".join(sec))
    old = ps.get("sessions", {})
    explained = False

    for key, s in sessions.items():
        prev = old.get(key)
        ws = window_state(s, now)
        start = iso(s["start"])

        if prev is None:
            # новая сессия (или первый запуск)
            s["reminded"] = ws in ("open", "past") or (start is not None and start <= now + REMIND_BEFORE)
            s["open_alerted"] = ws in ("open", "past")
            log_event(now, exam, "initial" if first_run else "new", s)
            if first_run:
                continue
            explained = True
            text = f"🆕 {exam}: новая сессия\n{s['title']}\nЭкзамен: {s['schedule']}\nЗапись: {window_str(s)}\n"
            if ws == "open" and s["status"] != "sold_out":
                text += "🔥 Запись идёт ПРЯМО СЕЙЧАС!\n"
            elif ws == "future":
                text += f"Откроется через {human(start - now)}. Напомню примерно за 30 минут.\n"
            elif s["status"] == "sold_out":
                text += "Уже распродано.\n"
            msgs.append(text + url)
            continue

        s["reminded"] = prev.get("reminded", False)
        s["open_alerted"] = prev.get("open_alerted", False)

        if prev.get("status") != s["status"]:
            explained = True
            log_event(now, exam, s["status"], s)
            if prev.get("status") == "sold_out" and s["status"] != "sold_out":
                msgs.append(f"🔄 {exam}: снова появились места!\n{s['title']}\nЭкзамен: {s['schedule']}\n{url}")

        if not s["reminded"] and ws == "future" and start <= now + REMIND_BEFORE:
            s["reminded"] = True
            log_event(now, exam, "reminder", s)
            msgs.append(f"⏰ {exam}: через ~{human(start - now)} открывается запись\n{s['title']}\n"
                        f"Экзамен: {s['schedule']}\nЗапись: {window_str(s)}\nЗалогинься заранее 👉 {url}")

        if not s["open_alerted"] and ws == "open" and s["status"] != "sold_out":
            s["open_alerted"] = True
            log_event(now, exam, "window_open", s)
            msgs.append(f"🟢 {exam}: окно регистрации ОТКРЫТО\n{s['title']}\n"
                        f"Экзамен: {s['schedule']}\nЗапись: {window_str(s)}\n{url}")

    for key, s in old.items():
        if key not in sessions:
            explained = True
            log_event(now, exam, "removed", s)

    # страховка: изменения, которые разбор не распознал (например, сайт поменял формат таблицы)
    norm = [l for l in sec if not re.fullmatch(r"\d+", l)]  # счётчик мест не считаем изменением
    h = hashlib.sha256("\n".join(norm).encode()).hexdigest()
    if not first_run and ps.get("hash") and h != ps["hash"] and not explained:
        added = [l[2:] for l in difflib.ndiff(ps.get("lines", []), norm) if l.startswith("+ ")][:8]
        if added:
            msgs.append(f"ℹ️ {exam}: на странице с расписанием что-то изменилось:\n"
                        + "\n".join("• " + a[:150] for a in added) + f"\n{url}")
    ps["hash"], ps["lines"], ps["warned_layout"] = h, norm, False

    if first_run:
        upcoming = sorted((s for s in sessions.values() if window_state(s, now) in ("future", "open")),
                          key=lambda s: s["start"])
        text = f"✅ {exam}: мониторинг запущен, в таблице сейчас {len(sessions)} сесс."
        if upcoming:
            text += "\nБлижайшие окна записи:\n" + "\n".join(
                f"• {s['title']} — {window_str(s)}" for s in upcoming[:6])
        else:
            text += "\nОткрытых или предстоящих окон записи сейчас нет — напишу, как появятся."
        msgs.append(text)

    ps["sessions"] = sessions
    return msgs


def main(now=None):
    if "--test" in sys.argv:
        tg("✅ Тест: бот работает, уведомления о TEF/TCF в Эдмонтоне будут приходить сюда.")
        return

    now = now or datetime.now(TZ)
    exams = [e.strip().upper() for e in (os.environ.get("EXAMS") or "TEF,TCF").split(",") if e.strip()]
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    pages = state.setdefault("pages", {})
    outbox = []

    for exam in exams:
        url = PAGES.get(exam)
        if not url:
            print(f"Неизвестный экзамен: {exam} (доступны: {', '.join(PAGES)})")
            continue
        ps = pages.setdefault(exam, {})
        try:
            outbox += check_page(exam, url, ps, now, first_run="sessions" not in ps)
            ps["fail_count"] = 0
        except requests.RequestException as e:
            ps["fail_count"] = ps.get("fail_count", 0) + 1
            print(f"{exam}: ошибка загрузки ({e})")
            if ps["fail_count"] == 12:  # ~час подряд
                outbox.append(f"⚠️ {exam}: сайт не отвечает уже около часа. Проверь вручную:\n{url}")

    # сначала отправляем, потом сохраняем: если Telegram упадёт, в следующий раз сообщения уйдут повторно
    for m in outbox:
        tg(m)
    state["last_run_date"] = now.date().isoformat()  # ежедневный коммит держит репозиторий «активным»
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
