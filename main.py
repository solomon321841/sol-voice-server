import os
import json
import logging
import asyncio
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pyngrok import ngrok
import time
from openai import AsyncOpenAI

# ============ Logging ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# ============ Env ============
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "29b20888d7678028ad4fc54ee3f18539").strip()

# ============ n8n Webhooks ============
N8N_CALENDAR_URL = "https://n8n.marshall321.org/webhook/calendar-agent"
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

# ============ OpenAI Client ============
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
GPT_MODEL = "gpt-4o-mini"

# ============ FastAPI ============
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
    url = "https://api.mem0.ai/v2/memories/"
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"filters": {"user_id": user_id}, "query": query}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                log.info(f"🧠 Found {len(data)} memories for {user_id}")
                return data
        else:
            log.warning(f"⚠️ Mem0 v2 search failed ({r.status_code}): {r.text}")
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
        log.info(f"✅ Memory added for {user_id}")
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
            res.raise_for_status()
            data = res.json()
            text_parts = []
            for block in data.get("results", []):
                if block.get("type") == "paragraph":
                    text = "".join(
                        [r.get("plain_text", "") for r in block["paragraph"].get("rich_text", [])]
                    )
                    text_parts.append(text)
            return "\n".join(text_parts).strip() or "You are Solomon Roth’s AI assistant."
    except Exception as e:
        log.error(f"❌ Error fetching prompt from Notion: {e}")
        return "You are Solomon Roth’s AI assistant."

# =====================================================
# 🧩 n8n HELPERS
# =====================================================
async def send_to_n8n_calendar(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(N8N_CALENDAR_URL, json={"message": user_message})
            log.info(f"📩 n8n calendar response: {response.text}")
            return response.text.strip() if response.status_code == 200 else "Calendar workflow error."
    except Exception as e:
        log.error(f"❌ Error sending to calendar: {e}")
    return "Sorry, couldn’t reach your calendar."

async def send_to_plate(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(N8N_PLATE_URL, json={"message": user_message})
            log.info(f"🍽️ Plate response: {response.text}")
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list) and data and "reply" in data[0]:
                    return data[0]["reply"]
                elif isinstance(data, dict):
                    return data.get("reply") or data.get("message") or response.text
            return "Plate workflow error."
    except Exception as e:
        log.error(f"❌ Error sending to plate: {e}")
    return "Sorry, couldn’t reach your plate."

# =====================================================
# 🔌 RETELL CONNECTION (Streaming GPT)
# =====================================================

active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    if call_id in active_connections:
        await websocket.close()
        return
    active_connections.add(call_id)
    await websocket.accept()
    log.info(f"🔌 Retell WebSocket connected: {call_id}")
    user_id = "solomon_roth"

    async def send_speech(response_id: int, text: str, end_turn: bool = True):
        payload = {
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": end_turn,
            "end_turn": end_turn,
        }
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ Sent: {text[:80]}")

    await send_speech(0, "Hello Solomon, I'm ready whenever you are.")

    calendar_keywords = ["schedule", "meeting", "calendar", "cancel", "appointment"]
    plate_keywords = ["plate", "add", "task", "to-do", "notion", "what’s on my plate"]

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
                if user_message == last_message["text"] and now - last_message["time"] < 3:
                    continue
                last_message = {"text": user_message, "time": now}

                # Fetch memory and prompt
                mem_items = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mem_items)
                notion_prompt = await get_latest_prompt()

                # Route to workflows
                if any(k in user_message.lower() for k in plate_keywords):
                    await send_speech(response_id, "Got it, adding that now...", end_turn=False)
                    reply = await send_to_plate(user_message)
                    await send_speech(response_id, reply)
                    continue
                if any(k in user_message.lower() for k in calendar_keywords):
                    await send_speech(response_id, "Let me check your calendar...", end_turn=False)
                    reply = await send_to_n8n_calendar(user_message)
                    await send_speech(response_id, reply)
                    continue

                # GPT streaming
                system_prompt = f"{notion_prompt}\n\nFacts:\n{context}"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]

                try:
                    stream = await openai_client.chat.completions.create(
                        model=GPT_MODEL,
                        messages=messages,
                        max_tokens=150,
                        temperature=0.7,
                        stream=True,
                    )
                    collected = ""
                    async for chunk in stream:
                        delta = chunk.choices[0].delta.get("content", "")
                        if delta:
                            collected += delta
                            await send_speech(response_id, delta, end_turn=False)
                    await send_speech(response_id, "", end_turn=True)
                    if user_message:
                        asyncio.create_task(mem0_add_v1(user_id, user_message))
                except Exception as e:
                    log.error(f"❌ Streaming error: {e}")
                    await send_speech(response_id, "Sorry, I hit a small issue.")
    except WebSocketDisconnect:
        log.info(f"❌ Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"🔕 Closed: {call_id}")

# =====================================================
# 🚀 SERVER STARTUP
# =====================================================
def start_ngrok(port: int = 8000) -> str:
    tunnel = ngrok.connect(addr=port, proto="http")
    url = tunnel.public_url.replace("http://", "https://")
    log.info(f"🌐 Public URL: {url}")
    log.info(f"🔗 Retell Custom LLM URL: wss://{url.replace('https://', '')}/ws/{{call_id}}")
    return url

if __name__ == "__main__":
    start_ngrok(8000)
    log.info("🚀 FastAPI running...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

