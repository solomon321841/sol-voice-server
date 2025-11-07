import os
import json
import logging
import asyncio
import time
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pyngrok import ngrok

# =====================================================
# 🪶 Logging Setup
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# =====================================================
# 🔐 Environment Variables
# =====================================================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

N8N_CALENDAR_URL = "https://n8n.marshall321.org/webhook/calendar-agent"
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

# =====================================================
# ⚙️ FastAPI Setup
# =====================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/")
async def webhook_sink(request: Request):
    try:
        _ = await request.body()
    except Exception:
        pass
    return JSONResponse({"ok": True})

# =====================================================
# 🧠 MEM0 FUNCTIONS
# =====================================================
async def mem0_search_v2(user_id: str, query: str):
    if not MEMO_API_KEY:
        return []
    url = "https://api.mem0.ai/v2/memories/"
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"filters": {"user_id": user_id}, "query": query}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"🔥 Mem0 v2 search error: {e}")
    return []

async def mem0_add_v1(user_id: str, text: str):
    if not MEMO_API_KEY or not text:
        return
    url = "https://api.mem0.ai/v1/memories/"
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"user_id": user_id, "messages": [{"role": "user", "content": text}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as e:
        log.error(f"🔥 Error adding memory to Mem0: {e}")

def build_memory_context(items: list) -> str:
    if not items:
        return ""
    lines = []
    for it in items:
        if isinstance(it, dict):
            content = it.get("memory") or it.get("content") or it.get("text")
            if content:
                lines.append(f"- {content}")
    return "\n".join(lines)

# =====================================================
# 🤖 GPT-5-o CHAT
# =====================================================
GPT_MODEL = "gpt-4o"

async def openai_chat(messages: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "Missing OpenAI API key."
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    data = {"model": GPT_MODEL, "messages": messages, "max_tokens": 300, "temperature": 0.8}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            res = r.json()
            return res["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"OpenAI Error: {e}")
        return "Sorry, I ran into an issue with GPT-5-o."

# =====================================================
# 🧩 FETCH PROMPT LIVE FROM NOTION
# =====================================================
async def get_latest_prompt():
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers)
            data = res.json()
            text_parts = []
            for block in data.get("results", []):
                if block.get("type") == "paragraph":
                    text_parts.append("".join([r.get("plain_text", "") for r in block["paragraph"].get("rich_text", [])]))
            return "\n".join(text_parts).strip() or "You are Solomon Roth’s personal AI assistant."
    except Exception as e:
        log.error(f"❌ Notion prompt fetch error: {e}")
        return "You are Solomon Roth’s personal AI assistant."

# =====================================================
# 🧩 N8N WORKFLOWS
# =====================================================
async def send_to_n8n_calendar(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(N8N_CALENDAR_URL, json={"message": user_message})
            if response.status_code == 200:
                return response.text.strip()
    except Exception as e:
        log.error(f"❌ Calendar send error: {e}")
    return "Sorry, I couldn’t reach your calendar right now."

async def send_to_plate(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(N8N_PLATE_URL, json={"message": user_message})
            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        return data.get("reply") or data.get("message") or data.get("text") or json.dumps(data)
                    elif isinstance(data, list):
                        return " ".join(str(x) for x in data)
                except Exception:
                    return response.text.strip()
    except Exception as e:
        log.error(f"❌ Plate send error: {e}")
    return "Sorry, I couldn’t reach your plate right now."

# =====================================================
# 🔌 RETELL CONNECTION
# =====================================================
active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    if call_id in active_connections:
        await websocket.close()
        return
    active_connections.add(call_id)

    await websocket.accept()
    log.info(f"🔌 Retell connected: {call_id}")
    user_id = "solomon_roth"

    async def send_speech(response_id: int, text: str, end_turn: bool = True):
        payload = {
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": True,
            "end_turn": end_turn,
        }
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ Sent: {text[:100]}")

    await send_speech(0, "Hey Solomon — I’m running GPT-5-o now. Ready when you are.")

    calendar_keywords = ["schedule", "meeting", "calendar", "cancel", "event", "appointment", "reschedule"]
    plate_keywords = [
        "plate", "add", "task", "to-do", "notion", "on my plate",
        "remove from plate", "what’s on my plate", "add to my plate",
        "put on my plate", "add to tasks", "add to my list"
    ]

    last_message = {"text": None, "time": 0}

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            transcript = data.get("transcript", [])
            interaction_type = data.get("interaction_type")
            response_id = int(data.get("response_id", 1))
            user_message = ""
            if transcript and isinstance(transcript, list):
                for t in reversed(transcript):
                    if t.get("role") == "user":
                        user_message = t.get("content", "").strip()
                        break

            if interaction_type == "response_required" and user_message:
                now = time.time()
                if (
                    user_message.lower() == (last_message["text"] or "").lower()
                    and now - last_message["time"] < 3
                ):
                    continue
                last_message = {"text": user_message, "time": now}

                mem_items = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mem_items)
                notion_prompt = await get_latest_prompt()

                if any(kw in user_message.lower() for kw in plate_keywords):
                    reply = await send_to_plate(user_message)
                    await send_speech(response_id, reply)
                    continue

                if any(kw in user_message.lower() for kw in calendar_keywords):
                    reply = await send_to_n8n_calendar(user_message)
                    await send_speech(response_id, reply)
                    continue

                system_prompt = f"{notion_prompt}\n\n{context}"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
                reply = await openai_chat(messages)
                await send_speech(response_id, reply)
                asyncio.create_task(mem0_add_v1(user_id, user_message))

    except WebSocketDisconnect:
        active_connections.discard(call_id)
        log.info(f"❌ Disconnected: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# =====================================================
# 🚀 STARTUP
# =====================================================
def start_ngrok(port: int = 8000) -> str:
    tunnel = ngrok.connect(addr=port, proto="http")
    url = tunnel.public_url.replace("http://", "https://")
    log.info(f"🌐 Public URL: {url}")
    log.info(f"🔗 Retell LLM URL: wss://{url.replace('https://', '')}/ws/{{call_id}}")
    return url

if __name__ == "__main__":
    start_ngrok(8000)
    uvicorn.run(app, host="0.0.0.0", port=8000)

