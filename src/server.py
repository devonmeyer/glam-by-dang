import os
import sys
import time
import asyncio
import logging
import hmac
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

NEW_CONVERSATION_THRESHOLD = 60 * 60  # 1 hour
DEBOUNCE_SECONDS = 5
MAX_MESSAGE_LENGTH = 1000
SESSION_TTL = 24 * 60 * 60  # 24 hours

load_dotenv(Path(__file__).parent.parent / ".env")

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("Error: ANTHROPIC_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
    sys.exit(1)

INSTAGRAM_VERIFY_TOKEN = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "")
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
INSTAGRAM_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
DAILY_SUMMARY_RECIPIENT = os.environ.get("DAILY_SUMMARY_RECIPIENT", "")
GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
ET = ZoneInfo("America/New_York")

from agent import process_message

server_start_time: float = time.time()
last_summary_time: float = time.time()  # initialise to start time — no false restart flag on first run


@dataclass
class ConversationRecord:
    sender_id: str
    display_name: str = ""
    conversation_summary: str = ""
    actions_taken: list = dc_field(default_factory=list)
    is_escalation: bool = False
    timestamp: float = 0.0


daily_log: dict[str, ConversationRecord] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_daily_summary_scheduler())
    yield


app = FastAPI(title="Glam by Dang DM Simulator", lifespan=lifespan)


class SessionState:
    def __init__(self):
        self.history: list[dict] = []
        self.last_reply_time: Optional[float] = None
        self.last_activity: float = time.time()
        self.pending_messages: list[str] = []
        self.debounce_task: Optional[asyncio.Task] = None
        self.debounce_future: Optional[asyncio.Future] = None
        self.kha_active: bool = False
        self.last_kha_reply_time: Optional[float] = None


sessions: dict[str, SessionState] = {}


def evict_stale_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    stale = [sid for sid, s in sessions.items() if s.last_activity < cutoff]
    for sid in stale:
        del sessions[sid]
    if stale:
        logger.info("evicted %d stale session(s)", len(stale))


def get_session(session_id: str) -> SessionState:
    if session_id not in sessions:
        evict_stale_sessions()
        sessions[session_id] = SessionState()
        logger.info("new session: %s", session_id)
    session = sessions[session_id]
    session.last_activity = time.time()
    return session


class MessageRequest(BaseModel):
    session_id: str
    message: str


class KhaReplyRequest(BaseModel):
    session_id: str
    message: str


class SeedHistoryRequest(BaseModel):
    session_id: str
    history: list[dict]


class ResetRequest(BaseModel):
    session_id: str


