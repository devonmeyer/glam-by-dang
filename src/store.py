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

