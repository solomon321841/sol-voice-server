import os, json, logging, asyncio, httpx, re, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import uvicorn

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

@app.get("/health")
async def health():
    return {"status": "ok"}

# ==============================================================
# MEM0 MEMORY
# ==============================================================

async def mem0_add_v1(text: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://api.mem0.ai/v1/add",
                headers={"Authorization": f"Bearer {MEMO_API_KEY}"},
                json={"text": text},
            )
            res.raise_for_status()
    except Exception as e:
        log.error(f"Mem0 Add Error: {e}")

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
            if "results" in data:
                return "\n".join([r["text"] for r in data["results"]])
    except Exception as e:
        log.error(f"Mem0 Search Error: {e}")
    return ""

async def build_memory_context(user_text: str):
    memory = await mem0_search_v2(user_text)
    return f"Previously you said: {memory}" if memory else "Continue naturally."

# ==============================================================
# HELPERS
# ==============================================================

DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

def find_day(text):
    low = text.lower()
    for d in DAYS:
        if d in low: return d.capitalize()
    if "today" in low:
        return datetime.date.today().strftime("%A")
    if "tomorrow" in low:
        return (datetime.date.today() + datetime.timedelta(days=1)).strftime("%A")
    return None

def extract_title(text):
    m = re.search(r"add\s+(.+?)\s+(?:to my plate|for\s+\w+)", text, re.I)
    return m.group(1).strip() if m else text.strip()

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TASKS_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

# ==============================================================
# NOTION HANDLERS
# ==============================================================

async def add_to_notion(title, day):
    props = {
        "To-Do": {"title": [{"text": {"content": title}}]},
        "Plate": {"status": {"name": "Plate"}},
    }
    if day:
        props["Day"] = {"select": {"name": day}}
        props["Week"] = {"select": {"name": "This Week"}}
    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    log.info(json.dumps(payload, indent=2))

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.notion.com/v1/pages", headers=notion_headers(), json=payload)
        log.info(f"Notion response: {r.status_code} - {r.text}")
        if r.status_code in [200, 201]:
            await mem0_add_v1(f"Added {title} for {day or 'unspecified'}")
            return f"Added {title} to your plate{f' for {day}' if day else ''}."
        else:
            return f"Failed to add to Notion ({r.status_code})."
    except Exception as e:
        log.error(f"Add error: {e}")
        return "Sorry, I hit a small issue."

async def read_from_notion(day):
    query = {"filter": {"property": "Day", "select": {"equals": day}}} if day else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query", headers=notion_headers(), json=query)
        r.raise_for_status()
        data = r.json()
        items = [p["properties"]["To-Do"]["title"][0]["plain_text"] for p in data["results"] if "To-Do" in p["properties"]]
        if not items:
            return f"You have nothing on your plate for {day}."
        return f"On your plate for {day}: {', '.join(items)}."
    except Exception as e:
        log.error(f"Read error: {e}")
        return "Sorry, couldn’t read your plate."

# ==============================================================
# CEREBRAS CHAT
# ==============================================================

async def cerebras_chat(prompt):
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    context = await build_memory_context(prompt)
    data = {
        "model": "llama3.1-8b",
        "messages": [
            {"role": "system", "content": "You are Solomon’s personal AI assistant."},
            {"role": "system", "content": context},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 250,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=data)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM Error: {e}")
        return "Sorry, I hit a small issue."

# ==============================================================
# RETELL SOCKET (with new user_text fix)
# ==============================================================

active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(ws: WebSocket, call_id: str):
    if call_id in active_connections:
        await ws.close()
        return
    active_connections.add(call_id)
    await ws.accept()
    log.info(f"Connected: {call_id}")

    async def speak(id_, text, end=True):
        payload = {"type": "response_message", "response_id": id_, "content": text, "content_complete": True, "end_turn": end}
        await ws.send_text(json.dumps(payload))
        log.info(f"🗣️ {text}")

    await speak(0, "Hey Solomon, I’m ready when you are.")

    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            transcript = data.get("transcript", [])
            response_id = int(data.get("response_id", 1))

            # ✅ FIX: always capture actual speech text
            user_text = (
                data.get("text")
                or data.get("message", {}).get("content")
                or next(
                    (t.get("content") for t in reversed(transcript) if t.get("role") == "user"),
                    ""
                )
            ).strip()

            if not user_text:
                continue

            log.info(f"User said: {user_text}")
            low = user_text.lower()

            if "plate" in low:
                if "add" in low or "book" in low:
                    await speak(response_id, "Got it. Adding that now...", end=False)
                    title, day = extract_title(user_text), find_day(user_text)
                    asyncio.create_task(speak(response_id, await add_to_notion(title or "New Task", day)))
                    continue
                if "what" in low:
                    await speak(response_id, "Checking that now...", end=False)
                    asyncio.create_task(speak(response_id, await read_from_notion(find_day(user_text))))
                    continue

            await speak(response_id, await cerebras_chat(user_text))

    except WebSocketDisconnect:
        log.info(f"Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