@app.post("/chat")
async def chat(req: MessageRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if len(req.message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail="Message too long.")
    logger.info("incoming message: session=%s length=%d", req.session_id, len(req.message))

    session = get_session(req.session_id)
    now = time.time()

    # If Kha is actively handling this conversation, stay silent
    if session.kha_active:
        last_kha = session.last_kha_reply_time or 0
        if (now - last_kha) < NEW_CONVERSATION_THRESHOLD:
            return {"action": "silent", "category": None, "classification": None,
                    "messages": None, "escalation": None}
        else:
            session.kha_active = False  # Enough time has passed — re-engage

    session.pending_messages.append(req.message.strip())

    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()

    loop = asyncio.get_running_loop()
    if session.debounce_future is None or session.debounce_future.done():
        session.debounce_future = loop.create_future()

    future = session.debounce_future

    async def debounce():
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        fire_time = time.time()

        # Re-check kha_active — she may have replied during the debounce window
        if session.kha_active and session.last_kha_reply_time and \
                (fire_time - session.last_kha_reply_time) < NEW_CONVERSATION_THRESHOLD:
            session.pending_messages.clear()
            if not future.done():
                future.set_result({"action": "silent", "category": None,
                                   "classification": None, "messages": None, "escalation": None})
            return

        combined = "\n".join(session.pending_messages)
        session.pending_messages.clear()

        is_new_conversation = (
            session.last_reply_time is None or
            (fire_time - session.last_reply_time) >= NEW_CONVERSATION_THRESHOLD
        )

        try:
            result = process_message(combined, session.history, is_new_conversation)
        except Exception as e:
            logger.error("process_message failed: %s", e, exc_info=True)
            result = {
                "action": "reply",
                "category": "escalate",
                "classification": {"confidence": "low"},
                "messages": ["I'll make sure Kha sees this and gets back to you! 🤍"],
                "escalation": None,
            }

        if result.get("messages"):
            session.history.append({"role": "user", "content": combined})
            session.history.append({"role": "assistant", "content": " ".join(result["messages"])})
            session.last_reply_time = fire_time

        if not future.done():
            future.set_result(result)

    session.debounce_task = asyncio.create_task(debounce())
    return await future


@app.post("/kha-reply")
async def kha_reply(req: KhaReplyRequest):
    """Simulate Kha replying directly — marks assistant as inactive for this conversation."""
    session = get_session(req.session_id)
    session.history.append({"role": "assistant", "content": req.message})
    session.kha_active = True
    session.last_kha_reply_time = time.time()
    logger.info("kha replied directly: session=%s", req.session_id)
    return {"status": "ok"}


@app.post("/seed-history")
async def seed_history(req: SeedHistoryRequest):
    """Pre-load a conversation history to simulate a returning customer."""
    session = get_session(req.session_id)
    # Cancel any in-flight debounce
    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()
    session.history = req.history
    session.last_reply_time = None   # treat next message as a new conversation
    session.kha_active = False
    session.pending_messages = []
    return {"status": "ok"}


@app.post("/reset")
async def reset(req: ResetRequest):
    if req.session_id in sessions:
        s = sessions[req.session_id]
        if s.debounce_task and not s.debounce_task.done():
            s.debounce_task.cancel()
    sessions[req.session_id] = SessionState()
    logger.info("session reset: %s", req.session_id)
    return {"status": "ok"}


async def _send_instagram_message(recipient_id: str, text: str) -> None:
    if not META_PAGE_ACCESS_TOKEN:
        logger.warning("META_PAGE_ACCESS_TOKEN not set — skipping Instagram send")
        return
    token = META_PAGE_ACCESS_TOKEN.strip()
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{GRAPH_API_BASE}/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        if resp.status_code != 200:
            logger.error("Instagram send failed %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Instagram message sent to %s", recipient_id)


async def _fetch_instagram_name(sender_id: str) -> str:
    if not META_PAGE_ACCESS_TOKEN:
        return sender_id
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/{sender_id}",
                headers={"Authorization": f"Bearer {META_PAGE_ACCESS_TOKEN.strip()}"},
                params={"fields": "name,username"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("username"):
                    return f"@{data['username']}"
                if data.get("name"):
                    return data["name"]
    except Exception as e:
        logger.warning("Failed to fetch name for %s: %s", sender_id, e)
    return sender_id


async def _log_conversation(sender_id: str, result: dict) -> None:
    display_name = await _fetch_instagram_name(sender_id)
    daily_log[sender_id] = ConversationRecord(
        sender_id=sender_id,
        display_name=display_name,
        conversation_summary=result.get("conversation_summary", ""),
        actions_taken=result.get("actions_taken", []),
        is_escalation=result.get("action") == "escalate",
        timestamp=time.time(),
    )


async def _handle_instagram_dm(sender_id: str, text: str) -> None:
    session = get_session(sender_id)
    now = time.time()

    if session.kha_active:
        last_kha = session.last_kha_reply_time or 0
        if (now - last_kha) < NEW_CONVERSATION_THRESHOLD:
            return
        session.kha_active = False

    session.pending_messages.append(text)

    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()

    async def debounce():
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        fire_time = time.time()

        if session.kha_active and session.last_kha_reply_time and \
                (fire_time - session.last_kha_reply_time) < NEW_CONVERSATION_THRESHOLD:
            session.pending_messages.clear()
            return

        combined = "\n".join(session.pending_messages)
        session.pending_messages.clear()

        is_new_conversation = (
            session.last_reply_time is None or
            (fire_time - session.last_reply_time) >= NEW_CONVERSATION_THRESHOLD
        )

        try:
            result = process_message(combined, session.history, is_new_conversation)
        except Exception as e:
            logger.error("process_message failed: %s", e, exc_info=True)
            result = {
                "action": "reply",
                "category": "escalate",
                "messages": ["I'll make sure Kha sees this and gets back to you! 🤍"],
                "escalation": None,
            }

        if result.get("messages"):
            session.history.append({"role": "user", "content": combined})
            session.history.append({"role": "assistant", "content": " ".join(result["messages"])})
            session.last_reply_time = fire_time
            for msg_text in result["messages"]:
                await _send_instagram_message(sender_id, msg_text)

        await _log_conversation(sender_id, result)

    session.debounce_task = asyncio.create_task(debounce())


async def _send_daily_summary() -> None:
    global last_summary_time

    if not DAILY_SUMMARY_RECIPIENT:
        logger.warning("DAILY_SUMMARY_RECIPIENT not set — skipping daily summary")
        return

    now_et = datetime.now(ET)
    date_str = now_et.strftime("%A, %B %-d")

    restart_note = ""
    if server_start_time > last_summary_time:
        restart_dt = datetime.fromtimestamp(server_start_time, tz=ET)
        restart_note = f"⚠️ Note: The assistant restarted at {restart_dt.strftime('%-I:%M %p ET')} — some conversations before then may be missing.\n\n"

    escalations = [r for r in daily_log.values() if r.is_escalation]
    handled = [r for r in daily_log.values() if not r.is_escalation]

    if not escalations and not handled:
        chunks = [f"📅 Daily Summary — {date_str}\n\n{restart_note}No DMs to report today."]
    else:
        lines: list[str] = [f"📅 Daily Summary — {date_str}\n"]
        if restart_note:
            lines.append(restart_note)
        if escalations:
            lines.append("⚠️ Needs Attention\n")
            for r in escalations:
                actions = ", ".join(r.actions_taken) if r.actions_taken else "Escalated"
                lines.append(f"• {r.display_name} — {r.conversation_summary} — {actions}\n")
        if handled:
            lines.append("\n✅ For Your Visibility\n")
            for r in handled:
                actions = ", ".join(r.actions_taken) if r.actions_taken else "Handled"
                lines.append(f"• {r.display_name} — {r.conversation_summary} — {actions}\n")

        chunks: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) > 980:
                chunks.append(current.rstrip())
                current = line
            else:
                current += line
        if current.strip():
            chunks.append(current.rstrip())

    for chunk in chunks:
        await _send_instagram_message(DAILY_SUMMARY_RECIPIENT, chunk)

    last_summary_time = time.time()
    daily_log.clear()
    logger.info("Daily summary sent: %d escalations, %d handled", len(escalations), len(handled))


async def _daily_summary_scheduler() -> None:
    while True:
        now = datetime.now(ET)
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info("Daily summary scheduled in %.0f seconds", wait_seconds)
        await asyncio.sleep(wait_seconds)
        try:
            await _send_daily_summary()
        except Exception as e:
            logger.error("Daily summary failed: %s", e, exc_info=True)


@app.get("/webhook")
async def webhook_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == INSTAGRAM_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def instagram_webhook(request: Request):
    body_bytes = await request.body()

    # TODO: re-enable HMAC check once META_APP_SECRET is confirmed correct in Railway
    # if META_APP_SECRET:
    #     sig_header = request.headers.get("X-Hub-Signature-256", "")
    #     expected = "sha256=" + hmac.new(
    #         META_APP_SECRET.encode(), body_bytes, hashlib.sha256
    #     ).hexdigest()
    #     if sig_header and not hmac.compare_digest(sig_header, expected):
    #         raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body_bytes)

    if payload.get("object") != "instagram":
        return {"status": "ok"}

    for entry in payload.get("entry", []):
        # Real Instagram DMs arrive in messaging[], Meta's test button uses changes[]
        events = entry.get("messaging", [])
        if not events:
            events = [c.get("value", {}) for c in entry.get("changes", []) if c.get("field") == "messages"]

        for event in events:
            sender_id = event.get("sender", {}).get("id")
            if not sender_id:
                continue
            msg = event.get("message", {})
            if msg.get("is_echo") or "text" not in msg:
                continue
            text = msg["text"].strip()
            if not text or len(text) > MAX_MESSAGE_LENGTH:
                continue
            logger.info("Instagram DM from %s: %d chars", sender_id, len(text))
            asyncio.create_task(_handle_instagram_dm(sender_id, text))

    return {"status": "ok"}


static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/privacy")
async def privacy():
    return FileResponse(os.path.join(static_dir, "privacy.html"))
