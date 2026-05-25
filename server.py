import os, sys, json, urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI()
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]
BOT_ID = os.environ["BITRIX24_BOT_ID"]
CLIENT_ID = os.environ["BITRIX24_CLIENT_ID"]
chat_histories = {}
menu_shown = set()

# ID сотрудников, которым доступна управленческая аналитика
MANAGER_IDS = {"9503", "9355"}

OVERDUE_WINDOW_DAYS = 90
UPCOMING_WINDOW_DAYS = 7

# Самая ранняя дата задачи, которую показываем в списках
MIN_TASK_DATE = datetime(2025, 1, 1)

# Облегчённый режим: максимум страниц задач (50 на страницу)
MAX_TASK_PAGES = 6
B24_TIMEOUT = 25
# Размер одной порции при отправке длинного списка частями
CHUNK_SIZE = 30

REPORT_MENU = (
    "Доступные отчёты:\n"
    "1 — Общая сводка по задачам\n"
    "2 — Скоро дедлайн (ближайшие 7 дней)\n"
    "3 — Просроченные задачи (за 3 месяца)\n"
    "4 — Все активные задачи\n"
    "5 — Задачи без срока\n\n"
    "Напишите номер отчёта."
)

BASE_PROMPT = "You are a helpful AI assistant for a company in Bitrix24. Always respond in Russian language. Help with: 1) creating and tracking tasks 2) answering employee questions 3) analyzing reports. For tasks use: <action>{\"type\":\"create_task\",\"title\":\"...\",\"description\":\"...\",\"deadline\":\"YYYY-MM-DD\"}</action> For task list: <action>{\"type\":\"get_tasks\"}</action>"

MANAGER_PROMPT = " This user is a manager. You may also use these analytics actions: <action>{\"type\":\"report_menu\"}</action> when the user asks for the list of reports or a menu; <action>{\"type\":\"summary\"}</action> for an overall summary; <action>{\"type\":\"upcoming_tasks\"}</action> for tasks with a deadline within the next 7 days; <action>{\"type\":\"overdue_tasks\"}</action> for overdue tasks from the last 3 months; <action>{\"type\":\"all_active\"}</action> for the list of all active tasks; <action>{\"type\":\"no_deadline\"}</action> for tasks without a deadline."

WEEKDAYS_RU = [
    "\u043f\u043e\u043d\u0435\u0434\u0435\u043b\u044c\u043d\u0438\u043a",
    "\u0432\u0442\u043e\u0440\u043d\u0438\u043a",
    "\u0441\u0440\u0435\u0434\u0430",
    "\u0447\u0435\u0442\u0432\u0435\u0440\u0433",
    "\u043f\u044f\u0442\u043d\u0438\u0446\u0430",
    "\u0441\u0443\u0431\u0431\u043e\u0442\u0430",
    "\u0432\u043e\u0441\u043a\u0440\u0435\u0441\u0435\u043d\u044c\u0435",
]


def build_system_prompt(is_manager):
    today = datetime.now()
    lines = ["", "", "Today is " + today.strftime("%Y-%m-%d") + " (" + WEEKDAYS_RU[today.weekday()] + ")."]
    lines.append("Upcoming dates for reference:")
    for i in range(1, 15):
        d = today + timedelta(days=i)
        lines.append("  " + WEEKDAYS_RU[d.weekday()] + ": " + d.strftime("%Y-%m-%d"))
    lines.append("When the user mentions a weekday or a relative day, use the matching date from this list. Never invent dates and never use dates in the past.")
    prompt = BASE_PROMPT + "\n".join(lines)
    if is_manager:
        prompt += MANAGER_PROMPT
    return prompt


def log(msg):
    try:
        sys.stderr.buffer.write((str(msg) + "\n").encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    except Exception:
        pass


def safe_text(s):
    return s.encode("utf-8", errors="ignore").decode("utf-8")


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except Exception:
        return None


def b24_call(url, method, params):
    full_url = url + method
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        full_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=B24_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"B24 HTTP {e.code} on {method}: {body}")
        try:
            return json.loads(body)
        except Exception:
            return {"error": e.code, "error_description": body}


def send_msg(dialog_id, text):
    try:
        params = {
            "BOT_ID": BOT_ID,
            "CLIENT_ID": CLIENT_ID,
            "DIALOG_ID": dialog_id,
            "MESSAGE": safe_text(text),
        }
        result = b24_call(B24_WEBHOOK, "imbot.message.add", params)
        log(f"Sent: {result}")
    except Exception as e:
        log(f"send_msg failed: {e}")


