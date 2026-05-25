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

BASE_PROMPT = "You are a helpful AI assistant for a company in Bitrix24. Always respond in Russian language. Help with: 1) creating and tracking tasks 2) answering employee questions 3) analyzing reports. For tasks use: <action>{\"type\":\"create_task\",\"title\":\"...\",\"description\":\"...\",\"deadline\":\"YYYY-MM-DD\"}</action> For task list: <action>{\"type\":\"get_tasks\"}</action>"

WEEKDAYS_RU = [
    "\u043f\u043e\u043d\u0435\u0434\u0435\u043b\u044c\u043d\u0438\u043a",
    "\u0432\u0442\u043e\u0440\u043d\u0438\u043a",
    "\u0441\u0440\u0435\u0434\u0430",
    "\u0447\u0435\u0442\u0432\u0435\u0440\u0433",
    "\u043f\u044f\u0442\u043d\u0438\u0446\u0430",
    "\u0441\u0443\u0431\u0431\u043e\u0442\u0430",
    "\u0432\u043e\u0441\u043a\u0440\u0435\u0441\u0435\u043d\u044c\u0435",
]


def build_system_prompt():
    today = datetime.now()
    lines = ["", "", "Today is " + today.strftime("%Y-%m-%d") + " (" + WEEKDAYS_RU[today.weekday()] + ")."]
    lines.append("Upcoming dates for reference:")
    for i in range(1, 15):
        d = today + timedelta(days=i)
        lines.append("  " + WEEKDAYS_RU[d.weekday()] + ": " + d.strftime("%Y-%m-%d"))
    lines.append("When the user mentions a weekday or a relative day, use the matching date from this list. Never invent dates and never use dates in the past.")
    return BASE_PROMPT + "\n".join(lines)


def log(msg):
    try:
        sys.stderr.buffer.write((str(msg) + "\n").encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    except Exception:
        pass


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
        with urllib.request.urlopen(req, timeout=15) as r:
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
            "MESSAGE": text,
        }
        result = b24_call(B24_WEBHOOK, "imbot.message.add", params)
        log(f"Sent: {result}")
    except Exception as e:
        log(f"send_msg failed: {e}")


def do_action(action_json, responsible_id):
    try:
        a = json.loads(action_json)
        if a.get("type") == "create_task":
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
                return f"\n\u2705 \u0417\u0430\u0434\u0430\u0447\u0430 \u0441\u043e\u0437\u0434\u0430\u043d\u0430 (ID: {tid})"
            err = r.get("error_description") or r.get("error") or "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u043e\u0448\u0438\u0431\u043a\u0430"
            log(f"task.add failed: {r}")
            return f"\n\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0437\u0434\u0430\u0442\u044c \u0437\u0430\u0434\u0430\u0447\u0443: {err}"
        if a.get("type") == "get_tasks":
            r = b24_call(
                B24_WEBHOOK,
                "tasks.task.list",
                {
                    "filter": {"STATUS": "2"},
                    "select": ["ID", "TITLE", "DEADLINE"],
                    "order": {"DEADLINE": "ASC"},
                },
            )
            tasks = r.get("result", {}).get("tasks", [])
            if not tasks:
                return "\n\u041d\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0437\u0430\u0434\u0430\u0447"
            lines = ["\n\ud83d\udccb \u0417\u0430\u0434\u0430\u0447\u0438:"]
            for t in tasks[:10]:
                lines.append(f"\u2022 [{t['id']}] {t['title']}")
            return "\n".join(lines)
    except Exception as e:
        log(f"Action err: {e}")
        return "\n\u26a0\ufe0f \u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0440\u0438 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f"
    return ""


def handle(uid, text, dialog_id):
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
            system=build_system_prompt(),
            messages=hist,
        )
        reply = resp.content[0].text
        hist.append({"role": "assistant", "content": reply})
        extra = ""
        if "<action>" in reply and "</action>" in reply:
            s = reply.index("<action>") + 8
            e_idx = reply.index("</action>")
            extra = do_action(reply[s:e_idx], uid)
            reply = reply[:reply.index("<action>")] + reply[e_idx + 9:]
        send_msg(dialog_id, reply.strip() + extra)
    except Exception as ex:
        log(f"Handle err: {ex}")
        send_msg(dialog_id, "\u041e\u0448\u0438\u0431\u043a\u0430. \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441.")


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
