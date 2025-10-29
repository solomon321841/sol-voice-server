import os
import json
import logging
import asyncio
from typing import List, Dict, Optional

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# ─────────────────────────────────────────────────────
# Env
# ─────────────────────────────────────────────────────
load_dotenv()
CEREBRAS_API_KEY = (os.getenv("CEREBRAS_API_KEY") or "").strip()
MEMO_API_KEY     = (os.getenv("MEMO_API_KEY") or os.getenv("MEM0_API_KEY") or "").strip()

# ─────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────
# Mem0 helpers  (unchanged protocol; still Token auth)
# ─────────────────────────────────────────────────────
# v2 search with filters + optional query
async def mem0_search_v2(user_id: str, query: str) -> list:
    if not MEMO_API_KEY:
        return []
    url = "https://api.mem0.ai/v2/memories/"
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"filters": {"user_id": user_id}}
    if query:
        payload["query"] = query

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 200 and isinstance(r.json(), list):
            data = r.json()
            log.info(f"🧠 Found {len(data)} memories for {user_id}")
            return data
        else:
            log.warning(f"⚠️ Mem0 v2 search failed ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        log.warning(f"Mem0 v2 search error: {e}")
    return []

# v1 add with messages schema (same as your working version)
async def mem0_add_v1(user_id: str, text: str) -> None:
    if not MEMO_API_KEY or not text:
        return
    url = "https://api.mem0.ai/v1/memories/"
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"user_id": user_id, "messages": [{"role": "user", "content": text}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            log.info(f"✅ Memory added for {user_id}")
        else:
            log.warning(f"⚠️ Mem0 add failed ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        log.warning(f"Mem0 add error: {e}")

def build_memory_context(items: list) -> str:
    if not items:
        return ""
    lines = []
    for it in items:
        if isinstance(it, dict):
            content = it.get("memory") or it.get("content") or it.get("text")
            if content:
                lines.append(f"- {content}")
    return ("Known facts about Solomon (use only if relevant):\n" + "\n".join(lines)) if lines else ""

# ─────────────────────────────────────────────────────
# LLM (Cerebras) — faster model + tighter timeouts
# ─────────────────────────────────────────────────────
CEREBRAS_MODEL = "llama3.1-8b-instruct"   # fast + capable

async def cerebras_chat(messages: List[Dict[str, str]]) -> str:
    if not CEREBRAS_API_KEY:
        return "Missing CEREBRAS_API_KEY."
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": CEREBRAS_MODEL,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 180,   # keep answers tight to reduce latency
    }
    try:
        # low latency client + no retries to avoid extra lag
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=7.0, write=7.0, pool=3.0)) as client:
            r = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            res = r.json()
            return res["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        return "Sorry, I hit a speed bump. Try again?"

# ─────────────────────────────────────────────────────
# Retell WebSocket
# ─────────────────────────────────────────────────────
@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    await websocket.accept()
    log.info(f"🔌 Retell WebSocket connected: {call_id}")
    user_id = "solomon_roth"

    async def send_speech(response_id: int, text: str, end_turn: bool = True, complete: bool = True):
        # Retell expects a string in 'content', and booleans for flags.
        payload = {
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": bool(complete),
            "end_turn": bool(end_turn),
        }
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ Sent speech response: {text[:100]}")

    # tiny “ready” ping helps perceived latency
    await send_speech(0, "Ready. Go ahead.")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            interaction_type = data.get("interaction_type")
            response_id = int(data.get("response_id", 1))

            # get latest user message from transcript
            user_message = ""
            transcript = data.get("transcript", [])
            if transcript and isinstance(transcript, list):
                for t in reversed(transcript):
                    if t.get("role") == "user":
                        user_message = (t.get("content") or "").strip()
                        break

            if interaction_type == "response_required":
                # Fetch memories quickly (short timeout); if slow, continue anyway
                mem_items = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mem_items)

                # super short system prompt to reduce token latency
                system_prompt = (
                    "You are Andrew, Solomon’s personal voice assistant. "
                    "Be concise, friendly, and direct. Use the facts below only if helpful.\n"
                    f"{context}"
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message or "Hello?"},
                ]

                # call the fast model
                reply = await cerebras_chat(messages)

                # send once (fully); safe for Retell
                await send_speech(response_id, reply, end_turn=True, complete=True)

                # save memory in the background (non-blocking)
                if user_message:
                    asyncio.create_task(mem0_add_v1(user_id, user_message))

    except WebSocketDisconnect:
        log.info(f"❌ Retell WebSocket disconnected: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# ─────────────────────────────────────────────────────
# Server entry
# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    # On Render, PORT is provided. Locally defaults to 8000.
    port = int(os.getenv("PORT", "8000"))
    host = "0.0.0.0"
    log.info("🚀 Running FastAPI server...")
    uvicorn.run(app, host=host, port=port)