def send_long(dialog_id, header, lines, footer=""):
    """Отправляет длинный список частями по CHUNK_SIZE строк."""
    if not lines:
        send_msg(dialog_id, header + ("\n" + footer if footer else ""))
        return
    total = len(lines)
    parts = [lines[i:i + CHUNK_SIZE] for i in range(0, total, CHUNK_SIZE)]
    for idx, part in enumerate(parts, 1):
        head = header if idx == 1 else f"{header} (продолжение {idx}/{len(parts)})"
        body = head + "\n" + "\n".join(part)
        if idx == len(parts) and footer:
            body += "\n" + footer
        send_msg(dialog_id, body)


def get_user_names(user_ids):
    names = {}
    ids = [str(u) for u in user_ids if str(u).isdigit()]
    if not ids:
        return names
    try:
        r = b24_call(B24_WEBHOOK, "user.get", {"ID": ids})
        for u in r.get("result", []) or []:
            uid = str(u.get("ID", ""))
            full = (str(u.get("NAME", "")) + " " + str(u.get("LAST_NAME", ""))).strip()
            names[uid] = full if full else ("ID " + uid)
    except Exception as e:
        log(f"get_user_names err: {e}")
    return names


def fetch_tasks_limited(extra_filter):
    """Облегчённая загрузка задач, включая подзадачи (SUBTASKS=Y).
    Возвращает (список задач, было_ли_усечение)."""
    collected = []
    start = 0
    truncated = False
    for page in range(MAX_TASK_PAGES):
        params = {
            "filter": extra_filter,
            "select": ["ID", "TITLE", "DEADLINE", "RESPONSIBLE_ID", "STATUS", "PARENT_ID"],
            "order": {"DEADLINE": "ASC"},
            "start": start,
            "SUBTASKS": "Y",
        }
        try:
            r = b24_call(B24_WEBHOOK, "tasks.task.list", params)
        except Exception as e:
            log(f"fetch_tasks_limited err on page {page}: {e}")
            truncated = True
            break
        batch = r.get("result", {}).get("tasks", []) or []
        collected.extend(batch)
        nxt = r.get("next")
        if not nxt:
            break
        start = nxt
        if page == MAX_TASK_PAGES - 1:
            truncated = True
    return collected, truncated


def trunc_note(truncated):
    if truncated:
        return "(Показаны не все задачи — данных в портале много. Для полного отчёта обратитесь к администратору.)"
    return ""


def parent_label(t):
    pid = str(t.get("parentId") or "")
    if pid and pid not in ("0", "None", ""):
        return f" (в составе задачи #{pid})"
    return ""


def task_line(t, names, suffix):
    rid = str(t.get("responsibleId", ""))
    who = names.get(rid, "ID " + rid)
    extra = (" — " + suffix) if suffix else ""
    return f"- [{t.get('id')}] {t.get('title')} — {who}{extra}{parent_label(t)}"


def action_summary(dialog_id):
    today = datetime.now()
    today_day = datetime(today.year, today.month, today.day)
    overdue_start = today - timedelta(days=OVERDUE_WINDOW_DAYS)
    upcoming_end = today_day + timedelta(days=UPCOMING_WINDOW_DAYS)

    all_active, truncated = fetch_tasks_limited({"!STATUS": "5"})

    total = len(all_active)
    subtasks = sum(1 for t in all_active if parent_label(t))
    overdue = upcoming = no_deadline = 0
    for t in all_active:
        d = parse_date(t.get("deadline"))
        if d is None:
            no_deadline += 1
            continue
        if overdue_start <= d < today:
            overdue += 1
        elif today_day <= d <= upcoming_end:
            upcoming += 1

    lines = ["ОБЩАЯ СВОДКА ПО ЗАДАЧАМ", ""]
    lines.append(f"- Всего активных задач: {total}")
    lines.append(f"  из них подзадач: {subtasks}")
    lines.append(f"- Просрочено (за 3 месяца): {overdue}")
    lines.append(f"- Скоро дедлайн (7 дней): {upcoming}")
    lines.append(f"- Без срока: {no_deadline}")
    lines.append("")
    lines.append("Для детальных списков выберите отчёт 2-5 в меню.")
    note = trunc_note(truncated)
    if note:
        lines.append(note)
    send_msg(dialog_id, "\n".join(lines))


