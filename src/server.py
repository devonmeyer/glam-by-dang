import os
import sys
import time
import asyncio
import logging
import hmac
import hashlib
import json
from contextlib import asynccontextmanager
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

from instagram_token import TokenStore
from business import default_business
from store import ConversationRecord, Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

NEW_CONVERSATION_THRESHOLD = 60 * 60  # 1 hour
DEBOUNCE_SECONDS = 5
MAX_MESSAGE_LENGTH = 1000
MAX_HISTORY = 20
SESSION_TTL = 24 * 60 * 60  # 24 hours

load_dotenv(Path(__file__).parent.parent / ".env")

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("Error: ANTHROPIC_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
    sys.exit(1)

INSTAGRAM_VERIFY_TOKEN = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "")
META_PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
DAILY_SUMMARY_RECIPIENT = os.environ.get("DAILY_SUMMARY_RECIPIENT", "")
GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
ET = ZoneInfo("America/New_York")

TOKEN_FILE = Path("/data/meta_token.txt")
TOKEN_REFRESH_RETRY_INTERVAL = 6 * 60 * 60    # back off 6h between failed refresh attempts

# Token state — loaded at startup and updated by the auto-refresh background task
token_store = TokenStore(TOKEN_FILE, META_PAGE_ACCESS_TOKEN)


def _get_token() -> str:
    return token_store.token


def _note_auth_failure(resp: httpx.Response, action: str) -> None:
    if resp.status_code == 401:
        msg = f"Instagram rejected the access token (401) during {action}"
        logger.error("%s — set a fresh META_PAGE_ACCESS_TOKEN in Railway", msg)
        token_store.record_failure(msg)


async def _check_token_health() -> None:
    """Probe the token with a cheap API call and log a warning if it is dead, failing to
    refresh, or close to expiry. Runs at startup and before each daily summary, so the
    Railway logs carry the warning even when the summary DM itself can't be sent."""
    token = token_store.token
    if not token:
        return
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/me",
                params={"fields": "user_id"},
                headers={"Authorization": f"Bearer {token.strip()}"},
            )
        if resp.status_code != 200:
            logger.warning("Token health check failed %s: %s", resp.status_code, resp.text)
            _note_auth_failure(resp, "the daily token check")
    except httpx.HTTPError as e:
        logger.warning("Token health check could not reach Instagram: %s", e)
    note = token_store.health_note()
    if note:
        logger.warning(note)


async def _refresh_instagram_token() -> bool:
    token = token_store.token
    if not token:
        logger.warning("Token refresh skipped — no token set")
        return False
    async with httpx.AsyncClient() as http:
        resp = await http.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
    if resp.status_code != 200:
        logger.error("Token refresh failed %s: %s", resp.status_code, resp.text)
        token_store.record_failure(f"refresh failed with HTTP {resp.status_code}")
        return False
    data = resp.json()
    new_token = data.get("access_token", "")
    if not new_token:
        logger.error("Token refresh returned no access_token: %s", resp.text)
        token_store.record_failure("refresh returned no token")
        return False
    token_store.apply_refresh(new_token, data.get("expires_in"))
    logger.info("Instagram access token refreshed and persisted (expires in %ss)", data.get("expires_in"))
    return True

BOT_SENT_TTL = 24 * 60 * 60
own_account_id: str = ""                   # set from first incoming webhook

from agent import process_message

# Summary log, owner takeovers and Mia's sent-message ids, persisted across redeploys
store = Store()
_business_id: Optional[int] = None


def business_id() -> int:
    """Kha's business is the only one until webhooks are routed by account."""
    global _business_id
    if _business_id is None:
        _business_id = store.ensure_business(default_business().slug, default_business().name)
    return _business_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    token_store.load()
    asyncio.create_task(_check_token_health())
    asyncio.create_task(_daily_summary_scheduler())
    asyncio.create_task(_token_refresh_scheduler())
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
                "messages": [default_business().escalation_reply],
                "escalation": None,
            }

        if result.get("messages"):
            session.history.append({"role": "user", "content": combined})
            session.history.append({"role": "assistant", "content": " ".join(result["messages"])})
            session.last_reply_time = fire_time

        asyncio.create_task(_log_conversation(req.session_id, result))

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
    token = _get_token()
    if not token:
        logger.warning("META_PAGE_ACCESS_TOKEN not set — skipping Instagram send")
        return
    token = token.strip()
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{GRAPH_API_BASE}/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        if resp.status_code != 200:
            logger.error("Instagram send failed %s: %s", resp.status_code, resp.text)
            _note_auth_failure(resp, "a message send")
        else:
            data = resp.json()
            msg_id = data.get("message_id") or data.get("id")
            if msg_id:
                _record_bot_message(msg_id)
            logger.info("Instagram message sent to %s (mid=%s)", recipient_id, msg_id)


