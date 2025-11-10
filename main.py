import os
import json
import logging
import asyncio
import time
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from openai import AsyncOpenAI

# =====================================================
# 🔧 LOGGING
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")

# =====================================================
# 🔑 ENV
# =====================================================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

# =====================================================
# 🌐 n8n ENDPOINTS
# =====================================================
N8N_CALENDAR_URL = "https://n8n.marshall321.org/webhook/calendar-agent"
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

# =====================================================
# 🤖 MODEL
# =====================================================
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
GPT_MODEL = "gpt-4o-mini"

# =====================================================
# ⚙️ FASTAPI APP
# =====================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.get("/")
async def home():
    return {"status": "running", "message": "Silas backend is online."}

@app.get("/health")
async def health():
    return {"ok": True}

# =====================================================
# 🧠 MEM0 MEMORY
# =====================================================
async def mem0_search(user_id: str, query: str):
    if not MEMO_API_KEY:
        return []
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"filters": {"user_id": user_id}, "query": query}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.mem0.ai/v2/memories/", headers=headers, json=payload)
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        log.error(f"MEM0 search error: {e}")
    return []

async def mem0_add(user_id: str, text: str):
    if not MEMO_API_KEY or not text:
        return
    headers = {"Authorization": f"Token {MEMO_API_KEY}"}
    payload = {"user_id": user_id, "messages": [{"role": "user", "content": text}]}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post("https://api.mem0.ai/v1/memories/", headers=headers, json=payload)
    except Exception as e:
        log.error(f"MEM0 add error: {e}")

def memory_context(memories: list) -> str:
    if not memories:
        return ""
    lines = []
    for m in memories:
        if isinstance(m, dict):
            txt = m.get("memory") or m.get("content") or m.get("text")
            if txt:
                lines.append(f"- {txt}")
    return "Relevant memories:\n" + "\n".join(lines)

# =====================================================
# 🧩 NOTION PROMPT
# =====================================================
async def get_notion_prompt():
    if not NOTION_PAGE_ID or not NOTION_API_KEY:
        return "You are Solomon Roth’s personal AI assistant, Silas."
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            parts = []
            for blk in data.get("results", []):
                if blk.get("type") == "paragraph":
                    txt = "".join([r.get("plain_text", "") for r in blk["paragraph"].get("rich_text", [])])
                    parts.append(txt)
            return "\n".join(parts).strip() or "You are Solomon Roth’s AI assistant, Silas."
    except Exception as e:
        log.error(f"❌ Notion error: {e}")
        return "You are Solomon Roth’s AI assistant, Silas."

# =====================================================
# 🔹 /prompt ENDPOINT FOR ADMIN PANEL
# =====================================================
@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt_text():
    text = await get_notion_prompt()
    headers = {"Access-Control-Allow-Origin": "*"}
    return PlainTextResponse(text, headers=headers)

# =====================================================
# 🧩 n8n HELPERS
# =====================================================
async def send_to_n8n(url: str, message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            payload = {"message": message}
            r = await c.post(url, json=payload)
            log.info(f"📩 n8n raw response ({url}): {r.text}")

            if r.status_code == 200:
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        return (
                            data.get("reply")
                            or data.get("message")
                            or data.get("text")
                            or data.get("output")
                            or json.dumps(data, indent=2)
                        ).strip()
                    elif isinstance(data, list):
                        return " ".join(str(x) for x in data).strip()
                    else:
                        return str(data).strip()
                except Exception:
                    return r.text.strip()
            else:
                log.warning(f"⚠️ n8n returned {r.status_code}: {r.text}")
                return "Sorry, the automation returned an unexpected response."
    except Exception as e:
        log.error(f"n8n error: {e}")
        return "Sorry, couldn't reach automation."

# =====================================================
# 🔌 RETELL WS (Text-based debounce)
# =====================================================
connections = {}

@app.websocket("/ws/{call_id}")
async def ws_handler(ws: WebSocket, call_id: str):
    # Close any existing call_id connection
    if call_id in connections:
        try:
            await connections[call_id]["ws"].close()
        except Exception:
            pass

    connections[call_id] = {"ws": ws}
    await ws.accept()
    user_id = "solomon_roth"

    async def speak(resp_id, text, end=True):
        payload = {
            "type": "response_message",
            "response_id": resp_id,
            "content": text,
            "content_complete": end,
            "end_turn": end,
        }
        await ws.send_text(json.dumps(payload))
        log.info(f"🗣️ {text[:80]}")

    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I’m Silas."
    await speak(0, greet)

    last_msg = {"t": None, "time": 0}
    calendar_kw = ["calendar", "meeting", "schedule", "appointment"]
    plate_kw = ["plate", "add", "to-do", "task", "notion"]

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            trans = data.get("transcript", [])
            inter = data.get("interaction_type")
            rid = int(data.get("response_id", 1))
            msg = ""
            for t in reversed(trans or []):
                if t.get("role") == "user":
                    msg = t.get("content", "").strip()
                    break
            if not (inter == "response_required" and msg):
                continue

            # 🧩 Strong text-based debounce
            now = time.time()
            same = msg.lower() == (last_msg["t"] or "").lower()
            subset = (
                msg.lower().startswith((last_msg["t"] or "").lower())
                or (last_msg["t"] or "").lower().startswith(msg.lower())
            )
            if (same or subset) and now - last_msg["time"] < 2:
                log.info("🛑 Skipping near-duplicate text.")
                continue
            last_msg = {"t": msg, "time": now}

            mems = await mem0_search(user_id, msg)
            ctx = memory_context(mems)
            sys_prompt = f"{prompt}\n\nFacts:\n{ctx}"

            if any(k in msg.lower() for k in plate_kw):
                await speak(rid, "On it...", end=False)
                rep = await send_to_n8n(N8N_PLATE_URL, msg)
                await speak(rid, rep)
                continue

            if any(k in msg.lower() for k in calendar_kw):
                await speak(rid, "Checking your schedule...", end=False)
                rep = await send_to_n8n(N8N_CALENDAR_URL, msg)
                await speak(rid, rep)
                continue

            try:
                stream = await openai_client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": msg},
                    ],
                    max_tokens=150,
                    temperature=0.7,
                    stream=True,
                )
                async for chunk in stream:
                    delta = getattr(chunk.choices[0].delta, "content", None)
                    if delta:
                        await speak(rid, delta, end=False)
                await speak(rid, "", end=True)
                asyncio.create_task(mem0_add(user_id, msg))
            except Exception as e:
                log.error(f"LLM stream error: {e}")
                await speak(rid, "Sorry, I hit a small issue.")
    except WebSocketDisconnect:
        log.info(f"❌ Disconnected {call_id}")
    finally:
        connections.pop(call_id, None)

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