def action_upcoming_tasks(dialog_id):
    today = datetime.now()
    today_day = datetime(today.year, today.month, today.day)
    window_end = today_day + timedelta(days=UPCOMING_WINDOW_DAYS)

    all_active, truncated = fetch_tasks_limited({"!STATUS": "5"})

    upcoming = []
    for t in all_active:
        d = parse_date(t.get("deadline"))
        if d is None:
            continue
        if today_day <= d <= window_end:
            upcoming.append((t, d))
    # от поздних к ранним
    upcoming.sort(key=lambda x: x[1], reverse=True)

    if not upcoming:
        send_msg(dialog_id, "На ближайшие 7 дней задач с дедлайном нет.")
        return

    names = get_user_names({str(t.get("responsibleId")) for t, _ in upcoming if t.get("responsibleId")})
    lines = []
    for t, d in upcoming:
        days_left = (d - today_day).days
        when = "сегодня" if days_left == 0 else ("завтра" if days_left == 1 else f"через {days_left} дн.")
        lines.append(task_line(t, names, f"срок {d.strftime('%Y-%m-%d')} ({when})"))
    send_long(dialog_id, f"Скоро дедлайн (ближайшие 7 дней) — {len(upcoming)} задач:", lines, trunc_note(truncated))


def action_overdue_tasks(dialog_id):
    today = datetime.now()
    window_start = today - timedelta(days=OVERDUE_WINDOW_DAYS)

    all_active, truncated = fetch_tasks_limited({"!STATUS": "5"})

    overdue = []
    for t in all_active:
        d = parse_date(t.get("deadline"))
        if d is None:
            continue
        if window_start <= d < today:
            overdue.append((t, d))
    overdue.sort(key=lambda x: x[1], reverse=True)

    if not overdue:
        send_msg(dialog_id, "Просроченных задач за последние 3 месяца нет.")
        return

    names = get_user_names({str(t.get("responsibleId")) for t, _ in overdue if t.get("responsibleId")})
    lines = []
    for t, d in overdue:
        days_late = (today - d).days
        lines.append(task_line(t, names, f"срок {d.strftime('%Y-%m-%d')}, просрочено на {days_late} дн."))
    send_long(dialog_id, f"Просроченные задачи (за 3 месяца) — {len(overdue)} задач:", lines, trunc_note(truncated))


def action_all_active(dialog_id):
    all_active, truncated = fetch_tasks_limited({"!STATUS": "5"})

    # только задачи с дедлайном с MIN_TASK_DATE и новее
    dated = []
    for t in all_active:
        d = parse_date(t.get("deadline"))
        if d is not None and d >= MIN_TASK_DATE:
            dated.append((t, d))
    dated.sort(key=lambda x: x[1], reverse=True)

    if not dated:
        send_msg(dialog_id, "Активных задач с 2025 года нет.")
        return

    names = get_user_names({str(t.get("responsibleId")) for t, _ in dated if t.get("responsibleId")})
    lines = [task_line(t, names, "срок " + d.strftime("%Y-%m-%d")) for t, d in dated]
    send_long(dialog_id, f"Все активные задачи (с 2025 года) — {len(dated)} задач:", lines, trunc_note(truncated))


def action_no_deadline(dialog_id):
    all_active, truncated = fetch_tasks_limited({"!STATUS": "5"})
    no_dl = [t for t in all_active if parse_date(t.get("deadline")) is None]

    if not no_dl:
        send_msg(dialog_id, "Задач без срока нет.")
        return

    names = get_user_names({str(t.get("responsibleId")) for t in no_dl if t.get("responsibleId")})
    lines = [task_line(t, names, "") for t in no_dl]
    send_long(dialog_id, f"Задачи без срока — {len(no_dl)} задач:", lines, trunc_note(truncated))


def run_report(report_type, dialog_id):
    if report_type == "summary":
        action_summary(dialog_id)
    elif report_type == "upcoming_tasks":
        action_upcoming_tasks(dialog_id)
    elif report_type == "overdue_tasks":
        action_overdue_tasks(dialog_id)
    elif report_type == "all_active":
        action_all_active(dialog_id)
    elif report_type == "no_deadline":
        action_no_deadline(dialog_id)
    else:
        send_msg(dialog_id, "[!] Неизвестный отчёт")


MENU_CHOICES = {
    "1": "summary",
    "2": "upcoming_tasks",
    "3": "overdue_tasks",
    "4": "all_active",
    "5": "no_deadline",
}


