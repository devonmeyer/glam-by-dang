import os
import sys
import time
import asyncio
import logging
import hmac
import hashlib
import json
import secrets
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, Response
from html import escape
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path
from urllib.parse import quote
import httpx
import anthropic

import events
import instagram_connect
from accounts import (ROLES, LoginThrottle, User, check_password, hash_link_token, hash_password,
                      normalize_email, password_problem, read_session_cookie, session_cookie, valid_email)
import accounts
from admin_assistant import Assistant
from connected_tokens import ConnectedTokenStore, crypto_configured
from instagram_connect import ConnectError
from instagram_token import TokenStore
import business as business_config
from business import (DEFAULT_BUSINESS_SLUG, SLUG_RE, Business, build_business, business_files,
                      business_slugs, business_source, default_business, load_business, starter_files)
from email_sender import email_configured, send_email
import email_templates
from store import ConversationRecord, Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


class RecentLogs(logging.Handler):
    """Keeps the latest log lines in memory for the admin panel (Railway keeps the full history)."""

    def __init__(self, size: int = 3000):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)


recent_logs = RecentLogs()
logging.getLogger().addHandler(recent_logs)

NEW_CONVERSATION_THRESHOLD = 60 * 60  # 1 hour
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


def _note_auth_failure(resp: httpx.Response, action: str, tokens: TokenStore = token_store) -> None:
    if resp.status_code == 401:
        msg = f"Instagram rejected the access token (401) during {action}"
        logger.error("%s — set a fresh META_PAGE_ACCESS_TOKEN in Railway", msg)
        tokens.record_failure(msg)


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


async def _refresh_instagram_token(tokens: TokenStore = token_store) -> bool:
    token = tokens.token
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
        tokens.record_failure(f"refresh failed with HTTP {resp.status_code}")
        return False
    data = resp.json()
    new_token = data.get("access_token", "")
    if not new_token:
        logger.error("Token refresh returned no access_token: %s", resp.text)
        tokens.record_failure("refresh returned no token")
        return False
    tokens.apply_refresh(new_token, data.get("expires_in"))
    logger.info("Instagram access token refreshed and persisted (expires in %ss)", data.get("expires_in"))
    return True

BOT_SENT_TTL = 24 * 60 * 60

from agent import process_message

# Summary log, owner takeovers and Mia's sent-message ids, persisted across redeploys
store = Store()
_business_id: Optional[int] = None

# Businesses created or edited in the admin panel are read from the database.
business_config.use_saved_settings(lambda slug: store.saved_business(slug), lambda: store.saved_business_slugs())


def business_id() -> int:
    """Database id of the default business (Kha's), which the simulator and summary use."""
    global _business_id
    if _business_id is None:
        _business_id = store.ensure_business(default_business().slug, default_business().name)
    return _business_id


@dataclass
class Account:
    """An Instagram account Mia answers DMs on, and the business it belongs to."""
    account_id: str
    business: Business
    business_id: int
    tokens: TokenStore

    @property
    def token(self) -> str:
        return self.tokens.token.strip()


_connected_tokens: dict[str, ConnectedTokenStore] = {}


def _tokens_for(account_id: str, slug: str) -> Optional[TokenStore]:
    """An account connected through Instagram login uses its own saved token; Kha's
    account falls back to the META_PAGE_ACCESS_TOKEN chain until she reconnects."""
    if account_id in _connected_tokens:
        return _connected_tokens[account_id]
    if account_id and store.load_token(account_id):
        tokens = ConnectedTokenStore(store, account_id)
        tokens.load()
        _connected_tokens[account_id] = tokens
        return tokens
    return token_store if slug == default_business().slug else None


def _business_tokens(business: Business) -> Optional[TokenStore]:
    accounts = store.accounts_for_business(store.ensure_business(business.slug, business.name))
    return _tokens_for(accounts[0] if accounts else "", business.slug)


def default_account() -> Account:
    accounts = store.accounts_for_business(business_id())
    return Account(accounts[0] if accounts else "", default_business(), business_id(),
                   _business_tokens(default_business()) or token_store)


def _account_for(account_id: str) -> Optional[Account]:
    """The account a webhook entry belongs to, or None if Mia doesn't serve it."""
    found = store.account_business(account_id)
    if found is None:
        if store.business_count() > 1:
            return None
        # Only Kha's business exists: learn her account id from her first webhook.
        store.register_account(account_id, business_id())
        logger.info("Instagram account %s registered to %s", account_id, default_business().slug)
        found = (business_id(), default_business().slug)
    biz_id, slug = found
    tokens = _tokens_for(account_id, slug)
    if tokens is None:
        logger.warning("No Instagram token for business %s — ignoring account %s", slug, account_id)
        return None
    return Account(account_id, load_business(slug), biz_id, tokens)


@asynccontextmanager
async def lifespan(app: FastAPI):
    token_store.load()
    asyncio.create_task(_check_token_health())
    asyncio.create_task(_daily_summary_scheduler())
    asyncio.create_task(_token_refresh_scheduler())
    asyncio.create_task(_connected_token_refresh_scheduler())
    yield


app = FastAPI(title="Mia", lifespan=lifespan)


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


async def _send_instagram_message(account: Account, recipient_id: str, text: str) -> None:
    token = account.token
    if not token:
        logger.warning("No Instagram token for %s — skipping Instagram send", account.business.slug)
        return
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{GRAPH_API_BASE}/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
        )
        if resp.status_code != 200:
            logger.error("Instagram send failed %s: %s", resp.status_code, resp.text)
            _note_auth_failure(resp, "a message send", account.tokens)
        else:
            data = resp.json()
            msg_id = data.get("message_id") or data.get("id")
            if msg_id:
                store.record_bot_message(account.business_id, msg_id, BOT_SENT_TTL)
            logger.info("Instagram message sent to %s (mid=%s)", recipient_id, msg_id)


def _is_owner_active(account: Account, sender_id: str, recent_outbound: list[dict]) -> bool:
    if store.owner_replied_within(account.business_id, sender_id, NEW_CONVERSATION_THRESHOLD):
        return True
    return any(not store.is_bot_message(account.business_id, m["id"]) for m in recent_outbound)


async def _fetch_conversation_context(account: Account, sender_id: str) -> tuple[list[dict], list[dict], Optional[float]]:
    """Returns (history, recent_outbound, last_assistant_ts).
    history is oldest-first [{role, content}].
    recent_outbound is [{id, ts}] for outbound messages within the last hour.
    last_assistant_ts is the timestamp of the most recent assistant message, or None.
    """
    if not account.token or not account.account_id:
        return [], [], None
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/me/conversations",
                headers={"Authorization": f"Bearer {account.token}"},
                params={
                    "user_id": sender_id,
                    "fields": "messages.limit(25){id,message,from,created_time}",
                    "limit": 1,
                },
            )
            if resp.status_code != 200:
                logger.warning("Conversation fetch failed %s: %s", resp.status_code, resp.text)
                _note_auth_failure(resp, "a conversation fetch", account.tokens)
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
                elif from_id == account.account_id:
                    history.append({"role": "assistant", "content": text})
                    last_assistant_ts = ts
                    if now - ts < NEW_CONVERSATION_THRESHOLD:
                        recent_outbound.append({"id": msg_id, "ts": ts})

            return history[-MAX_HISTORY:], recent_outbound, last_assistant_ts

    except Exception as e:
        logger.error("_fetch_conversation_context failed: %s", e, exc_info=True)
        return [], [], None


