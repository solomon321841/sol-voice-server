import os, json, logging, asyncio, httpx, re, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import uvicorn

# ==============================================================
# CONFIGURATION
# ==============================================================

load_dotenv()
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "")
NOTION_TASKS_API_KEY = os.getenv("NOTION_TASKS_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
NOTION_VERSION = "2022-06-28"

# ==============================================================
# BASIC ENDPOINTS
# ==============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/")
async def index(request: Request):
    _ = await request.body()
    return JSONResponse({"ok": True})

# ==============================================================
# HELPERS
# ==============================================================

DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

def find_day(text: str):
    low = text.lower()
    for d in DAYS:
        if d in low:
            return d.capitalize()
    if "today" in low:
        return datetime.date.today().strftime("%A")
    if "tomorrow" in low:
        return (datetime.date.today() + datetime.timedelta(days=1)).strftime("%A")
    return None

def infer_week(day: str | None):
    if not day:
        return "This Week"
    today = datetime.date.today()
    target_idx = DAYS.index(day.lower())
    delta = (target_idx - today.weekday()) % 7
    target_date = today + datetime.timedelta(days=delta)
    return "This Week" if (target_date - today).days < 7 else "Next Week"

def extract_title(text: str):
    normalized = re.sub(r"(add\s+.+?)\s+to my plate\s+for\s+([a-zA-Z]+)",
                        r"\1 for \2 to my plate", text, flags=re.I)
    m = re.search(r"add\s+(.+?)\s+(?:for\s+[a-zA-Z]+|to my plate)", normalized, flags=re.I)
    if m: return m.group(1).strip()
    m2 = re.search(r"add\s+(.+)", normalized)
    return m2.group(1).strip() if m2 else text.strip()

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TASKS_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

# ==============================================================
# 🔹 ADD TO NOTION
# ==============================================================

async def add_to_notion(title: str, day: str | None):
    week = infer_week(day)
    props = {
        "To-Do": {"title": [{"text": {"content": title}}]},
        "Plate": {"status": {"name": "Plate"}},
        "Type": {"status": {"name": "Task"}},
        "Week": {"select": {"name": week}},
    }
    if day:
        props["Day"] = {"select": {"name": day}}

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.notion.com/v1/pages",
                                  headers=notion_headers(),
                                  json=payload)
            r.raise_for_status()
        log.info(f"✅ Added to Notion: {title}")
        return f"Added “{title}” to your plate{f' for {day}' if day else ''}."
    except Exception as e:
        log.error(f"❌ Notion add error: {e}")
        if 'r' in locals():
            log.error(f"RESPONSE CODE: {r.status_code}")
            log.error(f"RESPONSE TEXT: {r.text}")
        return "Sorry, I couldn’t add that to your plate."

# ==============================================================
# 🔹 READ FROM NOTION
# ==============================================================

async def read_from_notion(day: str | None):
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    query = {"filter": {"property": "Day", "select": {"equals": day}}} if day else {}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=notion_headers(), json=query)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.error(f"❌ Notion read error: {e}")
        if 'r' in locals():
            log.error(f"RESPONSE CODE: {r.status_code}")
            log.error(f"RESPONSE TEXT: {r.text}")
        return "Sorry, I couldn’t read your plate right now."

    items = []
    for page in data.get("results", []):
        props = page.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in props.get("To-Do", {}).get("title", []))
        day_val = props.get("Day", {}).get("select", {}).get("name", "")
        if title:
            items.append((title, day_val))

    if not items:
        return f"You have nothing on your plate{f' for {day}' if day else ''}."

    if day:
        listing = ", ".join(t for t, _ in items)
        return f"Here’s what’s on your plate for {day}: {listing}."
    else:
        grouped = {}
        for t, d in items:
            grouped.setdefault(d or "Unscheduled", []).append(t)
        parts = [f"{d}: " + ", ".join(arr) for d, arr in grouped.items()]
        return " | ".join(parts)

# ==============================================================
# 🔹 CEREBRAS CHAT
# ==============================================================

async def cerebras_chat(prompt: str):
    if not CEREBRAS_API_KEY:
        return "Cerebras key missing."
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}",
               "Content-Type": "application/json"}
    data = {"model": "llama3.1-8b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300, "temperature": 0.7}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.cerebras.ai/v1/chat/completions",
                                  headers=headers, json=data)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        if 'r' in locals(): log.error(f"RESPONSE TEXT: {r.text}")
        return "Sorry, I hit a small issue."

# ==============================================================
# 🔹 RETELL WEBSOCKET
# ==============================================================

active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(ws: WebSocket, call_id: str):
    if call_id in active_connections:
        await ws.close()
        return
    active_connections.add(call_id)
    await ws.accept()
    log.info(f"🔌 Connected: {call_id}")

    async def speak(id_: int, text: str, end_turn=True):
        payload = {"type": "response_message", "response_id": id_,
                   "content": text, "content_complete": True, "end_turn": end_turn}
        await ws.send_text(json.dumps(payload))
        log.info(f"🗣️ {text}")

    await speak(0, "Hey Solomon — I’m ready when you are.")

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            transcript = data.get("transcript", [])
            response_id = int(data.get("response_id", 1))

            user_text = ""
            if isinstance(transcript, list):
                for t in reversed(transcript):
                    if t.get("role") == "user":
                        user_text = t.get("content", "").strip()
                        break

            if not user_text:
                continue

            log.info(f"User said: {user_text}")

            low = user_text.lower()

            # Handle plate requests
            if "plate" in low:
                if "add" in low or "book" in low:
                    await speak(response_id, "Got it. Adding that now...", end_turn=False)
                    title = extract_title(user_text)
                    day = find_day(user_text)
                    asyncio.create_task(
                        speak(response_id, await add_to_notion(title or "New Task", day))
                    )
                    continue

                if "what" in low:
                    await speak(response_id, "Checking that now...", end_turn=False)
                    day_ = find_day(user_text)
                    asyncio.create_task(
                        speak(response_id, await read_from_notion(day_))
                    )
                    continue

            reply = await cerebras_chat(user_text)
            await speak(response_id, reply)

    except WebSocketDisconnect:
        log.info(f"Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"Closed: {call_id}")

# ==============================================================
# MAIN ENTRY
# ==============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

