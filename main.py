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
GPT_MODEL = "gpt-4o"

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
                out = r.json()
                return out if isinstance(out, list) else []
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
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children}"
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
                    parts.append("".join([t.get("plain_text", "") for t in blk["paragraph"]["rich_text"]]))
            return "\n".join(parts).strip() or "You are Solomon Roth’s AI assistant, Silas."
    except Exception:
        return "You are Solomon Roth’s AI assistant, Silas."

@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt_text():
    txt = await get_notion_prompt()
    return PlainTextResponse(txt, headers={"Access-Control-Allow-Origin": "*"})

# =====================================================
# 🔧 CLEAN NORMALIZATION AND DEDUP
# =====================================================
def _normalize(m: str):
    m = m.lower().strip()
    m = "".join(ch for ch in m if ch not in string.punctuation)
    return " ".join(m.split())

def _is_similar(a: str, b: str):
    return bool(a and b and (a == b or a in b or b in a))

# =====================================================
# 🎤 WEBSOCKET FIXED VERSION (NO PLATE LOGIC CHANGES)
# =====================================================
@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    user_id = "solomon_roth"
    recent = []
    processed = set()

    # === Load prompt & fix greeting ===
    prompt = await get_notion_prompt()
    greet = "Hey Solomon, I'm Silas."

    # Speak greeting with TTS (no JSON)
    try:
        t = await openai_client.audio.speech.create(
            model="gpt-4o-mini-tts", voice="alloy", input=greet
        )
        await ws.send_bytes(await t.aread())
    except:
        pass

    try:
        while True:

            audio = await ws.receive_bytes()

            # ======= FIXED STT =======
            try:
                stt = await openai_client.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe",
                    file=("audio.webm", audio, "audio/webm")
                )
                msg = getattr(stt, "text", "").strip()
            except:
                continue

            if not msg:
                continue

            norm = _normalize(msg)
            now = time.time()

            # strong duplicate filtering
            recent = [(m, t) for m, t in recent if now - t < 2]
            if any(_is_similar(m, norm) for m, t in recent):
                continue
            if norm in processed:
                continue

            processed.add(norm)
            recent.append((norm, now))

            # memory / system prompt
            mems = await mem0_search(user_id, msg)
            ctx = memory_context(mems)
            sys_prompt = f"{prompt}\n\nFacts:\n{ctx}"
            lower = msg.lower()

            # =====================================================
            # =============     PLATE LOGIC UNTOUCHED    ==========
            # =====================================================
            plate_kw = ["plate", "add", "to-do", "task", "notion", "list"]
            plate_add_kw = ["add", "put", "create", "new", "include"]
            plate_check_kw = ["what", "show", "see", "check", "read"]

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

            if any(k in lower for k in plate_kw):

                if msg in processed:
                    continue
                processed.add(msg)

                phrase = (
                    random.choice(add_phrases) if any(k in lower for k in plate_add_kw)
                    else random.choice(check_phrases) if any(k in lower for k in plate_check_kw)
                    else "Let me handle that..."
                )

                # speak the working phrase
                try:
                    t = await openai_client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice="alloy",
                        input=phrase
                    )
                    await ws.send_bytes(await t.aread())
                except:
                    pass

                reply = await send_to_n8n(N8N_PLATE_URL, msg)

                # final tts
                try:
                    t2 = await openai_client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice="alloy",
                        input=reply
                    )
                    await ws.send_bytes(await t2.aread())
                except:
                    pass

                continue

            # =====================================================
            # ==================== CALENDAR LOGIC =================
            # =====================================================
            calendar_kw = ["calendar", "meeting", "schedule", "appointment"]
            calendar_phrases = [
                "Let me check your schedule real quick...",
                "Just a second while I pull that up...",
                "Alright, let’s take a look at your calendar...",
                "Okay, seeing what’s on your agenda...",
            ]

            if any(k in lower for k in calendar_kw):

                try:
                    t = await openai_client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice="alloy",
                        input=random.choice(calendar_phrases)
                    )
                    await ws.send_bytes(await t.aread())
                except:
                    pass

                reply = await send_to_n8n(N8N_CALENDAR_URL, msg)

                try:
                    t2 = await openai_client.audio.speech.create(
                        model="gpt-4o-mini-tts",
                        voice="alloy",
                        input=reply
                    )
                    await ws.send_bytes(await t2.aread())
                except:
                    pass

                continue

            # =====================================================
            # ==================== NORMAL CHAT ====================
            # =====================================================
            try:
                stream = await openai_client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": msg},
                    ],
                    stream=True,
                )

                buf = ""

                async for chunk in stream:
                    delta = getattr(chunk.choices[0].delta, "content", "")
                    if not delta:
                        continue

                    buf += delta

                    if buf.endswith((". ", "?", "!")):
                        try:
                            t = await openai_client.audio.speech.create(
                                model="gpt-4o-mini-tts",
                                voice="alloy",
                                input=buf
                            )
                            await ws.send_bytes(await t.aread())
                        except:
                            pass
                        buf = ""

                if buf.strip():
                    try:
                        t = await openai_client.audio.speech.create(
                            model="gpt-4o-mini-tts",
                            voice="alloy",
                            input=buf
                        )
                        await ws.send_bytes(await t.aread())
                    except:
                        pass

                asyncio.create_task(mem0_add(user_id, msg))

            except Exception as e:
                log.error(f"LLM error: {e}")

    except WebSocketDisconnect:
        pass

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

