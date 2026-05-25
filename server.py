# -*- coding: utf-8 -*-
import os
import sys
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI()
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]
chat_histories = {}

SYSTEM_PROMPT = "You are an AI assistant for a company integrated into Bitrix24. Always respond in Russian. Help employees with: 1) TASKS - create, track, get recommendations. 2) QUESTIONS - answer work questions. 3) REPORTS - analyze and summarize. When employee asks to create a task, extract: title, responsible person, deadline, description. Be concise, professional, friendly. If need to create a task, return JSON at the end: <action>{\"type\": \"create_task\", \"title\": \"...\", \"description\": \"...\", \"deadline\": \"YYYY-MM-DD\"}</action>. If need to get task list: <action>{\"type\": \"get_tasks\"}</action>"

async def b24_request(method: str, params: dict) -> dict:
    url = f"{B24_WEBHOOK}{method}"
    async with httpx.AsyncClient() as client:
        r = await client.post(
            url,
            json=params,
            timeout=15,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        return r.json()

async def create_task(title: str, description: str = "", deadline: str = "") -> dict:
    fields = {"TITLE": title, "DESCRIPTION": description}
    if deadline:
        fields["DEADLINE"] = deadline
    return await b24_request("tasks.task.add", {"fields": fields})

async def get_tasks() -> list:
    result = await b24_request("tasks.task.list", {
        "filter": {"STATUS": "2"},
        "select": ["ID", "TITLE", "DEADLINE", "STATUS"],
        "order": {"DEADLINE": "ASC"}
    })
    return result.get("result", {}).get("tasks", [])

async def send_message(dialog_id: str, text: str):
    await b24_request("imbot.message.add", {
        "DIALOG_ID": dialog_id,
        "MESSAGE": text
    })

async def process_action(action_json: str) -> str:
    try:
        action = json.loads(action_json)
        if action.get("type") == "create_task":
            result = await create_task(
                title=action.get("title", "New task"),
                description=action.get("description", ""),
                deadline=action.get("deadline", "")
            )
            task_id = result.get("result", {}).get("task", {}).get("id")
            if task_id:
                return f"\n\u2705 Zadacha sozdana (ID: {task_id})"
            return "\n\u26a0\ufe0f Ne udalos sozdat zadachu."
        elif action.get("type") == "get_tasks":
            tasks = await get_tasks()
            if not tasks:
                return "\n Aktivnykh zadach net."
            lines = ["\n Aktivnye zadachi:"]
            for t in tasks[:10]:
                deadline = t.get("deadline", "bez sroka")
                lines.append(f"[{t['id']}] {t['title']} - srok: {deadline}")
            return "\n".join(lines)
    except Exception as e:
        sys.stderr.write(f"Action error: {str(e)}\n")
    return ""

async def handle_message(user_id: str, text: str, dialog_id: str):
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
            action_json = reply[start:end]
            action_result = await process_action(action_json)
            reply = reply[:reply.index("<action>")] + reply[end + 9:]
        final = reply.strip() + action_result
        await send_message(dialog_id, final)
    except Exception as e:
        sys.stderr.write(f"Handle error: {str(e)}\n")
        try:
            await send_message(dialog_id, "Izvinite, proizoshla oshibka. Pojaluysta, povtorite zapros.")
        except Exception:
            pass

@app.get("/")
async def root():
    return {"status": "Claude Bitrix24 Assistant is running"}

@app.post("/webhook/bitrix")
async def webhook(request: Request):
    try:
        data = dict(await request.form())
        event = data.get("event", "")
        if event == "ONIMBOTMESSAGEADD":
            user_id = data.get("data[USER][ID]", "unknown")
            text = data.get("data[PARAMS][MESSAGE]", "")
            dialog_id = data.get("data[PARAMS][DIALOG_ID]", "")
            if text and dialog_id:
                await handle_message(user_id, text, dialog_id)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        sys.stderr.write(f"Webhook error: {str(e)}\n")
        return JSONResponse({"status": "error"}, status_code=500)
