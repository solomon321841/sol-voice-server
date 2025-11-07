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
import random

# ============ Logging ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# ============ Env ============
load_dotenv()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "29b20888d7678028ad4fc54ee3f18539").strip()

# ============ n8n Webhooks ============
N8N_CALENDAR_URL = "https://n8n.marshall321.org/webhook/calendar-agent"
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

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
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 200:
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
    if not lines:
        return ""
    return "Relevant memories (use only if helpful):\n" + "\n".join(lines)

# =====================================================
# ⚙️ CEREBRAS CHAT
# =====================================================
CEREBRAS_MODEL = "llama3.1-8b"

async def cerebras_chat(messages: List[Dict[str, str]]) -> str:
    if not CEREBRAS_API_KEY:
        return "Cerebras key missing."
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    data = {"model": CEREBRAS_MODEL, "messages": messages, "max_tokens": 300, "temperature": 0.7}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            res = r.json()
            return res["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        return "Sorry, I hit a speed bump. Try again?"

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
            prompt_text = "\n".join(text_parts).strip()
            return prompt_text or "You are Solomon Roth’s personal AI assistant."
    except Exception as e:
        log.error(f"❌ Error fetching prompt from Notion: {e}")
        return "You are Solomon Roth’s personal AI assistant."

# =====================================================
# 🧩 N8N HELPERS
# =====================================================
async def send_to_n8n_calendar(user_message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"message": user_message}
            response = await client.post(N8N_CALENDAR_URL, json=payload)
            log.info(f"📩 n8n calendar response: {response.text}")
            if response.status_code == 200:
                return response.text.strip()
            else:
                log.warning(f"⚠️ n8n calendar returned {response.status_code}: {response.text}")
    except Exception as e:
        log.error(f"❌ Error sending to n8n calendar: {e}")
    return "Sorry, I couldn’t reach your calendar right now."

# =====================================================
# 🧩 PLATE (NOTION) WORKFLOW
# =====================================================
async def send_to_plate(user_message: str) -> str:
    """Send user message to Notion Plate workflow and return clean reply."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"message": user_message}
            response = await client.post(N8N_PLATE_URL, json=payload)
            log.info(f"🍽️ Plate response: {response.text}")

            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        reply_text = (
                            data.get("reply")
                            or data.get("message")
                            or data.get("text")
                            or data.get("output")
                        )
                        if reply_text:
                            return str(reply_text).strip()
                        return json.dumps(data, indent=2)
                    elif isinstance(data, list):
                        # ✅ Extract readable reply if n8n sends [{"reply": "..."}]
                        reply_texts = []
                        for x in data:
                            if isinstance(x, dict):
                                reply_texts.append(x.get("reply") or x.get("message") or str(x))
                            else:
                                reply_texts.append(str(x))
                        return " ".join(reply_texts)
                    else:
                        return str(data).strip()
                except Exception:
                    return response.text.strip()
            else:
                log.warning(f"⚠️ Plate returned {response.status_code}: {response.text}")
    except Exception as e:
        log.error(f"❌ Error sending to plate workflow: {e}")
    return "Sorry, I couldn’t reach your plate right now."

# =====================================================
# 🔌 RETELL CONNECTION
# =====================================================

active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    # --- Prevent multiple simultaneous connections for same call_id ---
    if call_id in active_connections:
        log.info(f"⚠️ Duplicate connection detected for {call_id}, closing old one.")
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
            "content_complete": True,
            "end_turn": end_turn,
        }
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ Sent speech response: {text[:100]}")

    await send_speech(0, "Hello Solomon, I’m ready. What can I do for you today?")

    calendar_keywords = ["schedule", "meeting", "calendar", "cancel", "event", "appointment", "reschedule"]
    plate_keywords = [
        "plate", "add", "task", "to-do", "notion", "on my plate",
        "remove from plate", "what’s on my plate", "add to my plate",
        "put on my plate", "add to tasks", "add to my list"
    ]

    quick_add = ["Got it, adding that now.", "Sure thing, I’ll add it.", "Okay, putting that on your plate."]
    quick_check = ["Checking that for you.", "One second, let me see.", "Here’s what’s on your plate."]
    quick_remove = ["Got it, removing that.", "Okay, it’s gone now.", "Done, that’s removed."]

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

            if interaction_type == "response_required":
                if user_message:
                    now = time.time()
                    same = user_message.strip().lower() == (last_message["text"] or "").strip().lower()
                    subset = (
                        user_message.strip().lower().startswith((last_message["text"] or "").strip().lower())
                        or (last_message["text"] or "").strip().lower().startswith(user_message.strip().lower())
                    )
                    if (same or subset) and now - last_message["time"] < 3:
                        log.info("🛑 Skipping duplicate/partial message to prevent double voice.")
                        continue
                    last_message = {"text": user_message, "time": now}

                mem_items = await mem0_search_v2(user_id, user_message or "")
                context = build_memory_context(mem_items)
                notion_prompt = await get_latest_prompt()

                lower_msg = user_message.lower()

                # --- PLATE logic with dynamic responses ---
                if any(kw in lower_msg for kw in plate_keywords):
                    if "add" in lower_msg or "put" in lower_msg or "save" in lower_msg:
                        await send_speech(response_id, random.choice(quick_add), end_turn=False)
                        reply = await send_to_plate(user_message)
                        await send_speech(response_id, reply)
                        continue
                    elif "what" in lower_msg or "show" in lower_msg or "see" in lower_msg or "check" in lower_msg:
                        await send_speech(response_id, random.choice(quick_check), end_turn=False)
                        reply = await send_to_plate(user_message)
                        await send_speech(response_id, reply)
                        continue
                    elif "remove" in lower_msg or "delete" in lower_msg:
                        await send_speech(response_id, random.choice(quick_remove), end_turn=False)
                        reply = await send_to_plate(user_message)
                        await send_speech(response_id, reply)
                        continue

                if any(kw in user_message.lower() for kw in calendar_keywords):
                    log.info(f"📅 Routing to calendar workflow: {user_message}")
                    reply = await send_to_n8n_calendar(user_message)
                    await send_speech(response_id, reply)
                    continue

                system_prompt = (
                    f"{notion_prompt}\n\n"
                    "The following are true remembered facts about Solomon. "
                    "Do not say you don't know them if they're listed below.\n"
                    f"{context}\n"
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message or "Hello?"},
                ]
                reply = await cerebras_chat(messages)
                await send_speech(response_id, reply)
                if user_message:
                    asyncio.create_task(mem0_add_v1(user_id, user_message))

    except WebSocketDisconnect:
        log.info(f"❌ Retell WebSocket disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"🔕 Connection closed and removed: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# =====================================================
# 🚀 SERVER STARTUP
# =====================================================
def start_ngrok(port: int = 8000) -> str:
    tunnel = ngrok.connect(addr=port, proto="http")
    url = tunnel.public_url.replace("http://", "https://")
    log.info(f"🌐 Public URL: {url}")
    log.info(f"🔗 Retell Custom LLM URL (paste this): wss://{url.replace('https://', '')}/ws/{{call_id}}")
    return url

if __name__ == "__main__":
    start_ngrok(8000)
    log.info("🚀 Running FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

