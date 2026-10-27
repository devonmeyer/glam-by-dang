"""Persistent state in SQLite on the Railway volume.

Holds what used to live in in-memory dicts (the daily summary log, owner-takeover
markers, ids of messages Mia sent) so a redeploy no longer drops them. Every row
carries a business_id so a second business can be added without mixing data.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- Which business each connected Instagram account belongs to (the id webhooks carry).
CREATE TABLE IF NOT EXISTS instagram_accounts (
    account_id TEXT PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    created_at REAL NOT NULL
);
-- Tokens for accounts connected through Instagram login, encrypted (see connected_tokens.py).
-- app_user_id is the id Meta uses in deauthorize and data deletion callbacks.
CREATE TABLE IF NOT EXISTS instagram_tokens (
    account_id TEXT PRIMARY KEY,
    app_user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    token_enc TEXT NOT NULL,
    expires_at REAL,
    obtained_at REAL NOT NULL
);
-- One-time links that let an owner connect their Instagram to a business.
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    account_id TEXT
);
-- Business settings created or edited in the admin panel ({file name: text} as JSON).
CREATE TABLE IF NOT EXISTS business_settings (
    slug TEXT PRIMARY KEY,
    files TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- Every saved version of a business's settings, for history and undo.
CREATE TABLE IF NOT EXISTS business_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    files TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    saved_at REAL NOT NULL
);
-- One row per customer per day's summary; replaced as the conversation goes on.
CREATE TABLE IF NOT EXISTS conversations (
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    sender_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    conversation_summary TEXT NOT NULL DEFAULT '',
    actions_taken TEXT NOT NULL DEFAULT '[]',
    is_escalation INTEGER NOT NULL DEFAULT 0,
    timestamp REAL NOT NULL,
    PRIMARY KEY (business_id, sender_id)
);
-- When the owner last replied to a customer by hand (Mia stays quiet after).
CREATE TABLE IF NOT EXISTS owner_takeovers (
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    sender_id TEXT NOT NULL,
    confirmed_at REAL NOT NULL,
    PRIMARY KEY (business_id, sender_id)
);
-- Ids of messages Mia sent, to tell her echoes apart from the owner's.
CREATE TABLE IF NOT EXISTS bot_messages (
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    message_id TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (business_id, message_id)
);
-- People who can log in to the panel: admins see everything, owners only their business.
-- session_epoch goes up when a password changes or access is turned off, which ends old sessions.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    business_slug TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_login_at REAL
);
-- Links that let someone set or reset their password.
CREATE TABLE IF NOT EXISTS password_links (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
-- Event management (see events.py): per-business setup, the events, and the people in each line.
-- hook_token is the secret in the JotForm webhook URL that enrolls guests.
CREATE TABLE IF NOT EXISTS event_settings (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
    jotform_url TEXT NOT NULL DEFAULT '',
    venmo TEXT NOT NULL DEFAULT '',
    hook_token TEXT NOT NULL UNIQUE,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    tagline TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL
);
-- status: waiting, now (being tattooed), done, no_show. position orders the line.
-- submission_id is JotForm's, so a resent webhook doesn't add someone twice; NULL for walk-ins.
CREATE TABLE IF NOT EXISTS event_guests (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL DEFAULT '',
    submission_id TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    position INTEGER NOT NULL,
    joined_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (event_id, submission_id)
);
"""


@dataclass
class ConversationRecord:
    sender_id: str
    display_name: str = ""
    conversation_summary: str = ""
    actions_taken: list = field(default_factory=list)
    is_escalation: bool = False
    timestamp: float = 0.0


def default_db_path() -> str:
    """MIA_DB_PATH wins; otherwise the Railway volume, or memory when there is none."""
    if os.environ.get("MIA_DB_PATH"):
        return os.environ["MIA_DB_PATH"]
    if Path("/data").is_dir():
        return "/data/mia.db"
    logger.warning("No /data volume and MIA_DB_PATH unset — state will not survive a restart")
    return ":memory:"


