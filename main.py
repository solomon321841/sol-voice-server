import os, json, logging, asyncio, time, random, re
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
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.mem0.ai/v2/memories/",
                headers={"Authorization": f"Token {MEMO_API_KEY}"},
                json={"filters": {"user_id": user_id}, "query": query},
            )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"🔥 Mem0 v2 error: {e}")
    return []

async def mem0_add_v1(user_id: str, text: str):
    if not MEMO_API_KEY or not text:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://api.mem0.ai/v1/memories/",
                headers={"Authorization": f"Token {MEMO_API_KEY}"},
                json={"user_id": user_id, "messages": [{"role": "user", "content": text}]},
            )
        log.info(f"✅ Memory added for {user_id}")
    except Exception as e:
        log.error(f"🔥 Error adding memory: {e}")

def build_memory_context(items: list) -> str:
    lines = [f"- {it.get('memory') or it.get('content') or it.get('text')}" for it in items if isinstance(it, dict)]
    return "Relevant memories:\n" + "\n".join(lines) if lines else ""

# =====================================================
# ⚙️ CEREBRAS CHAT
# =====================================================
CEREBRAS_MODEL = "llama3.1-8b"

async def cerebras_chat(messages: List[Dict[str, str]]) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.cerebras.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
                json={"model": CEREBRAS_MODEL, "messages": messages, "max_tokens": 300, "temperature": 0.7},
            )
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        return "Sorry, I hit a speed bump. Try again?"

# =====================================================
# 🧩 FETCH PROMPT FROM NOTION
# =====================================================
async def get_latest_prompt():
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            data = r.json()
            text_parts = [
                "".join([r.get("plain_text", "") for r in block["paragraph"].get("rich_text", [])])
                for block in data.get("results", [])
                if block.get("type") == "paragraph"
            ]
            return "\n".join(text_parts).strip() or "You are Solomon Roth’s personal AI assistant."
    except Exception as e:
        log.error(f"❌ Error fetching prompt: {e}")
        return "You are Solomon Roth’s personal AI assistant."

# =====================================================
# 🧩 N8N PLATE HANDLER (UPDATED)
# =====================================================
async def send_to_plate(user_message: str) -> str:
    try:
        normalized = re.sub(r"(add .*?) to my plate for (.+)", r"add \1 for \2 to my plate", user_message, flags=re.I)
        user_message = normalized

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(N8N_PLATE_URL, json={"message": user_message})

        raw_text = r.text
        data = None
        try:
            data = r.json()
        except Exception:
            pass

        log.info(f"🔁 N8N {r.status_code} RAW: {raw_text[:500]}")

        if not data:
            return "Sorry, I hit a small issue."

        # ✅ Extract reply text safely
        reply = None
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            reply = data[0].get("reply")
        elif isinstance(data, dict):
            reply = data.get("reply")

        if not reply:
            reply = str(data)

        # ✅ Clean markdown + line breaks
        reply = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", reply)  # strip markdown links
        reply = reply.replace("\\n", " ").replace("\n", " ").strip()

        return reply or "Task completed successfully."

    except Exception as e:
        log.error(f"❌ Plate workflow error: {e}")
        return "Sorry, I hit a small issue."

# =====================================================
# 🔌 RETELL CONNECTION
# =====================================================
active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    if call_id in active_connections:
        log.info(f"⚠️ Duplicate connection detected for {call_id}, closing old one.")
        await websocket.close()
        return
    active_connections.add(call_id)
    await websocket.accept()
    log.info(f"🔌 Connected: {call_id}")
    user_id = "solomon_roth"

    async def speak(response_id: int, text: str, end_turn=True):
        await websocket.send_text(json.dumps({
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": True,
            "end_turn": end_turn,
        }))
        log.info(f"🗣️ {text[:100]}")

    await speak(0, "Hey Solomon, I’m ready when you are.")

    quick_add = ["Got it.", "Okay, adding that now.", "On it.", "Sure thing.", "Done."]
    quick_check = ["Checking that now.", "One sec, let me check.", "Okay, here’s what I found."]
    quick_remove = ["Got it, removing that.", "Okay, it’s gone.", "Done, removed."]

    last_message = {"text": None, "time": 0}
    plate_keywords = ["plate", "task", "to-do", "notion", "list"]
    add_keywords = ["add", "put", "save", "book"]
    check_keywords = ["what", "show", "see", "check"]
    remove_keywords = ["remove", "delete", "clear"]

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            transcript = data.get("transcript", [])
            interaction = data.get("interaction_type")
            response_id = int(data.get("response_id", 1))
            user_message = ""
            for t in reversed(transcript or []):
                if t.get("role") == "user":
                    user_message = t.get("content", "").strip()
                    break

            if interaction == "response_required":
                now = time.time()
                if (
                    user_message
                    and last_message["text"]
                    and (user_message.lower().startswith(last_message["text"].lower())
                         or last_message["text"].lower().startswith(user_message.lower()))
                    and now - last_message["time"] < 3
                ):
                    log.info("🛑 Skipping duplicate/partial message.")
                    continue
                last_message = {"text": user_message, "time": now}

                lower = user_message.lower()

                # 🧠 Intent detection
                if any(k in lower for k in plate_keywords):
                    if any(k in lower for k in add_keywords):
                        await speak(response_id, random.choice(quick_add), end_turn=False)
                        reply = await send_to_plate(user_message)
                        await speak(response_id, reply)
                        continue
                    elif any(k in lower for k in check_keywords):
                        await speak(response_id, random.choice(quick_check), end_turn=False)
                        reply = await send_to_plate("check my plate")
                        await speak(response_id, reply)
                        continue
                    elif any(k in lower for k in remove_keywords):
                        await speak(response_id, random.choice(quick_remove), end_turn=False)
                        reply = await send_to_plate(user_message)
                        await speak(response_id, reply)
                        continue

                # Default conversation
                notion_prompt = await get_latest_prompt()
                mems = await mem0_search_v2(user_id, user_message)
                context = build_memory_context(mems)
                system_prompt = f"{notion_prompt}\n\nKnown facts:\n{context}\n"
                reply = await cerebras_chat([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message or "Hello?"},
                ])
                await speak(response_id, reply)
                asyncio.create_task(mem0_add_v1(user_id, user_message))

    except WebSocketDisconnect:
        log.info(f"❌ Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"🔕 Connection closed: {call_id}")

# =====================================================
# 🚀 SERVER STARTUP
# =====================================================
def start_ngrok(port: int = 8000):
    url = ngrok.connect(addr=port, proto="http").public_url.replace("http://", "https://")
    log.info(f"🌐 {url}")
    log.info(f"🔗 Retell URL: wss://{url.replace('https://', '')}/ws/{{call_id}}")
    return url

if __name__ == "__main__":
    start_ngrok(8000)
    log.info("🚀 Server running...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