async def _fetch_instagram_name(account: Account, sender_id: str) -> str:
    if not sender_id.isdigit():
        return "Simulator"
    if not account.token:
        return sender_id
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                f"{GRAPH_API_BASE}/{sender_id}",
                headers={"Authorization": f"Bearer {account.token}"},
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


async def _log_conversation(account: Account, sender_id: str, result: dict) -> None:
    display_name = await _fetch_instagram_name(account, sender_id)
    store.log_conversation(account.business_id, ConversationRecord(
        sender_id=sender_id,
        display_name=display_name,
        conversation_summary=result.get("conversation_summary", ""),
        actions_taken=result.get("actions_taken", []),
        is_escalation=result.get("action") == "escalate",
        timestamp=time.time(),
    ))


async def _handle_instagram_dm(account: Account, sender_id: str, text: str) -> None:
    session = get_session(f"{account.account_id}:{sender_id}")

    # Fast bail if the owner was confirmed active via a recent echo event
    if store.owner_replied_within(account.business_id, sender_id, NEW_CONVERSATION_THRESHOLD):
        logger.info("Owner confirmed active for %s — skipping", sender_id)
        return

    session.pending_messages.append(text)

    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()

    async def debounce():
        try:
            await asyncio.sleep(account.business.reply_delay_seconds)
        except asyncio.CancelledError:
            return

        fire_time = time.time()
        combined = "\n".join(session.pending_messages)
        session.pending_messages.clear()

        # Fetch conversation context from Instagram API
        history, recent_outbound, last_assistant_ts = await _fetch_conversation_context(account, sender_id)

        # Pre-process owner check
        if _is_owner_active(account, sender_id, recent_outbound):
            logger.info("Owner active for %s — skipping", sender_id)
            return

        is_new_conversation = (
            last_assistant_ts is None or
            (fire_time - last_assistant_ts) >= NEW_CONVERSATION_THRESHOLD
        )

        try:
            result = process_message(combined, history, is_new_conversation, account.business)
        except Exception as e:
            logger.error("process_message failed: %s", e, exc_info=True)
            result = {
                "action": "reply",
                "category": "escalate",
                "messages": [account.business.escalation_reply],
                "escalation": None,
                "conversation_summary": "",
                "actions_taken": [],
            }

        if not result.get("messages"):
            await _log_conversation(account, sender_id, result)
            return

        # Pre-send owner check — did they reply while Claude was thinking?
        _, recent_outbound_now, _ = await _fetch_conversation_context(account, sender_id)
        if _is_owner_active(account, sender_id, recent_outbound_now):
            logger.info("Owner replied while processing %s — not sending", sender_id)
            return

        for msg_text in result["messages"]:
            await _send_instagram_message(account, sender_id, msg_text)

        await _log_conversation(account, sender_id, result)

    session.debounce_task = asyncio.create_task(debounce())


def _summary_lines(business: Business, records: list[ConversationRecord]) -> list[str]:
    date_str = datetime.now(business.tz).strftime("%A, %B %-d")

    tokens = _business_tokens(business)
    token_note = tokens.health_note() if tokens else ""
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
    return lines


def _summary_html(business: Business, records: list[ConversationRecord]) -> str:
    """The branded email version of _summary_lines."""
    tokens = _business_tokens(business)
    rows = lambda rs, default: [(r.display_name, r.conversation_summary,
                                 ", ".join(r.actions_taken) if r.actions_taken else default) for r in rs]
    return email_templates.summary(
        business.assistant_name, business.owner_name, business.name,
        datetime.now(business.tz).strftime("%A, %B %-d"),
        tokens.health_note() if tokens else "",
        rows([r for r in records if r.is_escalation], "Escalated"),
        rows([r for r in records if not r.is_escalation], "Handled"),
    )


def _dm_chunks(lines: list[str]) -> list[str]:
    """Split summary lines into pieces short enough for one Instagram DM each."""
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


async def _send_daily_summary(slug: Optional[str] = None) -> None:
    """Email the owner their summary; Kha falls back to the old Instagram DM until email is set up."""
    business = load_business(slug) if slug else default_business()
    biz_id = store.ensure_business(business.slug, business.name)
    records = store.conversations(biz_id)
    lines = _summary_lines(business, records)

    if email_configured() and business.summary_email:
        subject = f"{business.assistant_name}'s daily summary for {business.name} — " \
                  f"{datetime.now(business.tz).strftime('%A, %B %-d')}"
        sent = await send_email(business.summary_email, subject, "".join(lines).rstrip(),
                                html=_summary_html(business, records))
    elif business.slug == default_business().slug and DAILY_SUMMARY_RECIPIENT:
        for chunk in _dm_chunks(lines):
            await _send_instagram_message(default_account(), DAILY_SUMMARY_RECIPIENT, chunk)
        sent = True
    else:
        logger.warning("No summary email for %s (or email not configured) — skipping daily summary", business.slug)
        sent = False

    if sent and records:
        store.clear_conversations(biz_id, up_to=records[-1].timestamp)
    logger.info("Daily summary for %s %s: %d conversations",
                business.slug, "sent" if sent else "not sent", len(records))


def _due_for_summary(at: datetime) -> list[Business]:
    """Businesses whose local summary hour starts at this moment."""
    return [
        b for b in (load_business(s) for s in business_slugs())
        if at.astimezone(b.tz).hour == b.summary_hour
    ]


async def _daily_summary_scheduler() -> None:
    """Wake at the top of every hour and send summaries to owners whose local hour has come."""
    while True:
        now = datetime.now(ET)
        target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        await asyncio.sleep((target - now).total_seconds())
        for business in _due_for_summary(target):
            if business.slug == default_business().slug:
                try:
                    await _check_token_health()
                except Exception as e:
                    logger.error("Token health check failed: %s", e, exc_info=True)
            try:
                await _send_daily_summary(business.slug)
            except Exception as e:
                logger.error("Daily summary for %s failed: %s", business.slug, e, exc_info=True)


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