class Store:
    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        # Opened on first use so importing the server (e.g. in tests) touches no disk.
        if self._conn is None:
            path = self.path or default_db_path()
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            logger.info("State database opened at %s", path)
        return self._conn

    def _migrate(self) -> None:
        """Add columns that databases created before them don't have yet."""
        invite_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(invites)")}
        if "reusable" not in invite_cols:
            # A reusable invite (for Meta's app reviewers) keeps working after it's used
            self._conn.execute("ALTER TABLE invites ADD COLUMN reusable INTEGER NOT NULL DEFAULT 0")
        event_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}
        if "tagline" not in event_cols:
            # The line under the event's name on the guest page and display
            self._conn.execute("ALTER TABLE events ADD COLUMN tagline TEXT NOT NULL DEFAULT ''")

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def ensure_business(self, slug: str, name: str) -> int:
        self._execute(
            "INSERT OR IGNORE INTO businesses (slug, name, created_at) VALUES (?, ?, ?)",
            (slug, name, self.clock()),
        )
        return self._execute("SELECT id FROM businesses WHERE slug = ?", (slug,))[0][0]

    def business_count(self) -> int:
        return self._execute("SELECT COUNT(*) FROM businesses")[0][0]

    # Instagram accounts

    def register_account(self, account_id: str, business_id: int) -> None:
        self._execute(
            "INSERT OR REPLACE INTO instagram_accounts (account_id, business_id, created_at) "
            "VALUES (?, ?, ?)",
            (account_id, business_id, self.clock()),
        )

    def account_business(self, account_id: str) -> Optional[tuple[int, str]]:
        """(business id, business slug) for a connected account, or None if unknown."""
        rows = self._execute(
            "SELECT b.id, b.slug FROM instagram_accounts a JOIN businesses b ON b.id = a.business_id "
            "WHERE a.account_id = ?",
            (account_id,),
        )
        return (rows[0][0], rows[0][1]) if rows else None

    def accounts_for_business(self, business_id: int) -> list[str]:
        rows = self._execute(
            "SELECT account_id FROM instagram_accounts WHERE business_id = ? ORDER BY created_at",
            (business_id,),
        )
        return [r[0] for r in rows]

    def account_details(self, business_id: int) -> list[tuple[str, str, Optional[float]]]:
        """(account id, username, token expiry) for each account linked to a business."""
        rows = self._execute(
            "SELECT a.account_id, COALESCE(t.username, ''), t.expires_at FROM instagram_accounts a "
            "LEFT JOIN instagram_tokens t ON t.account_id = a.account_id "
            "WHERE a.business_id = ? ORDER BY a.created_at",
            (business_id,),
        )
        return [(r[0], r[1], r[2]) for r in rows]

    # Business settings from the admin panel

    def saved_business(self, slug: str) -> Optional[dict[str, str]]:
        rows = self._execute("SELECT files FROM business_settings WHERE slug = ?", (slug,))
        return json.loads(rows[0][0]) if rows else None

    def saved_business_slugs(self) -> list[str]:
        return [r[0] for r in self._execute("SELECT slug FROM business_settings ORDER BY slug")]

    def save_business(self, slug: str, files: dict[str, str]) -> None:
        self._execute(
            "INSERT OR REPLACE INTO business_settings (slug, files, updated_at) VALUES (?, ?, ?)",
            (slug, json.dumps(files), self.clock()),
        )

    def add_business_version(self, slug: str, files: dict[str, str], note: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO business_versions (slug, files, note, saved_at) VALUES (?, ?, ?, ?)",
                (slug, json.dumps(files), note, self.clock()),
            )
            return cur.lastrowid

    def business_versions(self, slug: str, limit: int = 50) -> list[tuple[int, str, float]]:
        """(version id, note, saved_at), newest first."""
        rows = self._execute(
            "SELECT id, note, saved_at FROM business_versions WHERE slug = ? ORDER BY id DESC LIMIT ?",
            (slug, limit),
        )
        return [(r[0], r[1], r[2]) for r in rows]

    def business_version(self, version_id: int) -> Optional[tuple[str, dict[str, str]]]:
        rows = self._execute("SELECT slug, files FROM business_versions WHERE id = ?", (version_id,))
        return (rows[0][0], json.loads(rows[0][1])) if rows else None

    def discard_saved_business(self, slug: str) -> None:
        self._execute("DELETE FROM business_settings WHERE slug = ?", (slug,))

    # Tokens for connected accounts

    def save_token(self, account_id: str, token_enc: str, expires_at: Optional[float], obtained_at: float,
                   app_user_id: str = "", username: str = "") -> None:
        """Insert or update; an empty app_user_id or username keeps what was saved before."""
        self._execute(
            "INSERT INTO instagram_tokens (account_id, app_user_id, username, token_enc, expires_at, obtained_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
            "token_enc = excluded.token_enc, expires_at = excluded.expires_at, "
            "obtained_at = excluded.obtained_at, "
            "app_user_id = CASE WHEN excluded.app_user_id = '' THEN app_user_id ELSE excluded.app_user_id END, "
            "username = CASE WHEN excluded.username = '' THEN username ELSE excluded.username END",
            (account_id, app_user_id, username, token_enc, expires_at, obtained_at),
        )

    def load_token(self, account_id: str) -> Optional[tuple[str, Optional[float], float]]:
        """(encrypted token, expires_at, obtained_at), or None if the account never connected."""
        rows = self._execute(
            "SELECT token_enc, expires_at, obtained_at FROM instagram_tokens WHERE account_id = ?",
            (account_id,),
        )
        return (rows[0][0], rows[0][1], rows[0][2]) if rows else None

    def connected_accounts(self) -> list[str]:
        return [r[0] for r in self._execute("SELECT account_id FROM instagram_tokens ORDER BY account_id")]

    def account_for_user(self, user_id: str) -> Optional[str]:
        """The connected account matching an id from Meta (account id or app-scoped user id)."""
        rows = self._execute(
            "SELECT account_id FROM instagram_tokens WHERE account_id = ? OR app_user_id = ?",
            (user_id, user_id),
        )
        return rows[0][0] if rows else None

    def disconnect_account(self, account_id: str) -> None:
        self._execute("DELETE FROM instagram_tokens WHERE account_id = ?", (account_id,))
        self._execute("DELETE FROM instagram_accounts WHERE account_id = ?", (account_id,))

    def delete_business_history(self, business_id: int) -> None:
        """Drop stored conversations, takeovers and sent-message ids for a business."""
        for table in ("conversations", "owner_takeovers", "bot_messages"):
            self._execute(f"DELETE FROM {table} WHERE business_id = ?", (business_id,))

    # Connect invites

    def create_invite(self, token_hash: str, business_id: int, expires_at: float, reusable: bool = False) -> None:
        self._execute(
            "INSERT INTO invites (token_hash, business_id, created_at, expires_at, reusable) VALUES (?, ?, ?, ?, ?)",
            (token_hash, business_id, self.clock(), expires_at, int(reusable)),
        )

    def open_invite(self, token_hash: str) -> Optional[tuple[int, str]]:
        """(business id, slug) for an unused, unexpired invite, else None."""
        rows = self._execute(
            "SELECT b.id, b.slug FROM invites i JOIN businesses b ON b.id = i.business_id "
            "WHERE i.token_hash = ? AND i.used_at IS NULL AND i.expires_at > ?",
            (token_hash, self.clock()),
        )
        return (rows[0][0], rows[0][1]) if rows else None

    def use_invite(self, token_hash: str, account_id: str) -> None:
        self._execute(
            "UPDATE invites SET used_at = CASE WHEN reusable THEN NULL ELSE ? END, account_id = ? "
            "WHERE token_hash = ?",
            (self.clock(), account_id, token_hash),
        )

    # People who can log in

    USER_COLUMNS = "id, email, role, business_slug, password_hash, active, session_epoch, created_at, last_login_at"

    def create_user(self, email: str, role: str, business_slug: str = "") -> int:
        self._execute("INSERT INTO users (email, role, business_slug, created_at) VALUES (?, ?, ?, ?)",
                      (email, role, business_slug, self.clock()))
        return self._execute("SELECT id FROM users WHERE email = ?", (email,))[0][0]

    def user(self, user_id: int) -> Optional[tuple]:
        rows = self._execute(f"SELECT {self.USER_COLUMNS} FROM users WHERE id = ?", (user_id,))
        return tuple(rows[0]) if rows else None

    def user_by_email(self, email: str) -> Optional[tuple]:
        rows = self._execute(f"SELECT {self.USER_COLUMNS} FROM users WHERE email = ?", (email,))
        return tuple(rows[0]) if rows else None

    def users(self) -> list[tuple]:
        return [tuple(r) for r in self._execute(f"SELECT {self.USER_COLUMNS} FROM users ORDER BY role, email")]

    def set_password(self, user_id: int, password_hash: str) -> None:
        """Also ends every session the person had, and any other password links."""
        self._execute("UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
                      (password_hash, user_id))
        self._execute("UPDATE password_links SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                      (self.clock(), user_id))

    def set_user_active(self, user_id: int, active: bool) -> None:
        self._execute("UPDATE users SET active = ?, session_epoch = session_epoch + 1 WHERE id = ?",
                      (int(active), user_id))
        if not active:
            self._execute("UPDATE password_links SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                          (self.clock(), user_id))

    def record_login(self, user_id: int) -> None:
        self._execute("UPDATE users SET last_login_at = ? WHERE id = ?", (self.clock(), user_id))

    def create_password_link(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self._execute("INSERT INTO password_links (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (token_hash, user_id, self.clock(), expires_at))

    def open_password_link(self, token_hash: str) -> Optional[int]:
        """The user id for an unused, unexpired link, else None."""
        rows = self._execute(
            "SELECT l.user_id FROM password_links l JOIN users u ON u.id = l.user_id "
            "WHERE l.token_hash = ? AND l.used_at IS NULL AND l.expires_at > ? AND u.active = 1",
            (token_hash, self.clock()),
        )
        return rows[0][0] if rows else None

    # Daily summary log

    def log_conversation(self, business_id: int, record: ConversationRecord) -> None:
        self._execute(
            "INSERT OR REPLACE INTO conversations (business_id, sender_id, display_name, "
            "conversation_summary, actions_taken, is_escalation, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (business_id, record.sender_id, record.display_name, record.conversation_summary,
             json.dumps(record.actions_taken), int(record.is_escalation), record.timestamp),
        )

    def conversations(self, business_id: int) -> list[ConversationRecord]:
        rows = self._execute(
            "SELECT sender_id, display_name, conversation_summary, actions_taken, is_escalation, "
            "timestamp FROM conversations WHERE business_id = ? ORDER BY timestamp",
            (business_id,),
        )
        return [
            ConversationRecord(r[0], r[1], r[2], json.loads(r[3]), bool(r[4]), r[5]) for r in rows
        ]

    def clear_conversations(self, business_id: int, up_to: float) -> None:
        """Drop what a summary covered; conversations logged while it was sending stay."""
        self._execute(
            "DELETE FROM conversations WHERE business_id = ? AND timestamp <= ?",
            (business_id, up_to),
        )

    # Owner takeover

    def mark_owner_reply(self, business_id: int, sender_id: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO owner_takeovers (business_id, sender_id, confirmed_at) "
            "VALUES (?, ?, ?)",
            (business_id, sender_id, self.clock()),
        )

    def owner_replied_within(self, business_id: int, sender_id: str, seconds: float) -> bool:
        rows = self._execute(
            "SELECT confirmed_at FROM owner_takeovers WHERE business_id = ? AND sender_id = ?",
            (business_id, sender_id),
        )
        return bool(rows) and self.clock() - rows[0][0] < seconds

    # Messages Mia sent

    def record_bot_message(self, business_id: int, message_id: str, ttl: float) -> None:
        now = self.clock()
        self._execute(
            "INSERT OR REPLACE INTO bot_messages (business_id, message_id, sent_at) VALUES (?, ?, ?)",
            (business_id, message_id, now),
        )
        self._execute(
            "DELETE FROM bot_messages WHERE business_id = ? AND sent_at < ?",
            (business_id, now - ttl),
        )

    def is_bot_message(self, business_id: int, message_id: str) -> bool:
        return bool(self._execute(
            "SELECT 1 FROM bot_messages WHERE business_id = ? AND message_id = ?",
            (business_id, message_id),
        ))


    # Event management

    EVENT_SETTINGS_COLUMNS = "jotform_url, venmo, hook_token"
    GUEST_COLUMNS = "id, first_name, last_name, submission_id, status, position, joined_at, updated_at"

    def event_settings(self, business_id: int, create_token: str = "") -> Optional[dict]:
        """{jotform_url, venmo, hook_token}; with create_token, a missing row is created first."""
        if create_token:
            self._execute(
                "INSERT OR IGNORE INTO event_settings (business_id, hook_token, updated_at) VALUES (?, ?, ?)",
                (business_id, create_token, self.clock()),
            )
        rows = self._execute(f"SELECT {self.EVENT_SETTINGS_COLUMNS} FROM event_settings WHERE business_id = ?",
                             (business_id,))
        return dict(zip(("jotform_url", "venmo", "hook_token"), rows[0])) if rows else None

    def save_event_settings(self, business_id: int, jotform_url: str, venmo: str) -> None:
        self._execute("UPDATE event_settings SET jotform_url = ?, venmo = ?, updated_at = ? WHERE business_id = ?",
                      (jotform_url, venmo, self.clock(), business_id))

    def business_for_event_hook(self, hook_token: str) -> Optional[int]:
        rows = self._execute("SELECT business_id FROM event_settings WHERE hook_token = ?", (hook_token,))
        return rows[0][0] if rows else None

    def live_event(self, business_id: int) -> Optional[dict]:
        rows = self._execute(
            "SELECT id, name, tagline, started_at FROM events WHERE business_id = ? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (business_id,))
        return dict(zip(("id", "name", "tagline", "started_at"), rows[0])) if rows else None

    def start_event(self, business_id: int, name: str, tagline: str = "") -> int:
        with self._lock:
            return self.conn.execute("INSERT INTO events (business_id, name, tagline, started_at) VALUES (?, ?, ?, ?)",
                                     (business_id, name, tagline, self.clock())).lastrowid

    def update_event(self, event_id: int, name: str, tagline: str) -> None:
        self._execute("UPDATE events SET name = ?, tagline = ? WHERE id = ?", (name, tagline, event_id))

    def end_event(self, event_id: int) -> None:
        self._execute("UPDATE events SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (self.clock(), event_id))

    def past_events(self, business_id: int, limit: int = 20) -> list[dict]:
        rows = self._execute(
            "SELECT e.id, e.name, e.started_at, e.ended_at, COUNT(g.id), "
            "COALESCE(SUM(g.status = 'done'), 0) FROM events e LEFT JOIN event_guests g ON g.event_id = e.id "
            "WHERE e.business_id = ? AND e.ended_at IS NOT NULL GROUP BY e.id ORDER BY e.id DESC LIMIT ?",
            (business_id, limit))
        return [dict(zip(("id", "name", "started_at", "ended_at", "guests", "done"), r)) for r in rows]

    def add_guest(self, event_id: int, first_name: str, last_name: str,
                  submission_id: Optional[str] = None) -> Optional[int]:
        """The new guest's id, at the back of the line; None if that submission is already in."""
        now = self.clock()
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO event_guests (event_id, first_name, last_name, submission_id, position, "
                "joined_at, updated_at) VALUES (?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(position), 0) + 1 FROM event_guests WHERE event_id = ?), ?, ?)",
                (event_id, first_name, last_name, submission_id, event_id, now, now))
            return cur.lastrowid if cur.rowcount else None

    def guests(self, event_id: int) -> list[dict]:
        rows = self._execute(f"SELECT {self.GUEST_COLUMNS} FROM event_guests WHERE event_id = ? "
                             "ORDER BY position, id", (event_id,))
        return [dict(zip(self.GUEST_COLUMNS.split(", "), r)) for r in rows]

    def set_guest_status(self, event_id: int, guest_id: int, status: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "UPDATE event_guests SET status = ?, updated_at = ? WHERE id = ? AND event_id = ?",
                (status, self.clock(), guest_id, event_id)).rowcount > 0

    def remove_guest(self, event_id: int, guest_id: int) -> bool:
        with self._lock:
            return self.conn.execute("DELETE FROM event_guests WHERE id = ? AND event_id = ?",
                                     (guest_id, event_id)).rowcount > 0

    def reorder_guests(self, event_id: int, guest_ids: list[int]) -> None:
        """Puts these guests first, in this order; everyone else keeps their order after them."""
        rest = [g["id"] for g in self.guests(event_id) if g["id"] not in set(guest_ids)]
        with self._lock:
            for position, guest_id in enumerate([*guest_ids, *rest], start=1):
                self.conn.execute("UPDATE event_guests SET position = ? WHERE id = ? AND event_id = ?",
                                  (position, guest_id, event_id))
