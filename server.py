import os, sys, json, urllib.request
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI()
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]
chat_histories = {}

SYSTEM_PROMPT = "You are a helpful AI assistant for a company in Bitrix24. Always respond in Russian language. Help with: 1) creating and tracking tasks 2) answering employee questions 3) analyzing reports. For tasks use: <action>{\"type\":\"create_task\",\"title\":\"...\",\"description\":\"...\",\"deadline\":\"YYYY-MM-DD\"}</action> For task list: <action>{\"type\":\"get_tasks\"}</action>"

def log(msg):
    try:
        sys.stderr.buffer.write((str(msg) + "\n").encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()
    except Exception:
        pass

def b24_call(url, method, params):
    full_url = url + method
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(full_url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def send_msg(dialog_id, text):
    # Use main webhook with im.message.add
    methods = ["im.message.add", "imbot.message.add"]
    for method in methods:
        try:
            params = {"DIALOG_ID": dialog_id, "MESSAGE": text}
            result = b24_call(B24_WEBHOOK, method, params)
            log(f"Sent via {method}: {result}")
            return
        except Exception as e:
            log(f"Method {method} failed: {e}")

def do_action(action_json):
    try:
        a = json.loads(action_json)
        if a.get("type") == "create_task":
            fields = {"TITLE": a.get("title","Task"), "DESCRIPTION": a.get("description","")}
            if a.get("deadline"): fields["DEADLINE"] = a["deadline"]
            r = b24_call(B24_WEBHOOK, "tasks.task.add", {"fields": fields})
            tid = r.get("result",{}).get("task",{}).get("id")
            return f"\n\u2705 \u0417\u0430\u0434\u0430\u0447\u0430 \u0441\u043e\u0437\u0434\u0430\u043d\u0430 (ID: {tid})" if tid else "\n\u26a0\ufe0f \u041e\u0448\u0438\u0431\u043a\u0430"
        if a.get("type") == "get_tasks":
            r = b24_call(B24_WEBHOOK, "tasks.task.list", {"filter":{"STATUS":"2"},"select":["ID","TITLE","DEADLINE"],"order":{"DEADLINE":"ASC"}})
            tasks = r.get("result",{}).get("tasks",[])
            if not tasks: return "\n\u041d\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0437\u0430\u0434\u0430\u0447"
            lines = ["\n\ud83d\udccb \u0417\u0430\u0434\u0430\u0447\u0438:"] + [f"\u2022 [{t['id']}] {t['title']}" for t in tasks[:10]]
            return "\n".join(lines)
    except Exception as e:
        log(f"Action err: {e}")
    return ""

def handle(uid, text, dialog_id):
    if uid not in chat_histories: chat_histories[uid] = []
    hist = chat_histories[uid]
    hist.append({"role":"user","content":text})
    if len(hist) > 20: chat_histories[uid] = hist[-20:]; hist = chat_histories[uid]
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=hist
        )
        reply = resp.content[0].text
        hist.append({"role":"assistant","content":reply})
        extra = ""
        if "<action>" in reply and "</action>" in reply:
            s = reply.index("<action>") + 8
            e_idx = reply.index("</action>")
            extra = do_action(reply[s:e_idx])
            reply = reply[:reply.index("<action>")] + reply[e_idx+9:]
        send_msg(dialog_id, reply.strip() + extra)
    except Exception as ex:
        log(f"Handle err: {ex}")
        send_msg(dialog_id, "\u041e\u0448\u0438\u0431\u043a\u0430. \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u0437\u0430\u043f\u0440\u043e\u0441.")

@app.get("/")
async def root(): return {"status":"ok"}

@app.post("/webhook/bitrix")
async def webhook(request: Request):
    try:
        data = dict(await request.form())
        if data.get("event") == "ONIMBOTMESSAGEADD":
            uid = data.get("data[USER][ID]","?")
            text = data.get("data[PARAMS][MESSAGE]","")
            dlg = data.get("data[PARAMS][DIALOG_ID]","")
            log(f"Msg uid={uid} dlg={dlg} text={text[:20]}")
            if text and dlg: handle(uid, text, dlg)
        return JSONResponse({"status":"ok"})
    except Exception as e:
        log(f"Webhook err: {e}")
        return JSONResponse({"status":"error"}, status_code=500)