async def _connected_token_refresh_scheduler() -> None:
    """Hourly: refresh any connected account's token that is due (10 days before expiry)."""
    while True:
        for account_id in store.connected_accounts():
            found = store.account_business(account_id)
            tokens = _tokens_for(account_id, found[1] if found else "")
            if tokens is None or tokens.seconds_until_refresh() > 0:
                continue
            try:
                await _refresh_instagram_token(tokens)
            except Exception as e:
                logger.error("Token refresh for %s failed: %s", account_id, e, exc_info=True)
        await asyncio.sleep(60 * 60)


SESSION_COOKIE = "mia_session"
# The X-Admin-Secret header (for curl) acts as an admin without a login.
SECRET_ADMIN = User(0, "admin-secret", "admin", "", True, True, 0, 0.0, None)
login_throttle = LoginThrottle()


def _session_key() -> str:
    """Sessions are signed with ADMIN_SECRET; without it nobody can log in."""
    return os.environ.get("ADMIN_SECRET", "")


def _bootstrap_admin() -> None:
    """Make sure the operator's admin login exists (ADMIN_EMAIL, devon@itsmia.info by default)."""
    email = normalize_email(os.environ.get("ADMIN_EMAIL", "devon@itsmia.info"))
    if email and store.user_by_email(email) is None:
        store.create_user(email, "admin")
        logger.info("Created the admin login %s", email)


def _current_user(request: Request) -> Optional[User]:
    key = _session_key()
    if not key:
        return None
    header = request.headers.get("x-admin-secret", "")
    if header and hmac.compare_digest(header, key):
        return SECRET_ADMIN
    found = read_session_cookie(key, request.cookies.get(SESSION_COOKIE, ""))
    if not found:
        return None
    row = store.user(found[0])
    if row is None:
        return None
    user = User.from_row(row)
    if not user.active or user.session_epoch != found[1]:
        return None
    return user


def _require_user(request: Request) -> User:
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _require_admin(request: Request) -> User:
    user = _require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _require_business(request: Request, slug: str) -> tuple[User, Business]:
    """The logged-in person and the business, if they're an admin or that business's owner."""
    user = _require_user(request)
    if not user.can_access(slug):
        raise HTTPException(status_code=404, detail=f"No business {slug!r}")
    return user, _admin_business(slug)


def _admin_business(slug: str) -> Business:
    if slug not in business_slugs():
        raise HTTPException(status_code=404, detail=f"No business {slug!r}")
    return load_business(slug)


@app.post("/trigger-summary")
async def trigger_summary(request: Request, slug: str = DEFAULT_BUSINESS_SLUG):
    """Send a business's daily summary now (?slug=..., Kha's by default)."""
    _require_admin(request)
    await _send_daily_summary(_admin_business(slug).slug)
    return {"status": "ok"}


@app.get("/preview-summary")
async def preview_summary(request: Request, slug: str = DEFAULT_BUSINESS_SLUG):
    """Show what a business's summary would say, without sending or clearing it."""
    _require_admin(request)
    business = _admin_business(slug)
    biz_id = store.ensure_business(business.slug, business.name)
    lines = _summary_lines(business, store.conversations(biz_id))
    return {"chunks": _dm_chunks(lines)}


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
    body_bytes = await request.body()

    if not _verify_webhook_signature(body_bytes, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body_bytes)

    if payload.get("object") != "instagram":
        return {"status": "ok"}

    for entry in payload.get("entry", []):
        # entry.id is the business's Instagram account the events happened on
        account = _account_for(str(entry.get("id", ""))) if entry.get("id") else None
        if account is None:
            logger.warning("Webhook entry for unknown Instagram account %s — ignoring", entry.get("id"))
            continue

        events = entry.get("messaging", [])
        if not events:
            events = [c.get("value", {}) for c in entry.get("changes", []) if c.get("field") == "messages"]

        for event in events:
            sender_id = event.get("sender", {}).get("id")
            recipient_id = event.get("recipient", {}).get("id")
            msg = event.get("message", {})

            if msg.get("is_echo"):
                # Outbound message from the business account — check if the owner sent it
                msg_id = msg.get("mid") or msg.get("id", "")
                if msg_id and not store.is_bot_message(account.business_id, msg_id):
                    user_id = recipient_id
                    if user_id:
                        store.mark_owner_reply(account.business_id, user_id)
                        logger.info("Owner reply detected via echo for user %s", user_id)
                continue

            if not sender_id or "text" not in msg:
                continue
            text = msg["text"].strip()
            if not text or len(text) > MAX_MESSAGE_LENGTH:
                continue
            logger.info("Instagram DM from %s: %d chars", sender_id, len(text))
            asyncio.create_task(_handle_instagram_dm(account, sender_id, text))

    return {"status": "ok"}


# --- Connecting a business's Instagram (Business Login for Instagram) ---

INVITE_TTL = 7 * 24 * 60 * 60
REVIEWER_INVITE_TTL = 30 * 24 * 60 * 60   # Meta's app review can take weeks


def _hash_invite(invite: str) -> str:
    return hashlib.sha256(invite.encode()).hexdigest()


BRAND_HEAD = """<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/static/brand/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/brand/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:opsz,wght@6..96,500&family=Figtree:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="/static/brand/brand.css">"""


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=f"""<!doctype html>
<html lang="en"><head>{BRAND_HEAD}
<title>{escape(title)}</title>
</head><body class="mia-page"><main class="mia-shell">
<span class="mia-lockup"><img src="/static/brand/mark.svg" alt=""><span class="mia-wordmark">mia</span></span>
<div class="mia-card"><h1>{escape(title)}</h1>{body_html}</div>
<p class="mia-footer"><a href="/privacy">Privacy policy</a></p>
</main></body></html>""")


class SlugRequest(BaseModel):
    slug: str


@app.post("/admin/invites")
async def create_invite(req: SlugRequest, request: Request):
    """Make a one-time link an owner opens to connect their Instagram to a business."""
    _require_admin(request)
    return _new_invite(_admin_business(req.slug))


def _new_invite(business: Business, reviewer: bool = False) -> dict:
    """A one-time link that expires in 7 days, or for Meta's reviewers, a reusable one for 30."""
    invite = secrets.token_urlsafe(24)
    expires_at = time.time() + (REVIEWER_INVITE_TTL if reviewer else INVITE_TTL)
    store.create_invite(_hash_invite(invite), store.ensure_business(business.slug, business.name), expires_at,
                        reusable=reviewer)
    logger.info("Connect %s created for %s", "reviewer link" if reviewer else "invite", business.slug)
    return {"url": f"{instagram_connect.base_url()}/connect?invite={invite}",
            "expires_at": datetime.fromtimestamp(expires_at, tz=ET).isoformat()}


@app.post("/admin/delete-data")
async def delete_business_data(req: SlugRequest, request: Request):
    """For an owner who asks directly: disconnect their Instagram and delete what Mia stored."""
    _require_admin(request)
    business = _admin_business(req.slug)
    biz_id = store.ensure_business(business.slug, business.name)
    accounts = store.accounts_for_business(biz_id)
    for account_id in accounts:
        store.disconnect_account(account_id)
        _connected_tokens.pop(account_id, None)
    store.delete_business_history(biz_id)
    logger.info("Deleted stored data for %s (%d accounts disconnected)", business.slug, len(accounts))
    return {"status": "deleted", "accounts_disconnected": len(accounts)}


