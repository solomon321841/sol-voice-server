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
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "29b20888d7678028ad4fc54ee3f18539").strip()

# New webhook URL for n8n
N8N_WEBHOOK_URL = "https://n8n.marshall321.org/webhook/retell/create_web_call"

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
# 🧠 MEM0 + CEREBRAS (same as before)
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
# 🧩 FETCH PROMPT LIVE FROM NOTION
# =====================================================
async def get_latest_prompt():
    """Always fetch latest system prompt from Notion page before each chat."""
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
            prompt_text = "\n".join(text_parts).strip()
            return prompt_text or "You are Solomon Roth’s personal AI assistant."
    except Exception as e:
        log.error(f"❌ Error fetching prompt from Notion: {e}")
        return "You are Solomon Roth’s personal AI assistant."

# =====================================================
# 🔌 RETELL CONNECTION
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
            data = json.loads(raw)
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
                # 🧠 Send query to n8n for calendar check / logic
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.post(N8N_WEBHOOK_URL, json={"query": user_message})
                        n8n_data = resp.json()
                        log.info(f"📩 n8n responded: {n8n_data}")
                        if isinstance(n8n_data, dict) and "calendar_summary" in n8n_data:
                            reply = n8n_data["calendar_summary"]
                        else:
                            reply = "I couldn’t access your calendar right now."
                except Exception as e:
                    log.error(f"❌ n8n request failed: {e}")
                    reply = "I had trouble contacting your scheduling system."

                # 🗣️ Only one voice reply — no double response
                await send_speech(response_id, reply)

    except WebSocketDisconnect:
        log.info(f"❌ Retell WebSocket disconnected: {call_id}")
    except Exception as e:
        log.error(f"WebSocket error: {e}")

# =====================================================
# 🧩 ADMIN PANEL — Notion prompt editor
# =====================================================
@app.get("/get_prompt_live")
async def get_prompt_live():
    prompt = await get_latest_prompt()
    return {"prompt_text": prompt}

@app.post("/update_prompt_live")
async def update_prompt_live(request: Request):
    try:
        body = await request.json()
        new_text = body.get("prompt_text", "").strip()
        if not new_text:
            return {"success": False, "error": "Empty prompt_text"}

        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }

        url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers)
            res.raise_for_status()
            data = res.json()
            for block in data.get("results", []):
                block_id = block.get("id")
                if block_id:
                    await client.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=headers)

            payload = {
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": new_text}}]
                    }
                }]
            }
            await client.patch(url, headers=headers, json=payload)

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
    log.info(f"🔗 Retell Custom LLM URL: wss://{url.replace('https://', '')}/ws/{{call_id}}")
    return url

if __name__ == "__main__":
    start_ngrok(8000)
    log.info("🚀 Running FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

