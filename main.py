import os
import json
import logging
import uuid
import asyncio
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pyngrok import ngrok

# ============ Logging ============
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# ============ Env ============
load_dotenv()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip() or os.getenv("MEM0_API_KEY", "").strip()

# ============ FastAPI ============
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
            if isinstance(data, list):
                log.info(f"🧠 Found {len(data)} memories for {user_id}")
                return data
        else:
            log.warning(f"⚠️ Mem0 search failed ({r.status_code}): {r.text}")
    except Exception as e:
        log.error(f"🔥 Mem0 search error: {e}")
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
        else:
            log.warning(f"⚠️ Mem0 add failed ({r.status_code}): {r.text}")
    except Exception as e:
        log.error(f"🔥 Error adding memory: {e}")

def build_memory_context(items: list) -> str:
    if not items:
        return ""
    lines = []
    for it in items:
        content = it.get("memory") or it.get("content") or it.get("text")
        if content:
            lines.append(f"- {content}")
    return "Relevant memories (use only if helpful):\n" + "\n".join(lines) if lines else ""

# =====================================================
# ⚡ STREAMING CEREBRAS CHAT
# =====================================================
CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"

async def cerebras_chat(messages: List[Dict[str, str]], on_chunk=None) -> str:
    """Stream text tokens from Cerebras as they generate."""
    if not CEREBRAS_API_KEY:
        return "Cerebras key missing."

    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    data = {"model": CEREBRAS_MODEL, "messages": messages, "stream": True}
    full_text = ""

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                "https://api.cerebras.ai/v1/chat/completions",
                headers=headers,
                json=data,
            ) as response:
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        payload = json.loads(chunk)
                        delta = (
                            payload.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            full_text += delta
                            if on_chunk:
                                await on_chunk(delta)
                    except Exception:
                        continue
    except Exception as e:
        log.error(f"⚠️ Cerebras stream error: {e}")

    return full_text

# =====================================================
# 🔌 RETELL WEBSOCKET
# =====================================================
@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
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

    await send_speech(0, "Hello. I'm ready. What can I do for you?")

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
                mem_items = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mem_items)
                system_prompt = (
                    "You are Solomon Roth’s personal AI assistant.\n"
                    "The following are true remembered facts about Solomon. "
                    "Do not say you don't know them if they're listed below.\n"
                    f"{context}\n"
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message or "Hello?"},
                ]

                partial_reply = ""

                async def on_chunk(delta: str):
                    nonlocal partial_reply
                    partial_reply += delta
                    await send_speech(response_id, delta, end_turn=False)

                reply = await cerebras_chat(messages, on_chunk=on_chunk)
                await send_speech(response_id, "", end_turn=True)

                if user_message:
                    asyncio.create_task(mem0_add_v1(user_id, user_message))

    except WebSocketDisconnect:
        log.info(f"❌ Retell WebSocket disconnected: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# =====================================================
# 🚀 SERVER
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