@app.get("/connect")
async def connect_page(invite: str = ""):
    found = store.open_invite(_hash_invite(invite)) if invite else None
    if not found:
        return _page("This link has expired", "<p>Ask for a new connect link.</p>", 404)
    if not instagram_connect.connect_configured() or not crypto_configured():
        logger.error("Connect flow not configured (INSTAGRAM_APP_ID, META_APP_SECRET, PUBLIC_BASE_URL, TOKEN_ENCRYPTION_KEY)")
        return _page("Not available yet", "<p>Connecting isn't set up yet. Please try again later.</p>", 503)
    business = load_business(found[1])
    url = instagram_connect.authorize_url(state=invite)
    return _page(
        f"Connect {business.name} to {business.assistant_name}",
        f"<p>{escape(business.assistant_name)} will answer Instagram DMs for "
        f"{escape(business.name)} and hand anything sensitive to {escape(business.owner_name)}.</p>"
        f"<p>Log in with the business's Instagram account and allow message access.</p>"
        f'<a class="mia-button" href="{escape(url)}">Continue with Instagram</a>'
        f'<p class="mia-muted">{escape(business.assistant_name)} only reads and replies to direct messages. '
        f'Reply to anyone yourself and it steps back from that conversation.</p>',
    )


@app.get("/auth/instagram/callback")
async def instagram_callback(code: str = "", state: str = "", error_description: str = ""):
    invite_hash = _hash_invite(state) if state else ""
    found = store.open_invite(invite_hash) if state else None
    if not found:
        return _page("This link has expired", "<p>Ask for a new connect link.</p>", 404)
    if not code:
        reason = escape(error_description) or "Instagram access wasn't allowed."
        return _page("Not connected", f"<p>{reason}</p><p>You can open your link again to retry.</p>", 400)
    biz_id, slug = found
    business = load_business(slug)
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            # Instagram appends "#_" to the code in the redirect; it is not part of the code.
            short_token = await instagram_connect.exchange_code(http, code.removesuffix("#_"))
            token, expires_in = await instagram_connect.long_lived(http, short_token)
            me = await instagram_connect.whoami(http, token)
            await instagram_connect.subscribe(http, token)
    except ConnectError as e:
        logger.error("Connect for %s failed: %s", slug, e)
        return _page("Not connected", f"<p>{escape(str(e))}</p><p>You can open your link again to retry.</p>", 502)
    except httpx.HTTPError as e:
        logger.error("Connect for %s could not reach Instagram: %s", slug, e)
        return _page("Not connected", "<p>Couldn't reach Instagram. Open your link again to retry.</p>", 502)

    account_id = str(me["user_id"])
    store.register_account(account_id, biz_id)
    tokens = ConnectedTokenStore(store, account_id)
    tokens.save_new(token, expires_in, str(me.get("id", "")), me.get("username", ""))
    _connected_tokens[account_id] = tokens
    store.use_invite(invite_hash, account_id)
    logger.info("Instagram @%s (%s) connected to %s", me.get("username"), account_id, slug)
    return _page("You're connected",
                 f"<p>{escape(business.assistant_name)} is now set up for @{escape(me.get('username', ''))}. "
                 f"You can close this page.</p>")


def _disconnect_from_signed_request(signed_request: str) -> Optional[str]:
    payload = instagram_connect.parse_signed_request(signed_request)
    if payload is None:
        raise HTTPException(status_code=403, detail="Invalid signed_request")
    account_id = store.account_for_user(str(payload.get("user_id", "")))
    if account_id:
        store.disconnect_account(account_id)
        _connected_tokens.pop(account_id, None)
        logger.info("Instagram account %s disconnected by its owner", account_id)
    return account_id


@app.post("/auth/instagram/deauthorize")
async def instagram_deauthorize(signed_request: str = Form("")):
    """Meta calls this when an owner removes the app from their Instagram."""
    _disconnect_from_signed_request(signed_request)
    return {"status": "ok"}


@app.post("/auth/instagram/delete")
async def instagram_data_deletion(signed_request: str = Form("")):
    """Meta calls this when an owner asks for their data to be deleted."""
    payload = instagram_connect.parse_signed_request(signed_request)
    if payload is None:
        raise HTTPException(status_code=403, detail="Invalid signed_request")
    account_id = store.account_for_user(str(payload.get("user_id", "")))
    if account_id:
        found = store.account_business(account_id)
        _disconnect_from_signed_request(signed_request)
        if found:
            store.delete_business_history(found[0])
    code = secrets.token_hex(8)
    return {"url": f"{instagram_connect.base_url()}/auth/instagram/deletion?code={code}",
            "confirmation_code": code}


@app.get("/auth/instagram/deletion")
async def instagram_deletion_status(code: str = ""):
    return _page("Data deleted",
                 f"<p>Request {escape(code)}: the Instagram token and stored conversation summaries "
                 f"for this account have been deleted.</p>")


static_dir = os.path.join(os.path.dirname(__file__), "..", "static")


# --- Admin panel (/admin): admins and business owners log in with email and password ---


class Login(BaseModel):
    email: str = ""
    password: str = ""
    secret: str = ""   # the ADMIN_SECRET, as a fallback way in for the operator


class NewBusiness(BaseModel):
    slug: str
    name: str
    owner_name: str
    copy_from: str = ""


class BusinessFiles(BaseModel):
    files: dict[str, str]


class InviteEmail(BaseModel):
    email: str = ""
    reviewer: bool = False


class ForgotPassword(BaseModel):
    email: str


class SetPassword(BaseModel):
    token: str
    password: str


class NewUser(BaseModel):
    email: str
    role: str = "owner"
    business_slug: str = ""
    send_link: bool = True


class UserAccess(BaseModel):
    active: bool


class PasswordLinkRequest(BaseModel):
    send_email: bool = True


@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(static_dir, "admin.html"))


def _start_session(request: Request, response: Response, user: User) -> dict:
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(SESSION_COOKIE, session_cookie(_session_key(), user), max_age=accounts.SESSION_TTL,
                        httponly=True, secure=https, samesite="strict")
    store.record_login(user.id)
    logger.info("%s logged in to the panel", user.email)
    return {"user": user.to_dict()}


