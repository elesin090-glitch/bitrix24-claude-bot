# -*- coding: utf-8 -*-
import os
import sys
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI()
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]
chat_histories = {}

SYSTEM_PROMPT = "You are an AI assistant for a company integrated into Bitrix24. Always respond in Russian. Help employees with tasks, questions, and reports. When asked to create a task, extract title, deadline, description and return: <action>{\"type\": \"create_task\", \"title\": \"...\", \"description\": \"...\", \"deadline\": \"YYYY-MM-DD\"}</action>. When asked for task list return: <action>{\"type\": \"get_tasks\"}</action>"

def b24_request(method, params):
    url = (B24_WEBHOOK + method).encode('ascii')
    data = json.dumps(params, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))

def send_message(dialog_id, text):
    try:
        b24_request("imbot.message.add", {
            "DIALOG_ID": dialog_id,
            "MESSAGE": text
        })
    except Exception as e:
        sys.stderr.write(f"Send error: {e}\n")

def create_task(title, description="", deadline=""):
    fields = {"TITLE": title, "DESCRIPTION": description}
    if deadline:
        fields["DEADLINE"] = deadline
    return b24_request("tasks.task.add", {"fields": fields})

def get_tasks():
    result = b24_request("tasks.task.list", {
        "filter": {"STATUS": "2"},
        "select": ["ID", "TITLE", "DEADLINE"],
        "order": {"DEADLINE": "ASC"}
    })
    return result.get("result", {}).get("tasks", [])

def process_action(action_json):
    try:
        action = json.loads(action_json)
        if action.get("type") == "create_task":
            result = create_task(
                title=action.get("title", "New task"),
                description=action.get("description", ""),
                deadline=action.get("deadline", "")
            )
            task_id = result.get("result", {}).get("task", {}).get("id")
            if task_id:
                return f"\n✅ Задача создана (ID: {task_id})"
            return "\n⚠️ Не удалось создать задачу."
        elif action.get("type") == "get_tasks":
            tasks = get_tasks()
            if not tasks:
                return "\n📋 Активных задач нет."
            lines = ["\n📋 Активные задачи:"]
            for t in tasks[:10]:
                lines.append(f"• [{t['id']}] {t['title']} — {t.get('deadline','без срока')}")
            return "\n".join(lines)
    except Exception as e:
        sys.stderr.write(f"Action error: {e}\n")
    return ""

def handle_message(user_id, text, dialog_id):
    if user_id not in chat_histories:
        chat_histories[user_id] = []
    history = chat_histories[user_id]
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        chat_histories[user_id] = history[-20:]
        history = chat_histories[user_id]
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        action_result = ""
        if "<action>" in reply and "</action>" in reply:
            start = reply.index("<action>") + 8
            end = reply.index("</action>")
            action_result = process_action(reply[start:end])
            reply = reply[:reply.index("<action>")] + reply[end + 9:]
        send_message(dialog_id, reply.strip() + action_result)
    except Exception as e:
        sys.stderr.write(f"Handle error: {e}\n")
        send_message(dialog_id, "Извините, произошла ошибка. Повторите запрос.")

@app.get("/")
async def root():
    return {"status": "running"}

@app.post("/webhook/bitrix")
async def webhook(request: Request):
    try:
        data = dict(await request.form())
        if data.get("event") == "ONIMBOTMESSAGEADD":
            user_id = data.get("data[USER][ID]", "unknown")
            text = data.get("data[PARAMS][MESSAGE]", "")
            dialog_id = data.get("data[PARAMS][DIALOG_ID]", "")
            if text and dialog_id:
                handle_message(user_id, text, dialog_id)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        sys.stderr.write(f"Webhook error: {e}\n")
        return JSONResponse({"status": "error"}, status_code=500)
