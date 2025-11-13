import os
import json
import logging
import asyncio
import time
import random
import string
from typing import List, Dict
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
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
# 🔹 /prompt ENDPOINT
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
# 🔌 RETELL WS — Single Voice Connection + Strong Debounce + Inactivity Timeout
# =====================================================
connections = {}

def _normalize_message(msg: str) -> str:
    """Normalize text so tiny differences don't cause duplicate sends."""
    msg = msg.lower().strip()
    # Remove punctuation so "hello." and "hello" look the same
    msg = "".join(ch for ch in msg if ch not in string.punctuation)
    # Collapse whitespace
    msg = " ".join(msg.split())
    return msg

def _is_similar(a: str, b: str) -> bool:
    """
    Treat messages as the same intent if they are equal OR
    one is basically a prefix/extension of the other.
    Example:
      a = "add buy groceries"
      b = "add buy groceries to my plate for sunday"
    -> similar
    """
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)

INACTIVITY_TIMEOUT_SECONDS = 2.0  # ~2s after last activity → force close

@app.websocket("/ws/{call_id}")
async def ws_handler(ws: WebSocket, call_id: str):
    # 🧩 Always close any previous active connection to prevent double voice
    for cid, conn in list(connections.items()):
        try:
            if conn["ws"].client_state.name == "CONNECTED":
                log.info(f"🔇 Closing previous WebSocket: {cid}")
                await conn["ws"].close()
        except Exception:
            pass
        connections.pop(cid, None)

    connections[call_id] = {"ws": ws, "active": True}
    await ws.accept()
    user_id = "solomon_roth"

    # Track last activity time for inactivity timeout
    last_activity = time.time()

    async def speak(resp_id, text, end=True):
        nonlocal last_activity
        if not connections.get(call_id, {}).get("active"):
            return
        payload = {
            "type": "response_message",
            "response_id": resp_id,
            "content": text,
            "content_complete": end,
            "end_turn": end,
        }
        try:
            await ws.send_text(json.dumps(payload))
            last_activity = time.time()
            log.info(f"🗣️ {text[:80]}")
        except Exception:
            log.warning("Attempted to speak after disconnect — ignored.")

    # 🔍 Inactivity watchdog
    async def inactivity_watchdog():
        nonlocal last_activity
        try:
            while connections.get(call_id, {}).get("active"):
                await asyncio.sleep(0.5)
                if time.time() - last_activity > INACTIVITY_TIMEOUT_SECONDS:
                    log.info(f"⏹️ Inactivity timeout reached for {call_id}, closing WebSocket.")
                    # Mark inactive first so loops stop
                    if call_id in connections:
                        connections[call_id]["active"] = False
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break
        except Exception as e:
            log.error(f"Watchdog error for {call_id}: {e}")

    # Start watchdog
    asyncio.create_task(inactivity_watchdog())

    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I’m Silas."
    await speak(0, greet)

    # 🔹 Track recent normalized messages to prevent double n8n calls
    recent_msgs: list[tuple[str, float]] = []

    calendar_kw = ["calendar", "meeting", "schedule", "appointment"]
    plate_kw = ["plate", "add", "to-do", "task", "notion", "list"]

    plate_add_kw = ["add", "put", "create", "new", "include"]
    plate_check_kw = ["what", "show", "see", "check", "read"]

    # 🔹 Your custom pre-responses for ADD-to-plate
    add_phrases = [
        "Of course boss. Doing that now.",
        "Gotcha. Give me one sec.",
        "Of course. Adding that now.",
        "Okay. Putting that on your plate.",
        "Not a problem. I’ll be right back.",
    ]

    check_phrases = [
        "Let’s see what’s on your plate...",
        "One moment, checking that for you...",
        "Alright, here’s what you’ve got...",
        "Give me a sec, pulling that up...",
    ]
    calendar_phrases = [
        "Let me check your schedule real quick...",
        "Just a second while I pull that up...",
        "Alright, let’s take a look at your calendar...",
        "Okay, seeing what’s on your agenda...",
    ]

    try:
        while True:
            raw = await ws.receive_text()
            last_activity = time.time()
            if not connections.get(call_id, {}).get("active"):
                break

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

            # 🔹 Strong debouncing: ignore near-identical / prefix repeats within 5 seconds
            norm = _normalize_message(msg)
            now = time.time()
            # drop old entries
            recent_msgs = [(m, ts) for (m, ts) in recent_msgs if now - ts < 5]

            if any(_is_similar(m, norm) for (m, ts) in recent_msgs):
                log.info(f"🛑 Skipping debounced *similar* message: {msg}")
                continue

            recent_msgs.append((norm, now))

            mems = await mem0_search(user_id, msg)
            ctx = memory_context(mems)
            sys_prompt = f"{prompt}\n\nFacts:\n{ctx}"
            lower_msg = msg.lower()

            # 🧩 PLATE HANDLING
            if any(k in lower_msg for k in plate_kw):
                if any(k in lower_msg for k in plate_add_kw):
                    # ADD flow → use your 5 custom "working on it" responses
                    phrase = random.choice(add_phrases)
                elif any(k in lower_msg for k in plate_check_kw):
                    # CHECK flow → keep existing "checking" phrases
                    phrase = random.choice(check_phrases)
                else:
                    phrase = "Let me handle that..."
                # 1) Immediately say working phrase
                await speak(rid, phrase, end=False)
                # 2) Then call n8n and speak its reply (UNCHANGED)
                rep = await send_to_n8n(N8N_PLATE_URL, msg)
                await speak(rid, rep)
                continue

            # 🧩 CALENDAR HANDLING
            if any(k in lower_msg for k in calendar_kw):
                await speak(rid, random.choice(calendar_phrases), end=False)
                rep = await send_to_n8n(N8N_CALENDAR_URL, msg)
                await speak(rid, rep)
                continue

            # 🧠 Default AI Chat Response
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
        if call_id in connections:
            connections[call_id]["active"] = False
            connections.pop(call_id, None)
        try:
            await ws.close()
        except Exception:
            pass
        log.info("🧹 Cleaned up connection completely.")

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

