"""
Claude AI Ассистент для Битрикс24
==================================
Сервер-посредник между Битрикс24 и Claude API.
Функции: управление задачами, ответы на вопросы сотрудников, анализ отчётов.
"""

import os
import json
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from anthropic import Anthropic

app = FastAPI(title="Claude Битрикс24 Ассистент")

# ─── Клиент Claude ───────────────────────────────────────────────────────────
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ─── Настройки Битрикс24 ─────────────────────────────────────────────────────
B24_WEBHOOK = os.environ["BITRIX24_WEBHOOK"]  # Входящий вебхук Б24
# Формат: https://ВАШ_ДОМЕН.bitrix24.ru/rest/1/ТОКЕН/

# ─── Хранилище истории диалогов (в памяти, для 25 сотрудников достаточно) ────
chat_histories: dict[str, list] = {}

# ─── Системный промпт для Claude ─────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — AI-ассистент компании, встроенный в Битрикс24.
Общаешься на русском языке. Ты помогаешь сотрудникам:

1. ЗАДАЧИ — создавать, отслеживать, получать рекомендации
2. ВОПРОСЫ — отвечать на рабочие вопросы
3. ОТЧЁТЫ — анализировать и резюмировать

Когда сотрудник просит создать задачу, извлеки из его сообщения:
- название задачи
- ответственного (если указан)
- дедлайн (если указан)
- описание

Отвечай кратко, по делу, дружелюбно. Если нужно создать задачу — 
верни JSON в конце ответа в формате:
<action>{"type": "create_task", "title": "...", "description": "...", "deadline": "YYYY-MM-DD", "responsible": "..."}</action>

Если нужно получить список задач:
<action>{"type": "get_tasks"}</action>
"""


# ─── Вспомогательные функции Битрикс24 ───────────────────────────────────────

async def b24_request(method: str, params: dict) -> dict:
    """Выполнить запрос к REST API Битрикс24."""
    url = f"{B24_WEBHOOK}{method}"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=params, timeout=10)
        return response.json()


async def create_task(title: str, description: str = "", deadline: str = "", responsible: str = "") -> dict:
    """Создать задачу в Битрикс24."""
    fields = {
        "TITLE": title,
        "DESCRIPTION": description,
    }
    if deadline:
        fields["DEADLINE"] = deadline
    # responsible — имя или ID; если передано имя, пропускаем (нужен ID)
    
    result = await b24_request("tasks.task.add", {"fields": fields})
    return result


async def get_tasks() -> list:
    """Получить список активных задач."""
    result = await b24_request("tasks.task.list", {
        "filter": {"STATUS": "2"},  # статус: в работе
        "select": ["ID", "TITLE", "DEADLINE", "STATUS", "RESPONSIBLE_ID"],
        "order": {"DEADLINE": "ASC"}
    })
    return result.get("result", {}).get("tasks", [])


async def send_message_to_chat(chat_id: str, text: str):
    """Отправить сообщение обратно в чат Битрикс24."""
    await b24_request("im.message.add", {
        "DIALOG_ID": chat_id,
        "MESSAGE": text
    })


# ─── Обработка действий из ответа Claude ─────────────────────────────────────

async def process_action(action_json: str) -> str:
    """Выполнить действие, которое Claude вернул в теге <action>."""
    try:
        action = json.loads(action_json)
        action_type = action.get("type")

        if action_type == "create_task":
            result = await create_task(
                title=action.get("title", "Новая задача"),
                description=action.get("description", ""),
                deadline=action.get("deadline", ""),
                responsible=action.get("responsible", "")
            )
            task_id = result.get("result", {}).get("task", {}).get("id")
            if task_id:
                return f"\n✅ Задача создана (ID: {task_id})"
            else:
                return "\n⚠️ Не удалось создать задачу. Проверьте настройки вебхука."

        elif action_type == "get_tasks":
            tasks = await get_tasks()
            if not tasks:
                return "\n📋 Активных задач не найдено."
            lines = ["\n📋 Активные задачи:"]
            for t in tasks[:10]:  # показываем не более 10
                deadline = t.get("deadline", "без срока")
                lines.append(f"• [{t['id']}] {t['title']} — срок: {deadline}")
            return "\n".join(lines)

    except Exception as e:
        return f"\n⚠️ Ошибка при выполнении действия: {e}"

    return ""


# ─── Основной обработчик сообщений ───────────────────────────────────────────

async def handle_message(user_id: str, user_name: str, text: str, dialog_id: str):
    """Обработать входящее сообщение от сотрудника."""

    # Инициализация истории диалога
    if user_id not in chat_histories:
        chat_histories[user_id] = []

    history = chat_histories[user_id]
    history.append({"role": "user", "content": text})

    # Ограничиваем историю последними 20 сообщениями
    if len(history) > 20:
        history = history[-20:]
        chat_histories[user_id] = history

    # Запрос к Claude
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply_text = response.content[0].text

    # Добавляем ответ в историю
    history.append({"role": "assistant", "content": reply_text})

    # Проверяем, есть ли действие в ответе
    action_result = ""
    if "<action>" in reply_text and "</action>" in reply_text:
        start = reply_text.index("<action>") + 8
        end = reply_text.index("</action>")
        action_json = reply_text[start:end]
        action_result = await process_action(action_json)
        # Убираем тег <action> из текста ответа
        reply_text = reply_text[:reply_text.index("<action>")] + reply_text[end + 9:]

    final_reply = reply_text.strip() + action_result

    # Отправляем ответ в Битрикс24
    await send_message_to_chat(dialog_id, final_reply)


# ─── Эндпоинты ───────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "Claude Битрикс24 Ассистент работает ✅"}


@app.post("/webhook/bitrix")
async def bitrix_webhook(request: Request):
    """Принимает события от Битрикс24 (сообщения в чате)."""
    try:
        data = await request.form()
        data = dict(data)

        event = data.get("event", "")

        # Обрабатываем только новые сообщения
        if event == "ONIMBOTMESSAGEADD":
            user_id = data.get("data[USER][ID]", "unknown")
            user_name = data.get("data[USER][NAME]", "Сотрудник")
            text = data.get("data[PARAMS][MESSAGE]", "")
            dialog_id = data.get("data[PARAMS][DIALOG_ID]", "")

            if text and dialog_id:
                await handle_message(user_id, user_name, text, dialog_id)

        return JSONResponse({"status": "ok"})

    except Exception as e:
        print(f"Ошибка вебхука: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/ask")
async def ask_directly(request: Request):
    """Прямой API для тестирования без Битрикс24."""
    body = await request.json()
    user_id = body.get("user_id", "test_user")
    text = body.get("message", "")

    if not text:
        return JSONResponse({"error": "Поле message обязательно"}, status_code=400)

    if user_id not in chat_histories:
        chat_histories[user_id] = []

    history = chat_histories[user_id]
    history.append({"role": "user", "content": text})

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})

    return JSONResponse({"reply": reply})
