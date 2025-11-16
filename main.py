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
# ⭐ FIXED — WAV HEADER BUILDER
# =====================================================
def create_wav_header(num_frames, sample_rate=24000, num_channels=1, bits_per_sample=16):
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = num_frames * num_channels * bits_per_sample // 8
    riff_chunk_size = 36 + data_size

    header = b"RIFF" + riff_chunk_size.to_bytes(4, "little") + b"WAVEfmt "
    header += (16).to_bytes(4, "little")            # PCM header size
    header += (1).to_bytes(2, "little")             # PCM format
    header += num_channels.to_bytes(2, "little")
    header += sample_rate.to_bytes(4, "little")
    header += byte_rate.to_bytes(4, "little")
    header += block_align.to_bytes(2, "little")
    header += bits_per_sample.to_bytes(2, "little")
    header += b"data" + data_size.to_bytes(4, "little")
    return header

# =====================================================
# ⭐ FIXED — STT (Now Accepts RAW PCM → WAV)
# =====================================================
async def transcribe_pcm(pcm_bytes: bytes):
    try:
        num_frames = len(pcm_bytes) // 2
        wav_data = create_wav_header(num_frames) + pcm_bytes

        resp = await openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.wav", wav_data, "audio/wav")
        )
        return resp.text.strip()
    except Exception as e:
        log.error(f"❌ STT error: {e}")
        return ""

# =====================================================
# ⭐ FIXED — TTS (Now single voice, no glitching)
# =====================================================
async def speak_once(text: str) -> bytes:
    try:
        audio = await openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text
        )
        return audio.read()
    except Exception as e:
        log.error(f"❌ TTS error: {e}")
        return b""

# =====================================================
# MEM0, NOTION, N8N — UNCHANGED
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
        txt = m.get("memory") or m.get("content") or m.get("text")
        if txt:
            lines.append(f"- {txt}")
    return "Relevant memories:\n" + "\n".join(lines)

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
                    txt = "".join([r.get("plain_text", "") for r in blk["paragraph"]["rich_text"]])
                    parts.append(txt)
            return "\n".join(parts).strip() or "You are Solomon Roth’s AI assistant, Silas."
    except Exception as e:
        log.error(f"❌ Notion error: {e}")
        return "You are Solomon Roth’s AI assistant, Silas."

@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt_text():
    text = await get_notion_prompt()
    headers = {"Access-Control-Allow-Origin": "*"}
    return PlainTextResponse(text, headers=headers)

async def send_to_n8n(url: str, message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            payload = {"message": message}
            r = await c.post(url, json=payload)
            log.info(f"📩 n8n raw response: {r.text}")

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
                    if isinstance(data, list):
                        return " ".join(str(x) for x in data)
                    return str(data)
                except:
                    return r.text.strip()
            return "Sorry, the automation returned an unexpected response."

    except Exception as e:
        log.error(f"n8n error: {e}")
        return "Sorry, couldn't reach automation."

# =====================================================
# ⭐ FIXED — RAW PCM WS HANDLER
# =====================================================
@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()
    user_id = "solomon_roth"

    recent_msgs = []
    processed_messages = set()

    calendar_kw = ["calendar", "meeting", "schedule", "appointment"]
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
    calendar_phrases = [
        "Let me check your schedule real quick...",
        "Just a second while I pull that up...",
        "Alright, let’s take a look at your calendar...",
        "Okay, seeing what’s on your agenda...",
    ]

    # Initial greeting
    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I’m Silas."
    await ws.send_text(json.dumps({"type": "text", "content": greet}))

    # PCM buffer
    pcm_buffer = bytearray()

    try:
        while True:

            incoming = await ws.receive()

            # Bytes = PCM audio
            if "bytes" in incoming:
                pcm_buffer.extend(incoming["bytes"])
                continue

            # Text messages
            if "text" in incoming:
                msg_json = json.loads(incoming["text"])
                t = msg_json.get("type")

                # User finished speaking → run STT
                if t == "stop":
                    pcm = bytes(pcm_buffer)
                    pcm_buffer = bytearray()

                    transcript = await transcribe_pcm(pcm)
                    if not transcript:
                        await ws.send_text(json.dumps({"type":"text","content":"I couldn't hear that."}))
                        continue

                    msg = transcript
                else:
                    continue

            # The rest of your logic is UNTOUCHED:
            # =====================================================
            # Everything below is IDENTICAL to your file.
            # =====================================================

            norm = msg.lower().strip()
            now = time.time()
            recent_msgs = [(m, ts) for (m, ts) in recent_msgs if now - ts < 2]
            if any((norm in m or m in norm) for (m, ts) in recent_msgs):
                continue
            recent_msgs.append((norm, now))

            mems = await mem0_search(user_id, msg)
            ctx = memory_context(mems)
            sys_prompt = f"{prompt}\n\nFacts:\n{ctx}"
            lower_msg = msg.lower()

            # ===================
            # PLATE LOGIC
            # ===================
            if any(k in lower_msg for k in plate_kw):

                if msg in processed_messages:
                    continue
                processed_messages.add(msg)

                if any(k in lower_msg for k in plate_add_kw):
                    phrase = random.choice(add_phrases)
                elif any(k in lower_msg for k in plate_check_kw):
                    phrase = random.choice(check_phrases)
                else:
                    phrase = "Let me handle that..."

                await ws.send_text(json.dumps({"type": "text", "content": phrase}))
                n8n_reply = await send_to_n8n(N8N_PLATE_URL, msg)
                audio_bytes = await speak_once(n8n_reply)
                await ws.send_bytes(audio_bytes)
                continue

            # ===================
            # CALENDAR LOGIC
            # ===================
            if any(k in lower_msg for k in calendar_kw):
                phrase = random.choice(calendar_phrases)
                await ws.send_text(json.dumps({"type": "text", "content": phrase}))
                cal_reply = await send_to_n8n(N8N_CALENDAR_URL, msg)
                audio_bytes = await speak_once(cal_reply)
                await ws.send_bytes(audio_bytes)
                continue

            # ===================
            # GENERAL RESPONSE
            # ===================
            try:
                result = await openai_client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": msg},
                    ]
                )

                reply = result.choices[0].message.content
                audio_bytes = await speak_once(reply)
                await ws.send_bytes(audio_bytes)

                asyncio.create_task(mem0_add(user_id, msg))

            except Exception as e:
                log.error(f"LLM error: {e}")
                await ws.send_text(json.dumps({"type": "text", "content":"Sorry, I hit an issue."}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WS error: {e}")

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

