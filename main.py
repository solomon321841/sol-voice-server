import os
import json
import logging
import asyncio
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pyngrok import ngrok
import time
from openai import AsyncOpenAI

# =====================================================
# 🔧 LOGGING SETUP
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# =====================================================
# 🔑 ENVIRONMENT VARIABLES
# =====================================================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "29b20888d7678028ad4fc54ee3f18539").strip()

# =====================================================
# 🌐 EXTERNAL ENDPOINTS
# =====================================================
N8N_CALENDAR_URL = "https://n8n.marshall321.org/webhook/calendar-agent"
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

# =====================================================
# 🧠 MODEL CONFIGURATION
# =====================================================
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
GPT_MODEL = "gpt-4o-mini"

# =====================================================
# ⚙️ FASTAPI SETUP
# =====================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
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
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"filters": {"user_id": user_id}, "query": query}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post("https://api.mem0.ai/v2/memories/", headers=headers, json=payload)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"🔥 Mem0 v2 search error: {e}")
    return []

async def mem0_add_v1(user_id: str, text: str):
    if not MEMO_API_KEY or not text:
        return
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"user_id": user_id, "messages": [{"role": "user", "content": text}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post("https://api.mem0.ai/v1/memories/", headers=headers, json=payload)
        log.info(f"✅ Memory added for {user_id}")
    except Exception as e:
        log.error(f"🔥 Error adding memory to Mem0: {e}")

def build_memory_context(items: list) -> str:
    lines = []
    for it in items or []:
        if isinstance(it, dict):
            content = it.get("memory") or it.get("content") or it.get("text")
            if content:
                lines.append(f"- {content}")
    return "Relevant memories:\n" + "\n".join(lines) if lines else ""

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
                    text = "".join([r.get("plain_text", "") for r in block["paragraph"].get("rich_text", [])])
                    text_parts.append(text)
            return "\n".join(text_parts).strip() or "You are Solomon Roth’s AI assistant, Silas."
    except Exception as e:
        log.error(f"❌ Error fetching prompt from Notion: {e}")
        return "You are Solomon Roth’s AI assistant, Silas."

# =====================================================
# 🧩 ADMIN PANEL PROMPT FETCH (PLAIN TEXT)
# =====================================================
@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt():
    """Return the system prompt as plain text (for admin panel)."""
    try:
        prompt_text = await get_latest_prompt()
        return PlainTextResponse(prompt_text)
    except Exception as e:
        log.error(f"❌ Error loading prompt for admin panel: {e}")
        return PlainTextResponse("⚠️ Failed to load prompt.")

# =====================================================
# 🧩 n8n HELPERS
# =====================================================
async def send_to_n8n_calendar(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(N8N_CALENDAR_URL, json={"message": user_message})
            if r.status_code == 200:
                return r.text.strip()
    except Exception as e:
        log.error(f"❌ Calendar error: {e}")
    return "Sorry, couldn't reach your calendar."

async def send_to_plate(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(N8N_PLATE_URL, json={"message": user_message})
            log.info(f"🍽️ Plate response: {r.text}")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data and "reply" in data[0]:
                    return data[0]["reply"]
                if isinstance(data, dict):
                    return data.get("reply") or data.get("message") or r.text
    except Exception as e:
        log.error(f"❌ Plate error: {e}")
    return "Sorry, couldn't reach your plate."

# =====================================================
# 🔌 RETELL CONNECTION
# =====================================================
active_connections: Dict[str, WebSocket] = {}

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    old_ws = active_connections.get(call_id)
    if old_ws:
        try:
            await old_ws.close()
            log.info(f"⚠️ Closed old session for {call_id}")
        except Exception:
            pass
    active_connections[call_id] = websocket

    await websocket.accept()
    log.info(f"🔌 Connected: {call_id}")
    user_id = "solomon_roth"

    async def send_speech(response_id: int, text: str, end_turn: bool = True):
        payload = {
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": end_turn,
            "end_turn": end_turn,
        }
        try:
            await websocket.send_text(json.dumps(payload))
            log.info(f"🗣️ Sent: {text[:80]}")
        except Exception as e:
            log.error(f"Send error: {e}")

    # Greeting
    notion_prompt = await get_latest_prompt()
    first_line = notion_prompt.splitlines()[0] if notion_prompt else ""
    greeting = first_line or "Hello Solomon, I’m Silas. Ready when you are."
    await send_speech(0, greeting)

    calendar_keywords = ["schedule", "meeting", "calendar", "cancel", "appointment"]
    plate_keywords = ["plate", "add", "task", "to-do", "notion", "what’s on my plate"]

    last_message = {"text": None, "time": 0}

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            transcript = data.get("transcript", [])
            interaction_type = data.get("interaction_type")
            response_id = int(data.get("response_id", 1))

            user_message = ""
            for t in reversed(transcript or []):
                if t.get("role") == "user":
                    user_message = t.get("content", "").strip()
                    break

            if interaction_type == "response_required" and user_message:
                now = time.time()
                if user_message == last_message["text"] and now - last_message["time"] < 3:
                    continue
                last_message = {"text": user_message, "time": now}

                mem_items = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mem_items)
                system_prompt = f"{notion_prompt}\n\nFacts:\n{context}"

                if any(k in user_message.lower() for k in plate_keywords):
                    await send_speech(response_id, "Got it, adding that now...", end_turn=False)
                    reply = await send_to_plate(user_message)
                    await send_speech(response_id, reply)
                    continue

                if any(k in user_message.lower() for k in calendar_keywords):
                    await send_speech(response_id, "Checking your calendar...", end_turn=False)
                    reply = await send_to_n8n_calendar(user_message)
                    await send_speech(response_id, reply)
                    continue

                try:
                    stream = await openai_client.chat.completions.create(
                        model=GPT_MODEL,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                        ],
                        max_tokens=150,
                        temperature=0.7,
                        stream=True,
                    )
                    async for chunk in stream:
                        delta = getattr(chunk.choices[0].delta, "content", None)
                        if delta:
                            await send_speech(response_id, delta, end_turn=False)
                    await send_speech(response_id, "", end_turn=True)
                    asyncio.create_task(mem0_add_v1(user_id, user_message))
                except Exception as e:
                    log.error(f"⚠️ Stream error: {e}")
                    await send_speech(response_id, "Sorry, I ran into a small hiccup.")
    except WebSocketDisconnect:
        log.info(f"❌ Disconnected: {call_id}")
    finally:
        active_connections.pop(call_id, None)
        log.info(f"🔕 Closed: {call_id}")

# =====================================================
# 🚀 START SERVER
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

