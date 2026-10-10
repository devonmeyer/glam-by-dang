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
            logger.info("State database opened at %s", path)
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def ensure_business(self, slug: str, name: str) -> int:
        self._execute(
            "INSERT OR IGNORE INTO businesses (slug, name, created_at) VALUES (?, ?, ?)",
            (slug, name, self.clock()),
        )
        return self._execute("SELECT id FROM businesses WHERE slug = ?", (slug,))[0][0]

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