def do_action(action_json, responsible_id, is_manager, dialog_id):
    """Возвращает текст для дописывания к ответу, либо None если отчёт
    уже отправлен напрямую (отчёты шлются частями сами)."""
    try:
        a = json.loads(action_json)
        atype = a.get("type")

        if atype == "create_task":
            fields = {
                "TITLE": a.get("title", "Task"),
                "DESCRIPTION": a.get("description", ""),
            }
            if responsible_id:
                fields["RESPONSIBLE_ID"] = responsible_id
            if a.get("deadline"):
                fields["DEADLINE"] = a["deadline"]
            r = b24_call(B24_WEBHOOK, "tasks.task.add", {"fields": fields})
            tid = r.get("result", {}).get("task", {}).get("id")
            if tid:
                return f"\n[OK] Задача создана (ID: {tid})"
            err = r.get("error_description") or r.get("error") or "неизвестная ошибка"
            log(f"task.add failed: {r}")
            return f"\n[!] Не удалось создать задачу: {err}"

        if atype == "get_tasks":
            r = b24_call(
                B24_WEBHOOK,
                "tasks.task.list",
                {
                    "filter": {"STATUS": "2"},
                    "select": ["ID", "TITLE", "DEADLINE"],
                    "order": {"DEADLINE": "ASC"},
                },
            )
            tasks = r.get("result", {}).get("tasks", []) or []
            if not tasks:
                return "\nНет активных задач"
            lines = ["\nЗадачи:"]
            for t in tasks[:10]:
                lines.append(f"- [{t['id']}] {t['title']}")
            return "\n".join(lines)

        if atype == "report_menu":
            if not is_manager:
                return "\n[Доступ ограничен] Эта информация доступна только руководителям"
            return "\n" + REPORT_MENU

        if atype in ("summary", "upcoming_tasks", "overdue_tasks", "all_active", "no_deadline"):
            if not is_manager:
                return "\n[Доступ ограничен] Эта информация доступна только руководителям"
            run_report(atype, dialog_id)
            return None

    except Exception as e:
        log(f"Action err: {e}")
        return "\n[!] Ошибка при выполнении действия"
    return ""


def process_message(uid, text, dialog_id):
    """Тяжёлая обработка — в фоне, Bitrix её не ждёт."""
    try:
        is_manager = str(uid) in MANAGER_IDS
        stripped = text.strip()

        if is_manager and uid in menu_shown and stripped in MENU_CHOICES:
            menu_shown.discard(uid)
            send_msg(dialog_id, "Готовлю отчёт, несколько секунд...")
            run_report(MENU_CHOICES[stripped], dialog_id)
            return

        if uid not in chat_histories:
            chat_histories[uid] = []
        hist = chat_histories[uid]
        hist.append({"role": "user", "content": text})
        if len(hist) > 20:
            chat_histories[uid] = hist[-20:]
            hist = chat_histories[uid]

        resp = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=build_system_prompt(is_manager),
            messages=hist,
        )
        reply = resp.content[0].text
        hist.append({"role": "assistant", "content": reply})

        if "<action>" in reply and "</action>" in reply:
            s = reply.index("<action>") + 8
            e_idx = reply.index("</action>")
            action_body = reply[s:e_idx]
            text_before = reply[:reply.index("<action>")].strip()
            extra = do_action(action_body, uid, is_manager, dialog_id)
            if '"report_menu"' in action_body and is_manager:
                menu_shown.add(uid)
            if extra is None:
                # отчёт уже отправлен частями; шлём только вступление, если оно было
                if text_before:
                    send_msg(dialog_id, text_before)
            else:
                rest = reply[e_idx + 9:]
                send_msg(dialog_id, (text_before + rest).strip() + extra)
        else:
            send_msg(dialog_id, reply.strip())
    except Exception as ex:
        log(f"process_message err: {ex}")
        try:
            send_msg(dialog_id, "Ошибка. Повторите запрос.")
        except Exception:
            pass


@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook/bitrix")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = dict(await request.form())
        if data.get("event") == "ONIMBOTMESSAGEADD":
            uid = data.get("data[USER][ID]", "?")
            text = data.get("data[PARAMS][MESSAGE]", "")
            dlg = data.get("data[PARAMS][DIALOG_ID]", "")
            log(f"Msg uid={uid} dlg={dlg} text={text[:20]}")
            if text and dlg:
                background_tasks.add_task(process_message, uid, text, dlg)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        log(f"Webhook err: {e}")
        return JSONResponse({"status": "error"}, status_code=500)
