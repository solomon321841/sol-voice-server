import os
import json
import logging
import asyncio
import time
import random
import string
import struct
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
# 🧠 MEM0 MEMORY (unchanged)
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
                    parts.append("".join([t.get("plain_text", "") for t in blk["paragraph"]["rich_text"]]))
            return "\n".join(parts).strip() or "You are Solomon Roth’s AI assistant, Silas."
    except Exception as e:
        log.error(f"❌ Notion error: {e}")
        return "You are Solomon Roth’s AI assistant, Silas."

# =====================================================
# 🔹 /prompt ENDPOINT
# =====================================================
@app.get("/prompt", response_class=PlainTextResponse)
async def get_prompt_text():
    txt = await get_notion_prompt()
    return PlainTextResponse(txt, headers={"Access-Control-Allow-Origin": "*"})

# =====================================================
# 🧩 n8n HELPERS (unchanged)
# =====================================================
async def send_to_n8n(url: str, message: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json={"message": message})
            log.info(f"📩 n8n raw response: {r.text}")

            if r.status_code == 200:
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        return (
                            data.get("reply") or
                            data.get("message") or
                            data.get("text") or
                            data.get("output") or
                            json.dumps(data, indent=2)
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
# 🎤 STREAMING AUDIO HANDLER (ChatGPT-style)
# =====================================================
def pcm_float_to_wav(pcm_data: List[float], sample_rate=16000):
    pcm16 = b''.join(struct.pack('<h', int(max(-1, min(1, s)) * 32767)) for s in pcm_data)

    wav_header = (
        b'RIFF' +
        struct.pack('<I', 36 + len(pcm16)) +
        b'WAVEfmt ' +
        struct.pack('<I', 16) +
        struct.pack('<H', 1) +
        struct.pack('<H', 1) +
        struct.pack('<I', sample_rate) +
        struct.pack('<I', sample_rate * 2) +
        struct.pack('<H', 2) +
        struct.pack('<H', 16) +
        b'data' +
        struct.pack('<I', len(pcm16))
    )
    return wav_header + pcm16

@app.websocket("/ws")
async def websocket_handler(ws: WebSocket):
    await ws.accept()

    user_id = "solomon_roth"
    audio_buffer = []
    recording = True

    # ===== Greeting (unchanged) =====
    prompt = await get_notion_prompt()
    greet = prompt.splitlines()[0] if prompt else "Hello Solomon, I'm Silas."
    try:
        tts_greet = await openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=greet
        )
        await ws.send_bytes(await tts_greet.aread())
    except:
        pass

    try:
        while True:
            data = await ws.receive()

            # STOP SIGNAL RECEIVED
            if data["type"] == "websocket.receive" and "text" in data and data["text"]:
                msg = json.loads(data["text"])
                if msg.get("event") == "stop":
                    recording = False
                    break

            # AUDIO CHUNK RECEIVED
            if "bytes" in data and data["bytes"]:
                try:
                    float_arr = struct.unpack('<' + 'f'*(len(data["bytes"])//4), data["bytes"])
                    audio_buffer.extend(float_arr)
                except:
                    pass

    except WebSocketDisconnect:
        pass

    # ===================================================
    # 🧠 TRANSCRIBE FULL BUFFER
    # ===================================================
    wav_bytes = pcm_float_to_wav(audio_buffer)

    text = ""
    try:
        stt = await openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.wav", wav_bytes, "audio/wav")
        )
        text = getattr(stt, "text", "")
    except Exception as e:
        log.error(f"❌ STT error: {e}")
        return

    if not text.strip():
        return

    lower = text.lower()

    # ===================================================
    # PLATE LOGIC (unchanged)
    # ===================================================
    if "plate" in lower or "task" in lower or "add" in lower or "list" in lower:
        reply = await send_to_n8n(N8N_PLATE_URL, text)
    # ===================================================
    # CALENDAR LOGIC (unchanged)
    # ===================================================
    elif "calendar" in lower or "schedule" in lower or "appointment" in lower:
        reply = await send_to_n8n(N8N_CALENDAR_URL, text)
    # ===================================================
    # NORMAL CHAT
    # ===================================================
    else:
        mems = await mem0_search(user_id, text)
        ctx = memory_context(mems)
        sys_prompt = f"{prompt}\n\nFacts:\n{ctx}"

        reply = ""
        stream = await openai_client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            stream=True,
        )

        async for chunk in stream:
            delta = getattr(chunk.choices[0].delta, "content", "")
            if delta:
                reply += delta

                # STREAM OUT TTS IN REALTIME
                tts = await openai_client.audio.speech.create(
                    model="gpt-4o-mini-tts",
                    voice="alloy",
                    input=delta
                )
                await ws.send_bytes(await tts.aread())

    asyncio.create_task(mem0_add(user_id, text))

# =====================================================
# 🚀 RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

