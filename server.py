import os, sys, json, urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI()
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]
BOT_ID = os.environ["BITRIX24_BOT_ID"]
CLIENT_ID = os.environ["BITRIX24_CLIENT_ID"]
chat_histories = {}

# ID сотрудников, которым доступна управленческая аналитика
MANAGER_IDS = {"9503", "9335"}

# За сколько дней назад показывать просроченные задачи
OVERDUE_WINDOW_DAYS = 90

BASE_PROMPT = "You are a helpful AI assistant for a company in Bitrix24. Always respond in Russian language. Help with: 1) creating and tracking tasks 2) answering employee questions 3) analyzing reports. For tasks use: <action>{\"type\":\"create_task\",\"title\":\"...\",\"description\":\"...\",\"deadline\":\"YYYY-MM-DD\"}</action> For task list: <action>{\"type\":\"get_tasks\"}</action>"

MANAGER_PROMPT = " This user is a manager. You may also use these analytics actions: <action>{\"type\":\"overdue_tasks\"}</action> to show overdue tasks from the last 3 months with a summary, and <action>{\"type\":\"workload\"}</action> to show how many active tasks each employee has."

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
    """Убирает битые суррогатные символы, чтобы текст всегда кодировался в UTF-8."""
    return s.encode("utf-8", errors="ignore").decode("utf-8")


def parse_date(s):
    """Парсит дату из строки Bitrix (берёт первые 10 символов YYYY-MM-DD)."""
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
        with urllib.request.urlopen(req, timeout=20) as r:
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


def get_user_names(user_ids):
    """Возвращает словарь {id: 'Имя Фамилия'} для переданных ID."""
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


def fetch_all_tasks(extra_filter):
    """Загружает задачи с постраничным проходом (Bitrix отдаёт по 50)."""
    collected = []
    start = 0
    for _ in range(20):
        r = b24_call(
            B24_WEBHOOK,
            "tasks.task.list",
            {
                "filter": extra_filter,
                "select": ["ID", "TITLE", "DEADLINE", "RESPONSIBLE_ID", "STATUS"],
                "order": {"DEADLINE": "ASC"},
                "start": start,
            },
        )
        batch = r.get("result", {}).get("tasks", []) or []
        collected.extend(batch)
        nxt = r.get("next")
        if not nxt:
            break
        start = nxt
    return collected


def action_overdue_tasks():
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    window_start = today - timedelta(days=OVERDUE_WINDOW_DAYS)

    # Все активные (не закрытые) задачи
    all_active = fetch_all_tasks({"!STATUS": "5"})
    total_active = len(all_active)

    # Просроченные: дедлайн в прошлом и попадает в окно последних 3 месяцев
    overdue = []
    for t in all_active:
        d = parse_date(t.get("deadline"))
        if d is None:
            continue
        if window_start <= d < today:
            overdue.append((t, d))
    overdue.sort(key=lambda x: x[1])

    lines = ["Сводка по задачам:"]
    lines.append(f"- Всего активных задач: {total_active}")
    lines.append(f"- Просрочено за последние 3 месяца: {len(overdue)}")

    if not overdue:
        lines.append("")
        lines.append("Просроченных задач за последние 3 месяца нет.")
        return "\n" + "\n".join(lines)

    resp_ids = {str(t.get("responsibleId")) for t, _ in overdue if t.get("responsibleId")}
    names = get_user_names(resp_ids)

    lines.append("")
    lines.append("Просроченные задачи (за 3 месяца):")
    for t, d in overdue[:30]:
        rid = str(t.get("responsibleId", ""))
        who = names.get(rid, "ID " + rid)
        days_late = (today - d).days
        dl = d.strftime("%Y-%m-%d")
        lines.append(f"- [{t.get('id')}] {t.get('title')} — {who}, срок {dl}, просрочено на {days_late} дн.")
    if len(overdue) > 30:
        lines.append(f"... и ещё {len(overdue) - 30}")
    return "\n" + "\n".join(lines)


def action_workload():
    tasks = fetch_all_tasks({"!STATUS": "5"})
    counts = {}
    for t in tasks:
        rid = str(t.get("responsibleId", ""))
        if rid and rid != "None":
            counts[rid] = counts.get(rid, 0) + 1
    if not counts:
        return "\nНет активных задач"
    names = get_user_names(counts.keys())
    ordered = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    lines = ["\nЗагрузка по сотрудникам:"]
    for rid, cnt in ordered[:25]:
        who = names.get(rid, "ID " + rid)
        lines.append(f"- {who}: {cnt} активных")
    return "\n".join(lines)


def do_action(action_json, responsible_id, is_manager):
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

        if atype in ("overdue_tasks", "workload"):
            if not is_manager:
                return "\n[Доступ ограничен] Эта информация доступна только руководителям"
            if atype == "overdue_tasks":
                return action_overdue_tasks()
            return action_workload()

    except Exception as e:
        log(f"Action err: {e}")
        return "\n[!] Ошибка при выполнении действия"
    return ""


def handle(uid, text, dialog_id):
    is_manager = str(uid) in MANAGER_IDS
    if uid not in chat_histories:
        chat_histories[uid] = []
    hist = chat_histories[uid]
    hist.append({"role": "user", "content": text})
    if len(hist) > 20:
        chat_histories[uid] = hist[-20:]
        hist = chat_histories[uid]
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=build_system_prompt(is_manager),
            messages=hist,
        )
        reply = resp.content[0].text
        hist.append({"role": "assistant", "content": reply})
        extra = ""
        if "<action>" in reply and "</action>" in reply:
            s = reply.index("<action>") + 8
            e_idx = reply.index("</action>")
            extra = do_action(reply[s:e_idx], uid, is_manager)
            reply = reply[:reply.index("<action>")] + reply[e_idx + 9:]
        send_msg(dialog_id, reply.strip() + extra)
    except Exception as ex:
        log(f"Handle err: {ex}")
        send_msg(dialog_id, "Ошибка. Повторите запрос.")


@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook/bitrix")
async def webhook(request: Request):
    try:
        data = dict(await request.form())
        if data.get("event") == "ONIMBOTMESSAGEADD":
            uid = data.get("data[USER][ID]", "?")
            text = data.get("data[PARAMS][MESSAGE]", "")
            dlg = data.get("data[PARAMS][DIALOG_ID]", "")
            log(f"Msg uid={uid} dlg={dlg} text={text[:20]}")
            if text and dlg:
                handle(uid, text, dlg)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        log(f"Webhook err: {e}")
        return JSONResponse({"status": "error"}, status_code=500)
