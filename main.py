import os, json, logging, asyncio, time, random, re, datetime
from typing import List, Dict, Optional
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# =================== Bootstrap ===================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("main")
load_dotenv()

# ---- Keys (Prompt / Account A) ----
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PAGE_ID = os.getenv("NOTION_PAGE_ID", "").strip()

# ---- Keys (Tasks / Account B) ----
NOTION_TASKS_API_KEY = os.getenv("NOTION_TASKS_API_KEY", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()

# ---- LLM (unchanged) ----
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = "llama3.1-8b"

# ---- Optional Mem0 (unchanged stubs) ----
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
    try:
        _ = await request.body()
    except Exception:
        pass
    return JSONResponse({"ok": True})

# =================== Helpers: Time / Parsing ===================
DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

def find_day_in_text(text: str) -> Optional[str]:
    low = text.lower()
    for d in DAYS:
        if d in low:
            return d.capitalize()
    # fallback phrases
    if "today" in low:
        return datetime.date.today().strftime("%A")
    if "tomorrow" in low:
        return (datetime.date.today() + datetime.timedelta(days=1)).strftime("%A")
    return None

def infer_week(day_name: Optional[str]) -> str:
    """
    Returns "This Week" or "Next Week" based on the next occurrence of the spoken day.
    If day is in the past for the current week, choose Next Week; else This Week.
    """
    today = datetime.date.today()
    if not day_name:
        return "This Week"
    target_idx = DAYS.index(day_name.lower())
    today_idx = today.weekday()  # Monday=0
    delta = (target_idx - today_idx) % 7
    target_date = today + datetime.timedelta(days=delta)
    # If delta==0 but it's earlier today, still treat as This Week.
    # We’ll say Next Week only if target_date < today (shouldn't happen with modulo).
    return "This Week" if (target_date - today).days <= 6 else "Next Week"

def extract_title(text: str) -> str:
    """
    Heuristic extraction. Works for:
      - "add book a flight to my plate for Friday"
      - "add book a flight for Friday to my plate"
      - "add lawn care to my plate"
    """
    low = text.lower().strip()
    # Normalize phrasing: "... to my plate for X" -> "... for X to my plate"
    normalized = re.sub(r"(add\s+.+?)\s+to my plate\s+for\s+([a-zA-Z]+)", r"\1 for \2 to my plate", low, flags=re.I)
    # Pull content between "add" and "for|to my plate"
    m = re.search(r"add\s+(.+?)\s+(?:for\s+[a-zA-Z]+|to my plate)", normalized, flags=re.I)
    if m:
        return m.group(1).strip().strip('"').strip("'")
    # Fallback: after "add "
    m2 = re.search(r"add\s+(.+)", normalized)
    if m2:
        return m2.group(1).strip()
    return text.strip()

# =================== Notion: Prompt (Account A) ===================
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
        prompt = "\n".join(parts).strip()
        return prompt or "You are Solomon’s personal AI assistant."
    except Exception as e:
        log.warning(f"Prompt fetch error: {e}")
        return "You are Solomon’s personal AI assistant."

# =================== Notion: Tasks (Account B) ===================
def notion_tasks_headers():
    return {
        "Authorization": f"Bearer {NOTION_TASKS_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

async def notion_add_task(title: str, day: Optional[str]) -> str:
    """
    Adds a task with:
      Type = "Review" (select or multi_select tolerant)
      Plate = "Plate"
      Day = spoken day (if provided)
      Week = inferred from day ("This Week" / "Next Week")
    """
    if not (NOTION_TASKS_API_KEY and NOTION_DATABASE_ID):
        return "Notion tasks credentials are missing."

    week = infer_week(day)
    # Build properties robustly (works if Type/Plate are select OR multi_select)
    props: Dict[str, dict] = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Plate": {"select": {"name": "Plate"}},
        "Week": {"select": {"name": week}},
    }
    if day:
        props["Day"] = {"select": {"name": day}}

    # Prefer 'select' for Type; if DB is multi_select it still works (Notion will coerce error -> we try alt)
    props["Type"] = {"select": {"name": "Review"}}

    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props}
    url = "https://api.notion.com/v1/pages"
    headers = notion_tasks_headers()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code == 400:
                # Retry with multi_select if schema requires it
                props["Type"] = {"multi_select": [{"name": "Review"}]}
                payload["properties"] = props
                r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        return f"Added “{title}” to your plate{f' for {day}' if day else ''}."
    except Exception as e:
        log.error(f"Notion add error: {e}")
        try:
            err = r.text  # type: ignore
        except Exception:
            err = ""
        return f"Sorry, I couldn’t add that. {err[:140]}"