def _record_bot_message(msg_id: str) -> None:
    store.record_bot_message(business_id(), msg_id, BOT_SENT_TTL)


def _is_kha_active(sender_id: str, recent_outbound: list[dict]) -> bool:
    if store.owner_replied_within(business_id(), sender_id, NEW_CONVERSATION_THRESHOLD):
        return True
    return any(not store.is_bot_message(business_id(), m["id"]) for m in recent_outbound)


async def _fetch_conversation_context(sender_id: str) -> tuple[list[dict], list[dict], Optional[float]]:
    """Returns (history, recent_outbound, last_assistant_ts).
    history is oldest-first [{role, content}].
    recent_outbound is [{id, ts}] for outbound messages within the last hour.
    last_assistant_ts is the timestamp of the most recent assistant message, or None.
    """
    if not _get_token() or not own_account_id:
        return [], [], None
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/me/conversations",
                headers={"Authorization": f"Bearer {_get_token().strip()}"},
                params={
                    "user_id": sender_id,
                    "fields": "messages.limit(25){id,message,from,created_time}",
                    "limit": 1,
                },
            )
            if resp.status_code != 200:
                logger.warning("Conversation fetch failed %s: %s", resp.status_code, resp.text)
                _note_auth_failure(resp, "a conversation fetch")
                return [], [], None

            convos = resp.json().get("data", [])
            if not convos:
                return [], [], None

            raw = list(reversed(convos[0].get("messages", {}).get("data", [])))
            now = time.time()
            history: list[dict] = []
            recent_outbound: list[dict] = []
            last_assistant_ts: Optional[float] = None

            for msg in raw:
                from_id = msg.get("from", {}).get("id", "")
                text = msg.get("message", "")
                msg_id = msg.get("id", "")
                try:
                    ts = datetime.fromisoformat(
                        msg.get("created_time", "").replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = 0.0

                if not text:
                    continue
                if from_id == sender_id:
                    history.append({"role": "user", "content": text})
                elif from_id == own_account_id:
                    history.append({"role": "assistant", "content": text})
                    last_assistant_ts = ts
                    if now - ts < NEW_CONVERSATION_THRESHOLD:
                        recent_outbound.append({"id": msg_id, "ts": ts})

            return history[-MAX_HISTORY:], recent_outbound, last_assistant_ts

    except Exception as e:
        logger.error("_fetch_conversation_context failed: %s", e, exc_info=True)
        return [], [], None


async def _fetch_instagram_name(sender_id: str) -> str:
    if not sender_id.isdigit():
        return "Simulator"
    if not _get_token():
        return sender_id
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/{sender_id}",
                headers={"Authorization": f"Bearer {_get_token().strip()}"},
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
    store.log_conversation(business_id(), ConversationRecord(
        sender_id=sender_id,
        display_name=display_name,
        conversation_summary=result.get("conversation_summary", ""),
        actions_taken=result.get("actions_taken", []),
        is_escalation=result.get("action") == "escalate",
        timestamp=time.time(),
    ))


async def _handle_instagram_dm(sender_id: str, text: str) -> None:
    session = get_session(sender_id)

    # Fast bail if Kha was confirmed active via a recent echo event
    if store.owner_replied_within(business_id(), sender_id, NEW_CONVERSATION_THRESHOLD):
        logger.info("Kha confirmed active for %s — skipping", sender_id)
        return

    session.pending_messages.append(text)

    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()

    async def debounce():
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        fire_time = time.time()
        combined = "\n".join(session.pending_messages)
        session.pending_messages.clear()

        # Fetch conversation context from Instagram API
        history, recent_outbound, last_assistant_ts = await _fetch_conversation_context(sender_id)

        # Pre-process Kha check
        if _is_kha_active(sender_id, recent_outbound):
            logger.info("Kha active for %s — skipping", sender_id)
            return

        is_new_conversation = (
            last_assistant_ts is None or
            (fire_time - last_assistant_ts) >= NEW_CONVERSATION_THRESHOLD
        )

        try:
            result = process_message(combined, history, is_new_conversation)
        except Exception as e:
            logger.error("process_message failed: %s", e, exc_info=True)
            result = {
                "action": "reply",
                "category": "escalate",
                "messages": [default_business().escalation_reply],
                "escalation": None,
                "conversation_summary": "",
                "actions_taken": [],
            }

        if not result.get("messages"):
            await _log_conversation(sender_id, result)
            return

        # Pre-send Kha check — did she reply while Claude was thinking?
        _, recent_outbound_now, _ = await _fetch_conversation_context(sender_id)
        if _is_kha_active(sender_id, recent_outbound_now):
            logger.info("Kha replied while processing %s — not sending", sender_id)
            return

        for msg_text in result["messages"]:
            await _send_instagram_message(sender_id, msg_text)

        await _log_conversation(sender_id, result)

    session.debounce_task = asyncio.create_task(debounce())


async def _build_summary_chunks(records: list[ConversationRecord]) -> list[str]:
    now_et = datetime.now(ET)
    date_str = now_et.strftime("%A, %B %-d")

    token_note = token_store.health_note()
    if token_note:
        token_note = f"{token_note}\n\n"

    escalations = [r for r in records if r.is_escalation]
    handled = [r for r in records if not r.is_escalation]

    if not escalations and not handled:
        return [f"📅 Daily Summary — {date_str}\n\n{token_note}No DMs to report today."]

    lines: list[str] = [f"📅 Daily Summary — {date_str}\n"]
    if token_note:
        lines.append(token_note)
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
    return chunks


async def _send_daily_summary() -> None:
    if not DAILY_SUMMARY_RECIPIENT:
        logger.warning("DAILY_SUMMARY_RECIPIENT not set — skipping daily summary")
        return

    records = store.conversations(business_id())
    chunks = await _build_summary_chunks(records)
    escalations = [r for r in records if r.is_escalation]
    handled = [r for r in records if not r.is_escalation]

    for chunk in chunks:
        await _send_instagram_message(DAILY_SUMMARY_RECIPIENT, chunk)

    if records:
        store.clear_conversations(business_id(), up_to=records[-1].timestamp)
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
            await _check_token_health()
        except Exception as e:
            logger.error("Token health check failed: %s", e, exc_info=True)
        try:
            await _send_daily_summary()
        except Exception as e:
            logger.error("Daily summary failed: %s", e, exc_info=True)


async def _token_refresh_scheduler() -> None:
    while True:
        await asyncio.sleep(token_store.seconds_until_refresh())
        try:
            ok = await _refresh_instagram_token()
        except Exception as e:
            logger.error("Token refresh failed: %s", e, exc_info=True)
            ok = False
        if not ok:
            # Don't hot-loop against a token that can't be refreshed — wait before retrying.
            await asyncio.sleep(TOKEN_REFRESH_RETRY_INTERVAL)


@app.post("/trigger-summary")
async def trigger_summary():
    await _send_daily_summary()
    return {"status": "ok"}


@app.get("/preview-summary")
async def preview_summary():
    chunks = await _build_summary_chunks(store.conversations(business_id()))
    return {"chunks": chunks}


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


def _verify_webhook_signature(body_bytes: bytes, sig_header: str) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the raw request body.

    Fails closed: a missing secret or missing/mismatched header is rejected.
    With Instagram Login, Meta signs with the Instagram app secret, not the
    Facebook app secret, so META_APP_SECRET must hold the Instagram one.
    """
    if not META_APP_SECRET:
        logger.error("META_APP_SECRET is not set; rejecting all webhook POSTs")
        return False
    if not sig_header:
        logger.warning("Webhook POST rejected: missing X-Hub-Signature-256")
        return False
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode(), body_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        logger.warning("Webhook POST rejected: signature mismatch")
        return False
    return True


@app.post("/webhook")
async def instagram_webhook(request: Request):
    global own_account_id

    body_bytes = await request.body()

    if not _verify_webhook_signature(body_bytes, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body_bytes)

    if payload.get("object") != "instagram":
        return {"status": "ok"}

    for entry in payload.get("entry", []):
        events = entry.get("messaging", [])
        if not events:
            events = [c.get("value", {}) for c in entry.get("changes", []) if c.get("field") == "messages"]

        for event in events:
            sender_id = event.get("sender", {}).get("id")
            recipient_id = event.get("recipient", {}).get("id")
            msg = event.get("message", {})

            if msg.get("is_echo"):
                # Outbound message from our account — check if Kha sent it
                msg_id = msg.get("mid") or msg.get("id", "")
                if msg_id and not store.is_bot_message(business_id(), msg_id):
                    user_id = recipient_id
                    if user_id:
                        store.mark_owner_reply(business_id(), user_id)
                        logger.info("Kha reply detected via echo for user %s", user_id)
                continue

            # Learn our own account ID from the recipient field of incoming messages
            if recipient_id and not own_account_id:
                own_account_id = recipient_id
                logger.info("Own account ID set to %s", own_account_id)

            if not sender_id or "text" not in msg:
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
