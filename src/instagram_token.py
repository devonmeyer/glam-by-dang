"""Instagram access token persistence and refresh scheduling.

The token lives in a JSON file on the persistent volume so refreshed tokens survive
redeploys. The file also records a fingerprint of the META_PAGE_ACCESS_TOKEN env var
the refresh chain started from: when the env var changes (e.g. after pasting a new
token into Railway), the env token wins and starts a new chain, so a stale file can
never override it.
"""
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

REFRESH_MARGIN = 10 * 24 * 60 * 60        # refresh this long before the token expires
MIN_TOKEN_AGE = 24 * 60 * 60              # Instagram rejects refreshes of tokens < 24h old
EXPIRY_WARNING_WINDOW = 7 * 24 * 60 * 60  # flag in the daily summary inside this window


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path, env_token: str, clock: Callable[[], float] = time.time):
        self.path = path
        self.env_token = env_token.strip()
        self.clock = clock
        self.token = self.env_token
        self.env_fingerprint = _fingerprint(self.env_token) if self.env_token else ""
        self.expires_at: Optional[float] = None
        self.obtained_at: float = clock()
        self.last_error = ""

    def load(self) -> None:
        saved = self._read_file()
        env_fp = self.env_fingerprint
        if saved and (not self.env_token or saved.get("env_fingerprint") == env_fp):
            self.token = saved["token"]
            self.env_fingerprint = saved.get("env_fingerprint", "")
            self.expires_at = saved.get("expires_at")
            self.obtained_at = saved.get("obtained_at", self.clock())
            logger.info("Loaded Instagram access token from %s", self.path)
            return
        if not self.env_token:
            logger.warning("No META_PAGE_ACCESS_TOKEN set — Instagram features disabled")
            return
        if saved:
            logger.info("META_PAGE_ACCESS_TOKEN changed — replacing persisted token with it")
        # Fresh chain from the env token. Its expiry is unknown until the first refresh.
        self.token = self.env_token
        self.expires_at = None
        self.obtained_at = self.clock()
        self._write_file()

    def seconds_until_refresh(self) -> float:
        now = self.clock()
        if self.expires_at is not None:
            return max(0.0, self.expires_at - REFRESH_MARGIN - now)
        # Unknown expiry: refresh as soon as Instagram allows, which also tells us
        # the real expiry for every refresh after that.
        return max(0.0, self.obtained_at + MIN_TOKEN_AGE - now)

    def apply_refresh(self, new_token: str, expires_in: Optional[float]) -> None:
        now = self.clock()
        self.token = new_token
        self.obtained_at = now
        self.expires_at = now + expires_in if expires_in else None
        self.last_error = ""
        self._write_file()

    def record_failure(self, message: str) -> None:
        self.last_error = message

    def health_note(self) -> str:
        """One-line warning for the daily summary, or "" when the token looks healthy."""
        if not self.token:
            return ""
        now = self.clock()
        if self.expires_at is not None and self.expires_at <= now:
            return "⚠️ Instagram token has expired — set a fresh META_PAGE_ACCESS_TOKEN in Railway."
        if self.last_error:
            return f"⚠️ Instagram token problem: {self.last_error}{self._expiry_suffix()}"
        if self.expires_at is not None and self.expires_at - now < EXPIRY_WARNING_WINDOW:
            return f"⚠️ Instagram token has not refreshed{self._expiry_suffix()}"
        return ""

    def _expiry_suffix(self) -> str:
        if self.expires_at is None:
            return "."
        return f" — it expires {datetime.fromtimestamp(self.expires_at).strftime('%B %-d')}."

    def _read_file(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        raw = self.path.read_text().strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Legacy plain-text file with no env fingerprint. Treat it as belonging to the
            # current env token only if they match; otherwise the env token wins.
            data = {"token": raw, "env_fingerprint": self.env_fingerprint if raw == self.env_token else ""}
        if not isinstance(data, dict) or not data.get("token"):
            return None
        return data

    def _write_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "token": self.token,
            "env_fingerprint": self.env_fingerprint,
            "expires_at": self.expires_at,
            "obtained_at": self.obtained_at,
        }))
        os.replace(tmp, self.path)
