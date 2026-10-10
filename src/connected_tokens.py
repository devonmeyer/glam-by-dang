"""Tokens for Instagram accounts connected through Instagram login.

They live encrypted in the state database (TOKEN_ENCRYPTION_KEY, a Fernet key, set in
Railway; generate one with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Refresh and health warnings work exactly like Kha's original token in TokenStore.
"""
import os
import time
from typing import Callable

from cryptography.fernet import Fernet

from instagram_token import TokenStore
from store import Store


def crypto_configured() -> bool:
    return bool(os.environ.get("TOKEN_ENCRYPTION_KEY"))


def _fernet() -> Fernet:
    return Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(token_enc: str) -> str:
    return _fernet().decrypt(token_enc.encode()).decode()


class ConnectedTokenStore(TokenStore):
    recovery_hint = "send the owner a new connect link to reconnect Instagram"

    def __init__(self, store: Store, account_id: str, clock: Callable[[], float] = time.time):
        self.store = store
        self.account_id = account_id
        self.clock = clock
        self.token = ""
        self.env_token = ""
        self.env_fingerprint = ""
        self.expires_at = None
        self.obtained_at = clock()
        self.last_error = ""

    def load(self) -> bool:
        saved = self.store.load_token(self.account_id)
        if not saved:
            return False
        token_enc, self.expires_at, self.obtained_at = saved
        self.token = decrypt(token_enc)
        return True

    def save_new(self, token: str, expires_in, app_user_id: str, username: str) -> None:
        now = self.clock()
        self.token = token
        self.obtained_at = now
        self.expires_at = now + expires_in if expires_in else None
        self.last_error = ""
        self.store.save_token(self.account_id, encrypt(token), self.expires_at, now, app_user_id, username)

    def _write_file(self) -> None:
        # TokenStore.apply_refresh persists through here
        self.store.save_token(self.account_id, encrypt(self.token), self.expires_at, self.obtained_at)
