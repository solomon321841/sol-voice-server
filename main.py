import os, json, logging, asyncio, time, random, re, datetime
from typing import List, Dict, Optional
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# =================== Setup ===================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")
load_dotenv()

# ---- Notion (Account A: prompt) ----
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

# ---- Notion (Account B: My Plate) ----
NOTION_TASKS_API_KEY = os.getenv("NOTION_TASKS_API_KEY", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()

# ---- LLM / Memory ----
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = "llama3.1-8b"
MEMO_API_KEY = os.getenv("MEMO_API_KEY", "").strip()

# =================== FastAPI ===================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/")
async def sink(request: Request):
    _ = await request.body()
    return JSONResponse({"ok": True})

# =================== Helpers ===================
DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

def find_day_in_text(text: str) -> Optional[str]:
    low = text.lower()
    for d in DAYS:
        if d in low:
            return d.capitalize()
    if "today" in low:
        return datetime.date.today().strftime("%A")
    if "tomorrow" in low:
        return (datetime.date.today() + datetime.timedelta(days=1)).strftime("%A")
    return None

def infer_week(day_name: Optional[str]) -> str:
    today = datetime.date.today()
    if not day_name:
        return "This Week"
    target_idx = DAYS.index(day_name.lower())
    today_idx = today.weekday()
    delta = (target_idx - today_idx) % 7
    target_date = today + datetime.timedelta(days=delta)
    return "This Week" if (target_date - today).days <= 6 else "Next Week"

def extract_title(text: str) -> str:
    low = text.lower().strip()
    normalized = re.sub(r"(add\s+.+?)\s+to my plate\s+for\s+([a-zA-Z]+)", r"\1 for \2 to my plate", low, flags=re.I)
    m = re.search(r"add\s+(.+?)\s+(?:for\s+[a-zA-Z]+|to my plate)", normalized, flags=re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"add\s+(.+)", normalized)
    if m2:
        return m2.group(1).strip()
    return text.strip()

# =================== Notion (Prompt) ===================
NOTION_VERSION = "2022-06-28"

async def get_prompt_from_notion() -> str:
    if not (NOTION_API_KEY and NOTION_PAGE_ID):
        return "You are Solomon’s personal AI assistant."
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
        parts = []
        for block in data.get("results", []):
            if block.get("type") == "paragraph":
                parts.append("".join([rt.get("plain_text","") for rt in block["paragraph"].get("rich_text", [])]))
        return "\n".join(parts).strip() or "You are Solomon’s personal AI assistant."
    except Exception as e:
        log.warning(f"Prompt fetch error: {e}")
        return "You are Solomon’s personal AI assistant."

# =================== Notion (Tasks) ===================
def notion_tasks_headers():
    return {
        "Authorization": f"Bearer {NOTION_TASKS_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

async def notion_add_task(title: str, day: Optional[str]) -> str:
    if not (NOTION_TASKS_API_KEY and NOTION_DATABASE_ID):
        return "Notion tasks credentials are missing."

    week = infer_week(day)
    props = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Plate": {"status": {"name": "Plate"}},
        "Type": {"status": {"name": "Review"}},
        "Week": {"select": {"name": week}},
    }
    if day:
        props["Day"] = {"select": {"name": day}}

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    url = "https://api.notion.com/v1/pages"
    headers = notion_tasks_headers()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        return f"Added “{title}” to your plate{f' for {day}' if day else ''}."
    except Exception as e:
        log.error(f"Notion add error: {e}")
        err = ""
        try:
            if 'r' in locals() and r is not None:
                err = r.text
        except Exception:
            pass
        return "Sorry, I couldn’t add that to your plate."

async def notion_read_plate(day: Optional[str]) -> str:
    if not (NOTION_TASKS_API_KEY and NOTION_DATABASE_ID):
        return "Notion tasks credentials are missing."

    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = notion_tasks_headers()
    filter_obj = {"filter": {"property": "Day", "select": {"equals": day}}} if day else {}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=filter_obj)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.error(f"Notion read error: {e}")
        return "Sorry, I couldn’t read your plate right now."

    items = []
    for page in data.get("results", []):
        props = page.get("properties", {})
        title = "".join(t.get("plain_text","") for t in props.get("To-Do", {}).get("title", []))
        day_val = props.get("Day", {}).get("select", {}).get("name", "")
        if title:
            items.append((title, day_val))

    if not items:
        return f"You have nothing on your plate{f' for {day}' if day else ''}."

    if day:
        listing = ", ".join(t for t,_ in items)
        return f"Here’s what’s on your plate for {day}: {listing}."
    else:
        by_day: Dict[str, List[str]] = {}
        for t, d in items:
            by_day.setdefault(d or "Unscheduled", []).append(t)
        parts = [f"{d}: " + ", ".join(arr) for d, arr in by_day.items()]
        return "Here’s your plate: " + " | ".join(parts)

# =================== LLM ===================
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
        return "Sorry, I hit a speed bump."

# =================== WebSocket (Retell) ===================
active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    if call_id in active_connections:
        await websocket.close()
        return
    active_connections.add(call_id)
    await websocket.accept()
    log.info(f"🔌 Connected: {call_id}")
    user_id = "solomon_roth"

    async def speak(response_id: int, text: str, end_turn=True):
        payload = {"type": "response_message", "response_id": response_id,
                   "content": text, "content_complete": True, "end_turn": end_turn}
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ {text[:100]}")

    await speak(0, "Hey Solomon — I’m ready when you are.")

    QUICK_ADD = ["Got it.", "Okay, adding that now.", "On it."]
    QUICK_READ = ["Checking that now.", "One sec, let me check."]
    plate_words = ["plate","task","to-do","todo"]
    add_words = ["add","put","save","book"]
    read_words = ["what","show","see","check","read"]

    last_msg = {"text": "", "time": 0.0}

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            transcript = data.get("transcript", [])
            interaction_type = data.get("interaction_type")
            response_id = int(data.get("response_id", 1))

            user_text = ""
            if isinstance(transcript, list):
                for t in reversed(transcript):
                    if t.get("role") == "user":
                        user_text = t.get("content", "").strip()
                        break
            if not user_text:
                continue

            now = time.time()
            if user_text.lower() == last_msg["text"].lower() and (now - last_msg["time"]) < 3:
                continue
            last_msg = {"text": user_text, "time": now}

            if interaction_type != "response_required":
                continue

            low = user_text.lower()

            # Plate handling
            if any(w in low for w in plate_words):
                normalized = re.sub(r"(add\s+.+?)\s+to my plate\s+for\s+([a-zA-Z]+)", r"\1 for \2 to my plate", user_text, flags=re.I)
                day = find_day_in_text(normalized)
                is_add = any(w in low for w in add_words)
                is_read = any(w in low for w in read_words) or "what's on my plate" in low

                if is_add:
                    title = extract_title(normalized)
                    await speak(response_id, random.choice(QUICK_ADD), end_turn=False)
                    final = await notion_add_task(title or "New Task", day)
                    await speak(response_id, final)
                    continue

                if is_read:
                    await speak(response_id, random.choice(QUICK_READ), end_turn=False)
                    final = await notion_read_plate(day)
                    await speak(response_id, final)
                    continue

            # General chat
            prompt = await get_prompt_from_notion()
            reply = await cerebras_chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text}
            ])
            await speak(response_id, reply)

    except WebSocketDisconnect:
        log.info(f"Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"Closed: {call_id}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