async def notion_read_plate(day: Optional[str]) -> str:
    """
    Reads tasks for given day, or the whole plate if day is None.
    """
    if not (NOTION_TASKS_API_KEY and NOTION_DATABASE_ID):
        return "Notion tasks credentials are missing."

    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = notion_tasks_headers()

    filter_obj: Dict = {}
    if day:
        filter_obj = {
            "filter": {
                "property": "Day",
                "select": {"equals": day}
            }
        }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers, json=filter_obj or {})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.error(f"Notion read error: {e}")
        return "Sorry, I couldn’t read your plate right now."

    items = []
    for page in data.get("results", []):
        props = page.get("properties", {})
        # Title extraction
        name_parts = []
        title_prop = props.get("Name", {})
        if "title" in title_prop:
            for t in title_prop["title"]:
                name_parts.append(t.get("plain_text",""))
        title = "".join(name_parts).strip() or "Untitled"

        # Optional day stamp
        day_val = ""
        if "Day" in props and props["Day"].get("select"):
            day_val = props["Day"]["select"].get("name","")

        items.append((title, day_val))

    if not items:
        return f"You have nothing on your plate{f' for {day}' if day else ''}."

    if day:
        listing = ", ".join([t for t,_ in items])
        return f"Here’s what’s on your plate for {day}: {listing}."
    else:
        # group by Day for nice readout
        by_day: Dict[str, List[str]] = {}
        for t, d in items:
            by_day.setdefault(d or "Unscheduled", []).append(t)
        parts = []
        for d, arr in by_day.items():
            parts.append(f"{d}: " + ", ".join(arr))
        return "Here’s your plate: " + " | ".join(parts)

# =================== LLM (unchanged) ===================
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

# =================== Mem0 (optional stubs) ===================
async def mem0_search_v2(user_id: str, query: str):
    return []

async def mem0_add_v1(user_id: str, text: str):
    return

def build_memory_context(items: list) -> str:
    return ""

# =================== WebSocket (Retell) ===================
active_connections = set()

@app.websocket("/ws/{call_id}")
async def websocket_endpoint(websocket: WebSocket, call_id: str):
    # single-connection guard
    if call_id in active_connections:
        await websocket.close()
        return
    active_connections.add(call_id)
    await websocket.accept()
    log.info(f"🔌 Connected: {call_id}")
    user_id = "solomon_roth"

    async def speak(response_id: int, text: str, end_turn: bool = True):
        payload = {
            "type": "response_message",
            "response_id": response_id,
            "content": text,
            "content_complete": True,
            "end_turn": end_turn,
        }
        await websocket.send_text(json.dumps(payload))
        log.info(f"🗣️ {text[:120]}")

    # Greeting
    await speak(0, "Hey Solomon — I’m ready when you are.")

    # Quick phrases
    QUICK_ADD = ["Got it.", "Okay, adding that now.", "On it.", "Sure thing."]
    QUICK_READ = ["Checking that now.", "One sec, let me check.", "Okay, getting that."]
    # Simple routing keywords
    plate_words = ["plate", "task", "to-do", "todo", "notion", "list"]
    add_words   = ["add", "put", "save", "book"]
    read_words  = ["what", "show", "see", "check", "read"]

    last_msg = {"text": "", "time": 0.0}

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

            user_text = ""
            if transcript and isinstance(transcript, list):
                for t in reversed(transcript):
                    if t.get("role") == "user":
                        user_text = t.get("content", "").strip()
                        break
            if not user_text:
                continue

            # Dedup within 3 seconds
            now = time.time()
            if user_text.lower() == last_msg["text"].lower() and (now - last_msg["time"]) < 3:
                log.info("Skipping duplicate utterance.")
                continue
            last_msg = {"text": user_text, "time": now}

            if interaction_type != "response_required":
                continue

            low = user_text.lower()

            # ---------- Routing: Plate (Add vs Read) ----------
            if any(w in low for w in plate_words):
                # Normalize phrasing so both “to my plate for X” and “for X to my plate” work
                normalized = re.sub(r"(add\s+.+?)\s+to my plate\s+for\s+([a-zA-Z]+)", r"\1 for \2 to my plate", user_text, flags=re.I)
                day = find_day_in_text(normalized)
                is_add = any(w in low for w in add_words)
                is_read = any(w in low for w in read_words) or ("what's on my plate" in low or "whats on my plate" in low)

                if is_add:
                    title = extract_title(normalized)
                    await speak(response_id, random.choice(QUICK_ADD), end_turn=False)
                    final = await notion_add_task(title=title or "New Task", day=day)
                    await speak(response_id, final)
                    continue

                # default to read when asking "what/show/see/check"
                if is_read or "what's on my plate" in low or "whats on my plate" in low:
                    await speak(response_id, random.choice(QUICK_READ), end_turn=False)
                    final = await notion_read_plate(day=day)
                    await speak(response_id, final)
                    continue

            # ---------- Fallback to general chat ----------
            prompt = await get_prompt_from_notion()
            mems = await mem0_search_v2(user_id, user_text)
            context = build_memory_context(mems)
            system_prompt = f"{prompt}\n\nKnown facts:\n{context}\n"
            reply = await cerebras_chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ])
            await speak(response_id, reply)
            asyncio.create_task(mem0_add_v1(user_id, user_text))

    except WebSocketDisconnect:
        log.info(f"Disconnected: {call_id}")
    finally:
        active_connections.discard(call_id)
        log.info(f"Closed: {call_id}")

# =================== Run ===================
if __name__ == "__main__":
    log.info("🚀 Running FastAPI server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

