"""Panel logins: people sign in with an email and password.

Admins (Devon) see every business; owners see only their own. Passwords are hashed with
scrypt. A session is a signed cookie naming the person and their session epoch, which goes
up when their password changes or their access is turned off, so old sessions stop working.
"""
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

ROLES = ("admin", "owner")
MIN_PASSWORD_LENGTH = 10
SESSION_TTL = 7 * 24 * 60 * 60
SETUP_LINK_TTL = 7 * 24 * 60 * 60
RESET_LINK_TTL = 24 * 60 * 60

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


@dataclass
class User:
    id: int
    email: str
    role: str
    business_slug: str
    has_password: bool
    active: bool
    session_epoch: int
    created_at: float
    last_login_at: Optional[float]

    @classmethod
    def from_row(cls, row: tuple) -> "User":
        uid, email, role, slug, pw_hash, active, epoch, created, last_login = row
        return cls(uid, email, role, slug, bool(pw_hash), bool(active), epoch, created, last_login)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_access(self, slug: str) -> bool:
        return self.is_admin or (self.role == "owner" and self.business_slug == slug)

    def to_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "role": self.role, "business_slug": self.business_slug,
                "has_password": self.has_password, "active": self.active,
                "created_at": self.created_at, "last_login_at": self.last_login_at}


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email and len(email) <= 254


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), dklen=32, **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


# Compared against when the email doesn't exist, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def password_problem(password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 200:
        return "That password is too long"
    return None


def _sig(key: str, payload: str) -> str:
    return hmac.new(key.encode(), f"mia-session:{payload}".encode(), hashlib.sha256).hexdigest()


def session_cookie(key: str, user: User, now: Optional[float] = None) -> str:
    expires = int((now or time.time()) + SESSION_TTL)
    payload = f"{user.id}.{user.session_epoch}.{expires}"
    return f"{payload}.{_sig(key, payload)}"


def read_session_cookie(key: str, cookie: str, now: Optional[float] = None) -> Optional[tuple[int, int]]:
    """(user id, session epoch) from a valid, unexpired cookie, else None."""
    parts = cookie.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
        return None
    uid, epoch, expires, sig = parts
    if int(expires) < (now or time.time()):
        return None
    if not hmac.compare_digest(sig, _sig(key, f"{uid}.{epoch}.{expires}")):
        return None
    return int(uid), int(epoch)


def hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottle:
    """Refuses more than `limit` failed logins per email within `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 15 * 60):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = {}

    def blocked(self, email: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(email, []) if now - t < self.window]
        self.failures[email] = recent
        return len(recent) >= self.limit

    def failed(self, email: str) -> None:
        self.failures.setdefault(email, []).append(time.time())

    def succeeded(self, email: str) -> None:
        self.failures.pop(email, None)
