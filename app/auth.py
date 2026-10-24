"""Single-admin login for the console itself.

The console holds directory and vCenter credentials, so every API call other
than login requires a session cookie. Where the admin password comes from, in
order of precedence:

1. CLOUDTEC_ADMIN_PASSWORD. It can't be changed from the UI while set.
2. <data_dir>/admin_password.hash, written when the password is changed in the UI.
3. <data_dir>/admin_password, a plain-text file. It is generated on first start
   (mode 0600) and can also be edited by hand. Changing the password in the UI
   replaces it with the hash file.
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from .store import _write_private

log = logging.getLogger("cloudtec")

COOKIE = "cloudtec_session"
PLAIN_FILE = "admin_password"
HASH_FILE = "admin_password.hash"
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class PasswordError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.strip().split("$")
        if scheme != "scrypt":
            return False
        attempt = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(attempt, bytes.fromhex(digest))


class AdminPassword:
    def __init__(self, data_dir: Path, configured: str | None):
        self._plain_path = data_dir / PLAIN_FILE
        self._hash_path = data_dir / HASH_FILE
        self._configured = configured
        self._lock = threading.Lock()
        if not configured and not self._hash_path.exists() and not self._read_plain():
            # No usable password yet (missing or blank file): generate one and show it.
            password = secrets.token_urlsafe(12)
            self._plain_path.unlink(missing_ok=True)
            _write_private(self._plain_path, password)
            log.warning("Admin password: %s (saved in %s)", password, self._plain_path)

    def _read_plain(self) -> str:
        try:
            return self._plain_path.read_text().strip()
        except FileNotFoundError:
            return ""

    @property
    def from_env(self) -> bool:
        return self._configured is not None

    def check(self, attempt: str) -> bool:
        if self._configured is not None:
            return hmac.compare_digest(attempt.encode(), self._configured.encode())
        with self._lock:
            if self._hash_path.exists():
                return verify_hash(attempt, self._hash_path.read_text())
            stored = self._read_plain()
        # A blank file must never mean "any empty password signs in".
        return bool(stored) and hmac.compare_digest(attempt.encode(), stored.encode())

    def change(self, current: str, new: str) -> None:
        if self.from_env:
            raise PasswordError(
                "The password is set by CLOUDTEC_ADMIN_PASSWORD. Remove that setting to change it here."
            )
        if not self.check(current):
            raise PasswordError("Current password is incorrect")
        with self._lock:
            tmp = self._hash_path.with_suffix(".tmp")
            tmp.unlink(missing_ok=True)
            _write_private(tmp, hash_password(new))
            os.replace(tmp, self._hash_path)
            self._plain_path.unlink(missing_ok=True)


class Sessions:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.time() + self._ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry < time.time():
                del self._tokens[token]
                return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._tokens.pop(token, None)

    def revoke_others(self, keep: str | None) -> None:
        with self._lock:
            self._tokens = {t: e for t, e in self._tokens.items() if t == keep}
