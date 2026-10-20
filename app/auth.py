"""Single-admin login for the console itself.

The console holds directory and vCenter credentials, so every API call other
than login requires a session cookie. The admin password comes from
CLOUDTEC_ADMIN_PASSWORD, or is generated on first start and written to
<data_dir>/admin_password (mode 0600).
"""

import hmac
import logging
import secrets
import threading
import time
from pathlib import Path

from .store import _write_private

log = logging.getLogger("cloudtec")

COOKIE = "cloudtec_session"


def load_or_create_password(data_dir: Path, configured: str | None) -> str:
    if configured:
        return configured
    path = data_dir / "admin_password"
    if not path.exists():
        _write_private(path, secrets.token_urlsafe(12))
        log.warning("Generated admin password, stored in %s", path)
    return path.read_text().strip()


class Sessions:
    def __init__(self, password: str, ttl_seconds: int):
        self._password = password.encode()
        self._ttl = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_password(self, attempt: str) -> bool:
        return hmac.compare_digest(attempt.encode(), self._password)

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
