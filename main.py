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
GPT_MODEL = "gpt-4.5-mini"
TTS_MODEL = "gpt-4.5-tts"
STT_MODEL = "gpt-4.5-transcribe"

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
# 🧠 MEM0
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

def memory_context(memories):
    if not memories:
        return ""
    out = []
    for m in memories:
        txt = m.get("memory") or m.get("content") or m.get("text")
        if txt:
            out.append("- " + txt)
    return "Relevant memories:\n" + "\n".join(out)

# =====================================================
# 🧩 NOTION PROMPT
# =====================================================
async def get_notion_prompt():
    if not NOTION_PAGE_ID or not NOTION_API_KEY:
        return "You are Silas."
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
            out = []
            for blk in data.get("results", []):
                if blk["type"] == "paragraph":
                    t = "".join([x["plain_text"] for x in blk["paragraph"]["rich_text"]])
                    out.append(t)
            return "\n".join(out) or "You are Silas."
    except Exception as e:
        log.error(f"Notion error: {e}")
        return "You are Silas."

# =====================================================
# 🔹 /prompt
# =====================================================
@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt_text():
    return PlainTextResponse(await get_notion_prompt(), headers={"Access-Control-Allow-Origin": "*"})

# =====================================================
# 🧩 n8n HELPERS
# =====================================================
async def send_to_n8n(url, message):
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json={"message": message})
            log.info("n8n raw: " + r.text)

            if r.status_code == 200:
                try:
                    d = r.json()
                    if isinstance(d, dict):
                        return (
                            d.get("reply") or
                            d.get("message") or
                            d.get("output") or
                            d.get("text") or
                            json.dumps(d)
                        )
                    if isinstance(d, list):
                        return " ".join(str(x) for x in d)
                except:
                    return r.text
            return "Unexpected automation response."
    except Exception as e:
        log.error(f"n8n error: {e}")
        return "Automation unreachable."

# =====================================================
# 🎤 UTILITIES
# =====================================================
def _normalize(msg):
    msg = msg.lower().strip()
    for p in string.punctuation:
        msg = msg.replace(p, "")
    return " ".join(msg.split())

def _is_similar(a, b):
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a) or a in b or b in a

# =====================================================
# 🎤 WS HANDLER
# =====================================================
@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    user_id = "solomon_roth"
    recent_msgs = []
    processed = set()

    calendar_kw = ["calendar", "meeting", "schedule", "appointment"]
    plate_kw = ["plate", "add", "to-do", "task", "notion", "list"]
    plate_add_kw = ["add", "put", "create", "new", "include"]
    plate_check_kw = ["what", "show", "see", "check", "read"]

    add_ph = [
        "Of course boss. Doing that now.",
        "Gotcha. Give me one sec.",
        "Okay. Adding that.",
        "Done. Putting that on your plate."
    ]
    check_ph = [
        "Checking your plate…",
        "One moment…",
        "Let’s see what’s on there…"
    ]
    cal_ph = [
        "Checking your schedule…",
        "Let me pull that up…"
    ]

    # greeting
    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I'm Silas."
    await ws.send_text(json.dumps({"type": "text", "content": greet}))

    try:
        while True:
            # raw audio from browser (WEBM/OPUS)
            data = await ws.receive_bytes()

            # =====================================================
            # ✅ FIXED STT (correct WEBM input)
            # =====================================================
            try:
                stt = await openai_client.audio.transcriptions.create(
                    file=("audio.webm", data, "audio/webm"),
                    model=STT_MODEL
                )
                msg = stt.text.strip()
            except Exception as e:
                log.error(f"STT error: {e}")
                await ws.send_text(json.dumps({"type": "text", "content": "I couldn’t hear that."}))
                continue

            if not msg:
                continue

            # duplicate filtering
            norm = _normalize(msg)
            now = time.time()
            recent_msgs = [(m, ts) for (m, ts) in recent_msgs if now - ts < 2]
            if any(_is_similar(m, norm) for (m, ts) in recent_msgs):
                continue
            recent_msgs.append((norm, now))

            mems = await mem0_search(user_id, msg)
            ctx = memory_context(mems)
            sys = f"{prompt}\n\nFacts:\n{ctx}"
            low = msg.lower()

            # PLATE
            if any(k in low for k in plate_kw):

                if msg in processed:
                    continue
                processed.add(msg)

                ph = random.choice(add_ph if any(k in low for k in plate_add_kw) else check_ph)

                await ws.send_text(json.dumps({"type": "text", "content": ph}))
                n8 = await send_to_n8n(N8N_PLATE_URL, msg)

                # =====================================================
                # ✅ FIXED TTS (new OpenAI API)
                # =====================================================
                audio = await openai_client.audio.speech.create(
                    model=TTS_MODEL,
                    voice="alloy",
                    input=n8
                )
                await ws.send_bytes(audio)
                continue

            # CALENDAR
            if any(k in low for k in calendar_kw):

                await ws.send_text(json.dumps({"type": "text", "content": random.choice(cal_ph)}))
                n8 = await send_to_n8n(N8N_CALENDAR_URL, msg)

                audio = await openai_client.audio.speech.create(
                    model=TTS_MODEL,
                    voice="alloy",
                    input=n8
                )
                await ws.send_bytes(audio)
                continue

            # DEFAULT CHAT
            try:
                stream = await openai_client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys},
                        {"role": "user", "content": msg},
                    ],
                    stream=True,
                )
                buf = ""
                async for chunk in stream:
                    d = getattr(chunk.choices[0].delta, "content", None)
                    if d:
                        buf += d
                        if buf.endswith(". ") or buf.endswith("!") or buf.endswith("?"):
                            audio = await openai_client.audio.speech.create(
                                model=TTS_MODEL,
                                voice="alloy",
                                input=buf
                            )
                            await ws.send_bytes(audio)
                            buf = ""

                if buf.strip():
                    audio = await openai_client.audio.speech.create(
                        model=TTS_MODEL,
                        voice="alloy",
                        input=buf
                    )
                    await ws.send_bytes(audio)

                asyncio.create_task(mem0_add(user_id, msg))

            except Exception as e:
                log.error(f"LLM error: {e}")
                await ws.send_text(json.dumps({"type": "text", "content": "Small issue on my end."}))

    except WebSocketDisconnect:
        log.info("Client disconnected.")

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

