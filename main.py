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

# Environment variables
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
# MEM0 MEMORY FUNCTIONS
# ==============================================================

async def mem0_search_v2(query: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://api.mem0.ai/v2/search",
                headers={"Authorization": f"Bearer {MEMO_API_KEY}"},
                json={"query": query, "limit": 5},
            )
            res.raise_for_status()
            data = res.json()
            if "results" in data and len(data["results"]) > 0:
                return "\n".join([r["text"] for r in data["results"]])
            return ""
    except Exception as e:
        log.error(f"Mem0 Search Error: {e}")
        return ""

async def mem0_add_v1(text: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://api.mem0.ai/v1/add",
                headers={"Authorization": f"Bearer {MEMO_API_KEY}"},
                json={"text": text},
            )
            res.raise_for_status()
            return True
    except Exception as e:
        log.error(f"Mem0 Add Error: {e}")
        return False

async def build_memory_context(user_text: str):
    memory = await mem0_search_v2(user_text)
    if memory:
        return f"Previously, you noted: {memory}\n\nNow, continue the conversation."
    return "Continue conversation naturally."

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
# 🔹 ADD TO NOTION (Debug Mode)
# ==============================================================

async def add_to_notion(title: str, day: str | None):
    week = infer_week(day)
    props = {
        "To-Do": {"title": [{"text": {"content": title}}]},
        "Week": {"select": {"name": week}},
    }
    if day:
        props["Day"] = {"select": {"name": day}}

    for field, value in {"Plate": "Plate", "Type": "Task"}.items():
        for mode in ["status", "select"]:
            props[field] = {mode: {"name": value}}
            payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}

            log.info("🟡 SENDING TO NOTION:")
            log.info(json.dumps(payload, indent=2))

            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post("https://api.notion.com/v1/pages",
                                          headers=notion_headers(),
                                          json=payload)
                log.info(f"🟢 RESPONSE CODE: {r.status_code}")
                log.info(f"🟢 RESPONSE TEXT: {r.text}")

                if r.status_code in [200, 201]:
                    log.info(f"✅ Added {title} using {mode} for {field}")
                    await mem0_add_v1(f"Added task '{title}' for {day or 'unspecified day'}")
                    return f"Added “{title}” to your plate{f' for {day}' if day else ''}."
            except Exception as e:
                log.error(f"❌ Notion add error ({mode} {field}): {e}")
                continue

    return "Sorry, I couldn’t add that to your plate."

# ==============================================================
# 🔹 READ FROM NOTION (Debug Mode)
# ==============================================================

async def read_from_notion(day: str | None):
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    query = {"filter": {"property": "Day", "select": {"equals": day}}} if day else {}

    log.info("🟡 QUERYING NOTION:")
    log.info(json.dumps(query, indent=2))

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=notion_headers(), json=query)
            log.info(f"🟢 RESPONSE CODE: {r.status_code}")
            log.info(f"🟢 RESPONSE TEXT: {r.text}")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.error(f"❌ Notion read error: {e}")
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
        await mem0_add_v1(f"Read tasks for {day}: {listing}")
        return f"Here’s what’s on your plate for {day}: {listing}."
    else:
        grouped = {}
        for t, d in items:
            grouped.setdefault(d or "Unscheduled", []).append(t)
        parts = [f"{d}: " + ", ".join(arr) for d, arr in grouped.items()]
        await mem0_add_v1("Read full plate overview.")
        return " | ".join(parts)

# ==============================================================
# 🔹 CEREBRAS CHAT
# ==============================================================

async def cerebras_chat(prompt: str):
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}",
               "Content-Type": "application/json"}
    context = await build_memory_context(prompt)
    data = {"model": "llama3.1-8b",
            "messages": [
                {"role": "system", "content": "You are Solomon’s personal AI assistant."},
                {"role": "system", "content": context},
                {"role": "user", "content": prompt}],
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

