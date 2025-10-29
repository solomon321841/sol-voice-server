import os
import json
import logging
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
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

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
# 🧠 MEM0 FUNCTIONS (unchanged)
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
                log.warning("Mem0 v2 returned non-list; ignoring.")
        else:
            log.warning(f"⚠️ Mem0 v2 search failed ({r.status_code}): {r.text}")
    except Exception as e:
        log.error(f"🔥 Mem0 v2 search error: {e}")
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
        log.error(f"🔥 Error adding memory to Mem0: {e}")

def build_memory_context(items: list) -> str:
    if not items:
        return ""
    lines = []
    for it in items:
        if isinstance(it, dict):
            content = it.get("memory") or it.get("content") or it.get("text")
            if content:
                lines.append(f"- {content}")
    if not lines:
        return ""
    return "Relevant memories (use only if helpful):\n" + "\n".join(lines)

# =====================================================
# ⚙️ CEREBRAS CHAT + RETELL CONNECTION (unchanged)
# =====================================================
CEREBRAS_MODEL = "llama3.1-8b"

async def cerebras_chat(messages: List[Dict[str, str]]) -> str:
    if not CEREBRAS_API_KEY:
        return "Cerebras key missing."
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    data = {"model": CEREBRAS_MODEL, "messages": messages, "max_tokens": 300, "temperature": 0.7}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=data)
            r.raise_for_status()
            res = r.json()
            return res["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        return "Sorry, I hit a speed bump. Try again?"

# =====================================================
# 🔌 RETELL CONNECTION (unchanged)
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
        log.info(f"🗣️ Sent speech response: {text[:100]}")

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
                mem_items = await mem0_search_v2(user_id, user_message or "")
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
                reply = await cerebras_chat(messages)
                await send_speech(response_id, reply)
                if user_message:
                    asyncio.create_task(mem0_add_v1(user_id, user_message))
    except WebSocketDisconnect:
        log.info(f"❌ Retell WebSocket disconnected: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# =====================================================
# 🧩 ADMIN PANEL — GET + UPDATE PROMPT FROM NOTION
# =====================================================
async def fetch_prompt_from_notion():
    """Retrieve plain text from the Notion page."""
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers)
            res.raise_for_status()
            data = res.json()
            text_parts = []
            for block in data.get("results", []):
                if block.get("type") == "paragraph":
                    text = "".join(
                        [r.get("plain_text", "") for r in block["paragraph"].get("rich_text", [])]
                    )
                    text_parts.append(text)
            return "\n".join(text_parts).strip()
    except Exception as e:
        log.error(f"❌ Error fetching prompt from Notion: {e}")
        return "Error fetching prompt from Notion."

@app.get("/get_prompt_live")
async def get_prompt_live():
    prompt = await fetch_prompt_from_notion()
    return {"prompt_text": prompt}

@app.post("/update_prompt_live")
async def update_prompt_live(request: Request):
    """Overwrite Notion page text with new prompt."""
    try:
        body = await request.json()
        new_text = body.get("prompt_text", "").strip()
        if not new_text:
            return {"success": False, "error": "Empty prompt_text"}

        # Delete existing children (clear page)
        delete_url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

        # Replace with new paragraph
        payload = {
            "children": [
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"type": "text", "text": {"content": new_text}}]}}
            ]
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.patch(delete_url, headers=headers, json=payload)
            res.raise_for_status()
        return {"success": True}
    except Exception as e:
        log.error(f"❌ Error updating prompt in Notion: {e}")
        return {"success": False, "error": str(e)}

# =====================================================
# 🚀 SERVER STARTUP
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