@app.post("/admin/api/login")
async def admin_login(req: Login, request: Request, response: Response):
    key = _session_key()
    if not key:
        raise HTTPException(status_code=503, detail="Logins aren't set up on the server (ADMIN_SECRET is missing)")
    _bootstrap_admin()
    if req.secret:
        if not hmac.compare_digest(req.secret.strip(), key):
            logger.warning("Panel login with the admin secret failed")
            raise HTTPException(status_code=403, detail="Wrong admin secret")
        row = store.user_by_email(normalize_email(os.environ.get("ADMIN_EMAIL", "devon@itsmia.info")))
        return _start_session(request, response, User.from_row(row))
    email = normalize_email(req.email)
    if login_throttle.blocked(email):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in 15 minutes or reset your password.")
    row = store.user_by_email(email)
    ok = check_password(req.password, row[4] if row and row[4] else accounts._DUMMY_HASH)
    user = User.from_row(row) if row else None
    if not (ok and user and user.active and user.has_password):
        login_throttle.failed(email)
        logger.warning("Panel login failed for %s", email or "(no email)")
        raise HTTPException(status_code=403, detail="Wrong email or password")
    login_throttle.succeeded(email)
    return _start_session(request, response, user)


@app.post("/admin/api/logout")
async def admin_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/admin/api/session")
async def admin_session(request: Request):
    user = _current_user(request)
    return {"admin": bool(user and user.is_admin), "user": user.to_dict() if user else None,
            "business_name": _safe_business_name(user.business_slug) if user and user.business_slug else ""}


def _base_url(request: Request) -> str:
    return instagram_connect.base_url() or str(request.base_url).rstrip("/")


async def _password_link(request: Request, user: User, send: bool) -> dict:
    """A link to set (first time) or reset a password, optionally emailed to the person."""
    setting_up = not user.has_password
    ttl = accounts.SETUP_LINK_TTL if setting_up else accounts.RESET_LINK_TTL
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl
    store.create_password_link(hash_link_token(token), user.id, expires_at)
    url = f"{_base_url(request)}/admin#password={token}"
    result = {"url": url, "kind": "setup" if setting_up else "reset",
              "expires_at": datetime.fromtimestamp(expires_at, tz=ET).isoformat()}
    if send:
        where = _safe_business_name(user.business_slug) if user.business_slug else "Mia"
        if setting_up:
            subject, heading = "Set up your Mia login", "Set up your Mia login"
            intro = f"You've been given access to {where} in Mia's dashboard. Choose a password to log in."
            valid = "The link works once and expires in 7 days."
        else:
            subject, heading = "Reset your Mia password", "Reset your Mia password"
            intro = "Use this link to choose a new password for Mia's dashboard."
            valid = "The link works once and expires in 24 hours. If you didn't ask for this, you can ignore it."
        text = f"{intro}\n\n{url}\n\nYour login email is {user.email}. {valid}\n"
        html = email_templates.password_link(heading, intro, user.email, url, valid)
        result["emailed"] = await send_email(user.email, subject, text, html=html)
    logger.info("Password %s link created for %s", result["kind"], user.email)
    return result


@app.post("/admin/api/password/forgot")
async def forgot_password(req: ForgotPassword, request: Request):
    """Emails a reset link if the address has a login. Always answers the same way."""
    _bootstrap_admin()
    row = store.user_by_email(normalize_email(req.email))
    if row:
        user = User.from_row(row)
        if user.active:
            await _password_link(request, user, send=True)
    return {"ok": True}


@app.get("/admin/api/password/{token}")
async def password_link_info(token: str):
    user_id = store.open_password_link(hash_link_token(token))
    if user_id is None:
        raise HTTPException(status_code=404, detail="This link has expired or was already used. Ask for a new one.")
    user = User.from_row(store.user(user_id))
    return {"email": user.email, "setting_up": not user.has_password}


@app.post("/admin/api/password")
async def set_password(req: SetPassword, request: Request, response: Response):
    user_id = store.open_password_link(hash_link_token(req.token))
    if user_id is None:
        raise HTTPException(status_code=404, detail="This link has expired or was already used. Ask for a new one.")
    problem = password_problem(req.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    store.set_password(user_id, hash_password(req.password))
    user = User.from_row(store.user(user_id))
    login_throttle.succeeded(user.email)
    logger.info("%s set a new password", user.email)
    return _start_session(request, response, user)


# --- People (admin only): who can log in, and to which business ---


def _user_or_404(user_id: int) -> User:
    row = store.user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such person")
    return User.from_row(row)


def _check_role(role: str, business_slug: str) -> str:
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="Role must be admin or owner")
    if role == "owner":
        if business_slug not in business_slugs():
            raise HTTPException(status_code=400, detail="Pick the business this owner manages")
        return business_slug
    return ""


@app.get("/admin/api/users")
async def admin_users(request: Request):
    me = _require_admin(request)
    _bootstrap_admin()
    return {"users": [User.from_row(r).to_dict() for r in store.users()], "me": me.id,
            "email_configured": email_configured()}


@app.post("/admin/api/users")
async def admin_create_user(req: NewUser, request: Request):
    _require_admin(request)
    email = normalize_email(req.email)
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if store.user_by_email(email):
        raise HTTPException(status_code=409, detail=f"{email} already has a login")
    slug = _check_role(req.role, req.business_slug)
    user = _user_or_404(store.create_user(email, req.role, slug))
    logger.info("Login created for %s (%s %s)", email, req.role, slug)
    link = await _password_link(request, user, send=True) if req.send_link else None
    return {"user": user.to_dict(), "link": link}


@app.post("/admin/api/users/{user_id}/password-link")
async def admin_password_link(user_id: int, req: PasswordLinkRequest, request: Request):
    _require_admin(request)
    user = _user_or_404(user_id)
    if not user.active:
        raise HTTPException(status_code=400, detail="Turn this person's access back on first")
    return await _password_link(request, user, send=req.send_email)


@app.post("/admin/api/users/{user_id}/access")
async def admin_user_access(user_id: int, req: UserAccess, request: Request):
    me = _require_admin(request)
    user = _user_or_404(user_id)
    if user.id == me.id and not req.active:
        raise HTTPException(status_code=400, detail="You can't turn off your own access")
    store.set_user_active(user.id, req.active)
    logger.info("Access for %s turned %s", user.email, "on" if req.active else "off")
    return {"user": _user_or_404(user_id).to_dict()}


def _save_business_files(slug: str, files: dict[str, str], note: str) -> Business:
    """Validate and save a business's files from the panel, keeping a version for undo.
    Raises ValueError with a readable reason if the files don't make a working business."""
    files = {name: files.get(name, "") for name in business_config.FILE_NAMES}
    business = build_business(slug, files)
    if not store.business_versions(slug, limit=1) and slug in business_slugs():
        # First panel save: keep what it replaces so it can be restored.
        store.add_business_version(slug, business_files(slug), "Starting point")
    store.save_business(slug, files)
    store.add_business_version(slug, files, note)
    load_business.cache_clear()
    store.ensure_business(slug, business.name)
    logger.info("Business %s saved: %s", slug, note)
    return business


