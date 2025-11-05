import os
import json
import asyncio
import httpx
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ==========================================
# CONFIGURATION
# ==========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
N8N_CALENDAR_URL = os.getenv("N8N_CALENDAR_URL", "https://n8n.marshall321.org/webhook/agent/calendar")
N8N_PLATE_URL = os.getenv("N8N_PLATE_URL", "https://n8n.marshall321.org/webhook/agent/plate")

# Initialize logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice_agent")

app = FastAPI()


# ==========================================
# HELPERS
# ==========================================
async def send_to_calendar(user_message: str) -> str:
    """Send user message to the calendar workflow and return a clean reply."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"message": user_message}
            response = await client.post(N8N_CALENDAR_URL, json=payload)
            log.info(f"📅 Calendar response: {response.text}")

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

            log.warning(f"⚠️ Calendar returned {response.status_code}: {response.text}")
    except Exception as e:
        log.error(f"❌ Error sending to calendar workflow: {e}")
    return "Sorry, I couldn’t reach your calendar right now."


async def send_to_plate(user_message: str) -> str:
    """Send user message to Notion Plate workflow and return clean reply."""
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

            log.warning(f"⚠️ Plate returned {response.status_code}: {response.text}")
    except Exception as e:
        log.error(f"❌ Error sending to plate workflow: {e}")
    return "Sorry, I couldn’t reach your plate right now."


# ==========================================
# ROUTING LOGIC
# ==========================================
calendar_keywords = [
    "schedule", "meeting", "calendar", "cancel",
    "event", "appointment", "reschedule"
]

plate_keywords = [
    "add", "plate", "task", "put", "create", "to-do", "todo", "note", "plan", "list"
]


def route_message(user_message: str) -> str:
    """Route message to either calendar or plate workflow."""
    msg = user_message.lower()
    if any(kw in msg for kw in calendar_keywords):
        return "calendar"
    if any(kw in msg for kw in plate_keywords):
        return "plate"
    return "general"


# ==========================================
# FASTAPI ENDPOINT
# ==========================================
class VoiceInput(BaseModel):
    message: str


@app.post("/api/agent")
async def handle_agent(request: Request):
    """Handle incoming message from Retell or other front-end."""
    try:
        data = await request.json()
        user_message = data.get("message", "").strip()

        if not user_message:
            return JSONResponse({
                "reply": "It looks like your message didn't come through. How can I assist you today?"
            })

        destination = route_message(user_message)
        log.info(f"🧭 Routed to: {destination}")

        if destination == "calendar":
            reply = await send_to_calendar(user_message)
        elif destination == "plate":
            reply = await send_to_plate(user_message)
        else:
            reply = "I'm here and ready — would you like to check your plate or your calendar?"

        return JSONResponse({"reply": reply})

    except Exception as e:
        log.error(f"❌ Error handling agent request: {e}")
        return JSONResponse({"reply": "There was an error processing your request."})


# ==========================================
# START SERVER
# ==========================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

