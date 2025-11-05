import os
import json
import asyncio
import httpx
import logging
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ==========================================
# 🔧 CONFIGURATION
# ==========================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
N8N_PLATE_URL = "https://n8n.marshall321.org/webhook/agent/plate"

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice_agent")

app = FastAPI()


# ==========================================
# 🧠 HELPERS
# ==========================================

async def send_to_plate(user_message: str) -> str:
    """Send user message to Notion Plate workflow and return reply."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"message": user_message}
            response = await client.post(N8N_PLATE_URL, json=payload)
            log.info(f"🍽️ Plate response: {response.text}")

            if response.status_code == 200:
                try:
                    data = response.json()
                    if isinstance(data, list):
                        data = data[0]
                    if isinstance(data, dict) and "reply" in data:
                        return data["reply"].strip()
                except Exception:
                    pass
                return response.text.strip()
            else:
                log.warning(f"⚠️ Plate returned {response.status_code}: {response.text}")
    except Exception as e:
        log.error(f"❌ Error sending to plate workflow: {e}")
    return "Sorry, I couldn’t reach your plate right now."


# ==========================================
# 🎯 MESSAGE ROUTING LOGIC
# ==========================================

def should_route_to_plate(user_message: str) -> bool:
    msg = user_message.lower()
    return "add to my plate" in msg or "what’s on my plate" in msg or "whats on my plate" in msg


# ==========================================
# 🌐 HTTP ENDPOINT
# ==========================================

class VoiceInput(BaseModel):
    message: str


@app.post("/api/agent")
async def handle_agent(request: Request):
    """Handle incoming messages from Retell or other front-end (HTTP)."""
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()

        if not user_message:
            return JSONResponse({
                "reply": "It looks like your message didn't come through. How can I assist you today?"
            })

        # Route message only if it's about "plate"
        if should_route_to_plate(user_message):
            reply = await send_to_plate(user_message)
        else:
            reply = "I'm here and ready — would you like to check your plate or your calendar?"

        return JSONResponse({"reply": reply})

    except Exception as e:
        log.error(f"❌ Error handling agent request: {e}")
        return JSONResponse({"reply": "There was an error processing your request."})


# ==========================================
# 🔌 WEBSOCKET ENDPOINT (Retell)
# ==========================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("🔌 WebSocket connected")

    try:
        while True:
            data = await websocket.receive_text()
            log.info(f"🗣️ WS received: {data}")

            try:
                payload = json.loads(data)
                user_message = payload.get("message", "").strip()
            except Exception:
                user_message = data.strip()

            if not user_message:
                await websocket.send_text("It looks like your message didn't come through.")
                continue

            if should_route_to_plate(user_message):
                reply = await send_to_plate(user_message)
            else:
                reply = "I'm here and ready — would you like to check your plate or your calendar?"

            await websocket.send_text(reply)
            log.info(f"📤 Sent WS reply: {reply}")

    except WebSocketDisconnect:
        log.info("🔌 WebSocket disconnected")
    except Exception as e:
        log.error(f"❌ WebSocket error: {e}")
        try:
            await websocket.send_text("There was an error processing your request.")
        except Exception:
            pass


# ==========================================
# 🏁 RUN SERVER
# ==========================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