def _business_overview(slug: str) -> dict:
    try:
        business = load_business(slug)
    except ValueError as e:
        return {"slug": slug, "name": slug, "error": str(e), "source": business_source(slug)}
    biz_id = store.ensure_business(business.slug, business.name)
    return {
        "slug": slug,
        "name": business.name,
        "owner_name": business.owner_name,
        "source": business_source(slug),
        "has_repo_folder": (business_config.BUSINESSES_DIR / slug / "business.json").is_file(),
        "summary_email": business.summary_email,
        "summary_hour": business.summary_hour,
        "reply_delay_seconds": business.reply_delay_seconds,
        "timezone": business.timezone,
        "pending_conversations": len(store.conversations(biz_id)),
        "accounts": [{"account_id": a, "username": u, "token_expires_at": e}
                     for a, u, e in store.account_details(biz_id)],
    }


@app.get("/admin/api/businesses")
async def admin_businesses(request: Request):
    user = _require_user(request)
    return {"businesses": [_business_overview(slug) for slug in business_slugs() if user.can_access(slug)],
            "email_configured": email_configured()}


@app.post("/admin/api/businesses")
async def admin_create_business(req: NewBusiness, request: Request):
    _require_admin(request)
    slug = req.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Use lowercase letters, numbers and dashes, e.g. lash-lab")
    if slug in business_slugs():
        raise HTTPException(status_code=409, detail=f"{slug} already exists")
    if req.copy_from:
        _admin_business(req.copy_from)
        files = dict(business_files(req.copy_from))
    else:
        files = starter_files()
    cfg = json.loads(files["business.json"])
    old_owner = cfg.get("owner_name", "")
    cfg["name"], cfg["owner_name"] = req.name.strip(), req.owner_name.strip()
    if old_owner:
        for key in ("greeting", "escalation_reply", "out_of_hours_note"):
            cfg[key] = cfg[key].replace(old_owner, cfg["owner_name"])
    cfg["summary_email"] = ""
    files["business.json"] = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    try:
        _save_business_files(slug, files, "Created in the admin panel")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _business_overview(slug)


@app.get("/admin/api/businesses/{slug}")
async def admin_business(slug: str, request: Request):
    _require_business(request, slug)
    return {**_business_overview(slug), "files": business_files(slug)}


