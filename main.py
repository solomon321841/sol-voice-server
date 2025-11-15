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
log = logging.getLogger("silas")

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
# 🤖 MODELS
# =====================================================
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

STT_MODEL = "gpt-4o-mini-transcribe"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "verse"          # best “human” voice
BRAIN_MODEL = "gpt-4.5"      # Silas intelligence model

# =====================================================
# ⚙️ FASTAPI APP
# =====================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def home():
    return {"status": "running", "message": "Silas backend online (GPT-4.5)"}

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
                js = r.json()
                return js if isinstance(js, list) else []
    except Exception as e:
        log.error(f"mem0_search error: {e}")

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
        log.error(f"mem0_add error: {e}")

def memory_context(memories: list):
    if not memories:
        return ""
    out = []
    for m in memories:
        txt = m.get("memory") or m.get("content") or m.get("text")
        if txt:
            out.append(f"- {txt}")
    return "Relevant memories:\n" + "\n".join(out)

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
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()

            blocks = r.json().get("results", [])
            out = []
            for b in blocks:
                if b.get("type") == "paragraph":
                    rich = b["paragraph"]["rich_text"]
                    text = "".join([t.get("plain_text", "") for t in rich])
                    if text.strip():
                        out.append(text)

            return "\n".join(out).strip() or "You are Silas."
    except Exception as e:
        log.error(f"Notion error: {e}")
        return "You are Silas."

# =====================================================
# 🔹 /prompt ENDPOINT
# =====================================================
@app.get("/prompt", response_class=PlainTextResponse)
async def prompt_endpoint():
    text = await get_notion_prompt()
    return PlainTextResponse(text, headers={"Access-Control-Allow-Origin": "*"})

# =====================================================
# 🔊 n8n HELPERS
# =====================================================
async def send_to_n8n(url: str, message: str):
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json={"message": message})

            if r.status_code == 200:
                try:
                    data = r.json()
                except:
                    return r.text.strip()

                if isinstance(data, dict):
                    return (
                        data.get("reply")
                        or data.get("message")
                        or data.get("text")
                        or data.get("output")
                        or json.dumps(data)
                    ).strip()

                if isinstance(data, list):
                    return " ".join(str(x) for x in data)

                return str(data)
    except Exception as e:
        log.error(f"n8n error: {e}")

    return "Sorry, automation error."

# =====================================================
# 🎤 REAL-TIME WS HANDLER (NO RETELL)
# =====================================================
@app.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    user_id = "solomon_roth"

    # Greeting
    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I'm Silas."
    await ws.send_text(json.dumps({"type": "text", "content": greet}))

    # Duplicate protection
    recent_msgs = []
    processed_messages = set()

    calendar_kw = ["calendar", "schedule", "appointment", "meeting"]
    plate_kw = ["plate", "task", "add", "todo", "notion", "list"]
    add_kw = ["add", "put", "create", "include", "new"]
    check_kw = ["what", "show", "see", "read", "check"]

    add_phrases = [
        "Of course boss. Doing that now.",
        "Gotcha. One sec.",
        "On it.",
        "Adding that now.",
    ]
    check_phrases = [
        "Let me check that...",
        "Pulling that up...",
        "One moment...",
    ]
    calendar_phrases = [
        "Checking your schedule...",
        "Let me see...",
    ]

    try:
        while True:
            # Receive raw audio bytes
            audio_bytes = await ws.receive_bytes()

            # STT -> text
            try:
                stt = await openai_client.audio.transcriptions.create(
                    model=STT_MODEL,
                    file=("audio.wav", audio_bytes, "audio/wav")
                )
                msg = stt.get("text", "").strip()
            except:
                await ws.send_text(json.dumps({"type": "text", "content": "I couldn't hear that"}))
                continue

            if not msg:
                continue

            # Duplicate protection
            lower = msg.lower().strip()
            now = time.time()
            recent_msgs = [(m, t) for (m, t) in recent_msgs if now - t < 2]
            if any(lower == old for (old, t) in recent_msgs):
                continue
            recent_msgs.append((lower, now))

            # Memory
            mems = await mem0_search(user_id, msg)
            facts = memory_context(mems)
            sys_prompt = f"{prompt}\n\nFacts:\n{facts}"

            # Routing
            if any(k in lower for k in plate_kw):
                if msg in processed_messages:
                    continue
                processed_messages.add(msg)

                if any(k in lower for k in add_kw):
                    phrase = random.choice(add_phrases)
                elif any(k in lower for k in check_kw):
                    phrase = random.choice(check_phrases)
                else:
                    phrase = "Let me handle that."

                await ws.send_text(json.dumps({"type": "text", "content": phrase}))

                reply = await send_to_n8n(N8N_PLATE_URL, msg)
                tts_audio = await openai_client.audio.speech.create(
                    model=TTS_MODEL,
                    voice=TTS_VOICE,
                    input=reply
                )
                await ws.send_bytes(tts_audio)
                continue

            if any(k in lower for k in calendar_kw):
                phrase = random.choice(calendar_phrases)
                await ws.send_text(json.dumps({"type": "text", "content": phrase}))

                reply = await send_to_n8n(N8N_CALENDAR_URL, msg)
                tts_audio = await openai_client.audio.speech.create(
                    model=TTS_MODEL,
                    voice=TTS_VOICE,
                    input=reply
                )
                await ws.send_bytes(tts_audio)
                continue

            # Default GPT-4.5 chat (STREAMING)
            try:
                stream = await openai_client.chat.completions.create(
                    model=BRAIN_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": msg},
                    ],
                    stream=True,
                )

                buffer = ""
                async for chunk in stream:
                    delta = getattr(chunk.choices[0].delta, "content", None)
                    if delta:
                        buffer += delta

                        # Stream when we hit sentence boundary
                        if buffer.endswith((". ", "!", "?")):
                            tts_audio = await openai_client.audio.speech.create(
                                model=TTS_MODEL,
                                voice=TTS_VOICE,
                                input=buffer
                            )
                            await ws.send_bytes(tts_audio)
                            buffer = ""

                # leftover
                if buffer.strip():
                    tts_audio = await openai_client.audio.speech.create(
                        model=TTS_MODEL,
                        voice=TTS_VOICE,
                        input=buffer
                    )
                    await ws.send_bytes(tts_audio)

                asyncio.create_task(mem0_add(user_id, msg))

            except Exception as e:
                log.error(f"Chat error: {e}")
                await ws.send_text(json.dumps({"type": "text", "content": "Small issue occurred"}))

    except WebSocketDisconnect:
        log.info("Client disconnected.")
    except Exception as e:
        log.error(f"WS error: {e}")

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