@app.put("/admin/api/businesses/{slug}")
async def admin_save_business(slug: str, req: BusinessFiles, request: Request):
    user, _ = _require_business(request, slug)
    try:
        _save_business_files(slug, req.files, "Edited in the admin panel" if user.is_admin else f"Edited by {user.email}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _business_overview(slug)


@app.delete("/admin/api/businesses/{slug}/saved")
async def admin_discard_saved(slug: str, request: Request):
    """Drop panel edits so a business follows its repo folder again."""
    _require_admin(request)
    if not (business_config.BUSINESSES_DIR / slug / "business.json").is_file():
        raise HTTPException(status_code=400, detail="This business only exists in the panel; there's no repo folder to go back to")
    store.discard_saved_business(slug)
    load_business.cache_clear()
    store.add_business_version(slug, business_files(slug), "Back to the repo folder")
    logger.info("Business %s reverted to its repo folder", slug)
    return _business_overview(slug)


@app.get("/admin/api/businesses/{slug}/versions")
async def admin_versions(slug: str, request: Request):
    _require_business(request, slug)
    return {"versions": [{"id": i, "note": n, "saved_at": t} for i, n, t in store.business_versions(slug)]}


@app.post("/admin/api/businesses/{slug}/versions/{version_id}/restore")
async def admin_restore_version(slug: str, version_id: int, request: Request):
    _require_business(request, slug)
    found = store.business_version(version_id)
    if not found or found[0] != slug:
        raise HTTPException(status_code=404, detail="No such version")
    saved_at = next((t for i, _, t in store.business_versions(slug, limit=10_000) if i == version_id), None)
    when = datetime.fromtimestamp(saved_at, tz=ET).strftime("%b %-d, %-I:%M %p") if saved_at else f"#{version_id}"
    try:
        _save_business_files(slug, found[1], f"Restored the version from {when}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _business_overview(slug)


@app.get("/admin/api/businesses/{slug}/summary")
async def admin_preview_summary(slug: str, request: Request):
    _, business = _require_business(request, slug)
    lines = _summary_lines(business, store.conversations(store.ensure_business(business.slug, business.name)))
    return {"text": "".join(lines).rstrip()}


@app.post("/admin/api/businesses/{slug}/summary")
async def admin_send_summary(slug: str, request: Request):
    _require_admin(request)
    business = _admin_business(slug)
    await _send_daily_summary(business.slug)
    return {"ok": True, "to": business.summary_email if email_configured() and business.summary_email else ""}


@app.post("/admin/api/businesses/{slug}/invite")
async def admin_invite(slug: str, req: InviteEmail, request: Request):
    _require_admin(request)
    business = _admin_business(slug)
    if not instagram_connect.connect_configured() or not crypto_configured():
        raise HTTPException(status_code=400, detail="Instagram login isn't configured on the server yet")
    invite = _new_invite(business, reviewer=req.reviewer)
    email = req.email.strip()
    if email:
        text = (f"Hi {business.owner_name},\n\n"
                f"Here's your private link to connect {business.name}'s Instagram to {business.assistant_name}:\n\n"
                f"{invite['url']}\n\n"
                f"Open it, log in with your business Instagram account, and tap Allow. "
                f"The link works once and expires in 7 days.\n")
        html = email_templates.invite(business.owner_name, business.name, business.assistant_name, invite["url"])
        invite["emailed"] = await send_email(email, f"Connect your Instagram to {business.assistant_name}", text, html=html)
    return invite


# --- Admin assistant: a chat that proposes business edits for the admin to apply ---

assistant = Assistant(
    list_slugs=lambda: business_slugs(),
    business_name=lambda slug: _safe_business_name(slug),
    read_files=lambda slug: business_files(slug),
    validate=lambda slug, files: build_business(slug, files) and None,
)
_assistant_busy: set[str] = set()
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _safe_business_name(slug: str) -> str:
    try:
        return load_business(slug).name
    except ValueError:
        return slug


class AssistantImage(BaseModel):
    media_type: str
    data: str


class AssistantMessage(BaseModel):
    message: str
    chat_id: str = ""
    viewing: str = ""
    images: list[AssistantImage] = []


@app.post("/admin/api/assistant")
async def admin_assistant(req: AssistantMessage, request: Request):
    user = _require_user(request)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Type a message")
    if len(req.images) > 4:
        raise HTTPException(status_code=400, detail="Attach at most 4 images")
    for img in req.images:
        if img.media_type not in IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="Images must be PNG, JPEG, GIF or WebP")
        if len(img.data) > 7_000_000:
            raise HTTPException(status_code=400, detail="Each image must be under 5 MB")
    chat = assistant.chat(req.chat_id) if req.chat_id else None
    if chat is None or chat.user_id != user.id:
        # Owners' chats can only see and change their own business
        chat = assistant.new_chat(secrets.token_urlsafe(12), user_id=user.id,
                                  scope=None if user.is_admin else [user.business_slug])
    if chat.id in _assistant_busy:
        raise HTTPException(status_code=409, detail="Still working on your last message")
    _assistant_busy.add(chat.id)
    try:
        result = await assistant.send(chat, req.message.strip(), [i.model_dump() for i in req.images],
                                      req.viewing if req.viewing in assistant._slugs(chat) else "")
    except anthropic.APIError as e:
        logger.error("Admin assistant call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"The assistant couldn't reach Claude: {e}")
    finally:
        _assistant_busy.discard(chat.id)
    return {"chat_id": chat.id, **result}


@app.get("/admin/api/assistant/{chat_id}")
async def admin_assistant_chat(chat_id: str, request: Request):
    user = _require_user(request)
    chat = assistant.chat(chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="That chat has ended")
    return {"chat_id": chat.id, "transcript": chat.transcript,
            "proposals": [assistant.proposals[p].to_dict() for p in chat.proposal_ids]}


def _proposal(proposal_id: int, user: User):
    proposal = assistant.proposals.get(proposal_id)
    chat = assistant.chats.get(proposal.chat_id) if proposal else None
    if proposal is None or chat is None or chat.user_id != user.id or not user.can_access(proposal.slug):
        raise HTTPException(status_code=404, detail="That proposal has expired")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"That proposal was already {proposal.status}")
    return proposal


@app.post("/admin/api/assistant/proposals/{proposal_id}/apply")
async def admin_apply_proposal(proposal_id: int, request: Request):
    user = _require_user(request)
    proposal = _proposal(proposal_id, user)
    files = business_files(proposal.slug)
    if files[proposal.file] != proposal.before:
        raise HTTPException(status_code=409, detail=f"{proposal.file} changed since this was proposed. Ask the assistant to redo it.")
    try:
        _save_business_files(proposal.slug, {**files, proposal.file: proposal.after},
                             f"Assistant: {proposal.summary}" if proposal.summary else "Assistant edit")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    proposal.status = "applied"
    return {"proposal": proposal.to_dict(), "business": _business_overview(proposal.slug)}


@app.post("/admin/api/assistant/proposals/{proposal_id}/dismiss")
async def admin_dismiss_proposal(proposal_id: int, request: Request):
    user = _require_user(request)
    proposal = _proposal(proposal_id, user)
    proposal.status = "dismissed"
    return {"proposal": proposal.to_dict()}


# --- Simulator: try Mia's replies for a business without touching Instagram ---


class SimTurn(BaseModel):
    role: str
    content: str


class SimulateRequest(BaseModel):
    message: str
    history: list[SimTurn] = []
    new_conversation: bool = True


@app.post("/admin/api/businesses/{slug}/simulate")
async def admin_simulate(slug: str, req: SimulateRequest, request: Request):
    """Mia's reply to a made-up customer message, from the business's current settings.
    Nothing is sent to Instagram or logged to the daily summary."""
    _, business = _require_business(request, slug)
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Type a message")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail="Message too long")
    history = [{"role": t.role, "content": t.content} for t in req.history
               if t.role in ("user", "assistant") and t.content.strip()][-MAX_HISTORY:]
    try:
        result = await asyncio.to_thread(process_message, message, history, req.new_conversation, business)
    except anthropic.APIError as e:
        logger.error("Simulator call failed for %s: %s", slug, e)
        raise HTTPException(status_code=502, detail=f"Couldn't reach Claude: {e}")
    except Exception as e:
        logger.error("Simulator failed for %s: %s", slug, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Mia couldn't produce a reply. Check the logs.")
    logger.info("Simulated a reply for %s (%s)", slug, result.get("action"))
    return {k: result.get(k) for k in ("action", "category", "classification", "messages", "escalation",
                                       "conversation_summary", "actions_taken")}


@app.get("/admin/api/logs")
async def admin_logs(request: Request, q: str = "", level: str = "", limit: int = Query(500, le=3000)):
    _require_admin(request)
    levels = {"ERROR": ("ERROR", "CRITICAL"), "WARNING": ("WARNING", "ERROR", "CRITICAL")}.get(level.upper())
    needle = q.lower()
    lines = [r for r in recent_logs.records
             if (not levels or r["level"] in levels) and (not needle or needle in r["message"].lower())]
    return {"lines": lines[-limit:]}


# --- Event management: the line at a party, joined through a JotForm waiver (see events.py) ---


def _event_business(slug: str) -> tuple[Business, int, dict]:
    """A business with events set up, for the public pages; 404 otherwise."""
    if slug not in business_slugs():
        raise HTTPException(status_code=404, detail="No such event page")
    business = load_business(slug)
    biz_id = store.ensure_business(business.slug, business.name)
    settings = store.event_settings(biz_id)
    if settings is None:
        raise HTTPException(status_code=404, detail="No such event page")
    return business, biz_id, settings


def _venmo_url(handle: str) -> str:
    return f"https://venmo.com/u/{handle}" if handle else ""


def _event_links(request: Request, slug: str) -> dict:
    base = f"{_base_url(request)}/events/{slug}"
    return {"hub": base, "line": f"{base}/line", "display": f"{base}/display"}


@app.get("/events/{slug}")
@app.get("/events/{slug}/line")
async def event_page(slug: str):
    _event_business(slug)
    return FileResponse(os.path.join(static_dir, "event.html"))


@app.post("/events/{slug}/line")
async def event_page_after_waiver(slug: str, request: Request):
    """JotForm's thank-you page can redirect here with HTTP POST, which carries the submission id,
    so the person lands on the line with their own spot shown."""
    _event_business(slug)
    form = await request.form()
    submission_id = str(form.get("submission_id") or form.get("submissionID") or "").strip()
    query = f"?s={quote(submission_id)}" if submission_id else ""
    return RedirectResponse(f"/events/{slug}/line{query}", status_code=303)


@app.get("/events/{slug}/display")
async def event_display_page(slug: str):
    _event_business(slug)
    return FileResponse(os.path.join(static_dir, "event-display.html"))


@app.get("/events/{slug}/join")
async def event_join(slug: str):
    """Sends people to the waiver; submitting it puts them in line."""
    _, biz_id, settings = _event_business(slug)
    if not settings["jotform_url"] or not store.live_event(biz_id):
        return RedirectResponse(f"/events/{slug}/line", status_code=302)
    return RedirectResponse(settings["jotform_url"], status_code=302)


@app.get("/events/{slug}/qr.svg")
async def event_qr(slug: str, request: Request):
    _event_business(slug)
    return Response(events.qr_svg(_event_links(request, slug)["hub"]), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/events/{slug}/api/line")
async def event_line(slug: str, request: Request, s: str = ""):
    """The line as the public sees it: first names and last initials only.
    s is a JotForm submission id, to tell someone where they are."""
    business, biz_id, settings = _event_business(slug)
    event = store.live_event(biz_id)
    guests = store.guests(event["id"]) if event else []
    waiting = [g for g in guests if g["status"] == "waiting"]
    you = None
    mine = next((g for g in guests if s and g["submission_id"] == s), None)
    if mine:
        place = next((i for i, g in enumerate(waiting, start=1) if g["id"] == mine["id"]), None)
        you = {"name": events.public_name(mine["first_name"], mine["last_name"]), "status": mine["status"],
               "place": place}
    return JSONResponse({
        "business": {"name": business.name, "owner_name": business.owner_name},
        "event": {"name": event["name"]} if event else None,
        "now": [events.public_name(g["first_name"], g["last_name"]) for g in guests if g["status"] == "now"],
        "waiting": [events.public_name(g["first_name"], g["last_name"]) for g in waiting],
        "done": sum(g["status"] == "done" for g in guests),
        "you": you,
        "links": {
            "hub": _event_links(request, slug)["hub"],
            "join": f"/events/{slug}/join" if event and settings["jotform_url"] else "",
            "review": business.review_link,
            "tip": _venmo_url(settings["venmo"]),
        },
    }, headers={"Cache-Control": "no-store"})


@app.post("/hooks/jotform/{token}")
async def jotform_webhook(token: str, request: Request):
    """JotForm calls this when someone submits the waiver; they go to the back of the live event's line."""
    biz_id = store.business_for_event_hook(token)
    if biz_id is None:
        raise HTTPException(status_code=404, detail="Unknown webhook")
    form = await request.form()
    submission_id = str(form.get("submissionID", "")).strip() or None
    name = events.jotform_guest_name(str(form.get("rawRequest", "")), str(form.get("pretty", "")))
    event = store.live_event(biz_id)
    if event is None:
        logger.warning("JotForm submission %s arrived with no event running; nobody was added", submission_id)
        return {"ok": True, "added": False, "reason": "no event running"}
    if name is None:
        logger.warning("JotForm submission %s had no name field we recognize; nobody was added", submission_id)
        return {"ok": True, "added": False, "reason": "no name field"}
    guest_id = store.add_guest(event["id"], name[0], name[1], submission_id)
    if guest_id:
        logger.info("%s joined the line for %s", events.public_name(*name), event["name"])
    return {"ok": True, "added": bool(guest_id)}


class EventSettings(BaseModel):
    jotform_url: str = ""
    venmo: str = ""


class NewEvent(BaseModel):
    name: str


class NewGuest(BaseModel):
    first_name: str
    last_name: str = ""


class GuestStatus(BaseModel):
    status: str


class GuestOrder(BaseModel):
    guest_ids: list[int]


def _require_event_business(request: Request, slug: str) -> tuple[Business, int, dict]:
    """Like _require_business; also sets up events for the business the first time."""
    _, business = _require_business(request, slug)
    biz_id = store.ensure_business(business.slug, business.name)
    return business, biz_id, store.event_settings(biz_id, create_token=secrets.token_urlsafe(24))


def _require_live_event(biz_id: int) -> dict:
    event = store.live_event(biz_id)
    if event is None:
        raise HTTPException(status_code=409, detail="No event is running. Start one first.")
    return event


def _events_overview(request: Request, slug: str) -> dict:
    business, biz_id, settings = _require_event_business(request, slug)
    event = store.live_event(biz_id)
    guests = store.guests(event["id"]) if event else []
    return {
        "settings": {"jotform_url": settings["jotform_url"], "venmo": settings["venmo"],
                     "review_link": business.review_link,
                     "webhook_url": f"{_base_url(request)}/hooks/jotform/{settings['hook_token']}"},
        "links": _event_links(request, slug),
        "event": event,
        "guests": [{**{k: g[k] for k in ("id", "first_name", "last_name", "status", "joined_at", "updated_at")},
                    "walk_in": g["submission_id"] is None} for g in guests],
        "past": store.past_events(biz_id),
    }


@app.get("/admin/api/businesses/{slug}/events")
async def admin_events(slug: str, request: Request):
    return _events_overview(request, slug)


@app.put("/admin/api/businesses/{slug}/events/settings")
async def admin_event_settings(slug: str, req: EventSettings, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    url = req.jotform_url.strip()
    if url and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="The JotForm link should start with https://")
    try:
        venmo = events.venmo_handle(req.venmo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store.save_event_settings(biz_id, url, venmo)
    return _events_overview(request, slug)


@app.post("/admin/api/businesses/{slug}/events")
async def admin_start_event(slug: str, req: NewEvent, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    name = events.clean_name(req.name)
    if not name:
        raise HTTPException(status_code=400, detail="Give the event a name")
    if store.live_event(biz_id):
        raise HTTPException(status_code=409, detail="An event is already running. End it first.")
    store.start_event(biz_id, name)
    logger.info("Event %r started for %s", name, slug)
    return _events_overview(request, slug)


@app.post("/admin/api/businesses/{slug}/events/end")
async def admin_end_event(slug: str, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    event = _require_live_event(biz_id)
    store.end_event(event["id"])
    logger.info("Event %r ended for %s", event["name"], slug)
    return _events_overview(request, slug)


@app.post("/admin/api/businesses/{slug}/events/guests")
async def admin_add_guest(slug: str, req: NewGuest, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    event = _require_live_event(biz_id)
    first, last = events.clean_name(req.first_name), events.clean_name(req.last_name)
    if not first:
        raise HTTPException(status_code=400, detail="Enter a first name")
    store.add_guest(event["id"], first, last)
    return _events_overview(request, slug)


@app.post("/admin/api/businesses/{slug}/events/guests/{guest_id}")
async def admin_guest_status(slug: str, guest_id: int, req: GuestStatus, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    if req.status not in events.STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status")
    if not store.set_guest_status(_require_live_event(biz_id)["id"], guest_id, req.status):
        raise HTTPException(status_code=404, detail="That person isn't in this event's line")
    return _events_overview(request, slug)


@app.delete("/admin/api/businesses/{slug}/events/guests/{guest_id}")
async def admin_remove_guest(slug: str, guest_id: int, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    if not store.remove_guest(_require_live_event(biz_id)["id"], guest_id):
        raise HTTPException(status_code=404, detail="That person isn't in this event's line")
    return _events_overview(request, slug)


@app.post("/admin/api/businesses/{slug}/events/order")
async def admin_order_guests(slug: str, req: GuestOrder, request: Request):
    _, biz_id, _ = _require_event_business(request, slug)
    store.reorder_guests(_require_live_event(biz_id)["id"], req.guest_ids)
    return _events_overview(request, slug)


app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(static_dir, "home.html"))


@app.get("/privacy")
async def privacy():
    return FileResponse(os.path.join(static_dir, "privacy.html"))
