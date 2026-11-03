"""Short-lived, one-use tokens for previewed document mutations."""

from __future__ import annotations

import secrets
import threading
import time

from .errors import WriterError
from .revisions import stable_hash


class ConfirmationStore:
    def __init__(self, *, ttl_seconds: float = 120.0, capacity: int = 512):
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float]] = {}

    def issue(self, payload) -> tuple[str, int]:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            while len(self._items) >= self._capacity:
                oldest = min(self._items, key=lambda item: self._items[item][1])
                del self._items[oldest]
            self._items[token] = (stable_hash(payload), now + self._ttl)
        return token, int(self._ttl)

    def consume(self, token: str, payload) -> None:
        if not isinstance(token, str) or not token:
            raise WriterError("CONFIRMATION_REQUIRED", "Preview the exact change and provide its confirmation_token.")
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            item = self._items.pop(token, None)
        if item is None:
            raise WriterError("CONFIRMATION_REQUIRED", "The confirmation token is invalid, expired, or already used.")
        if item[0] != stable_hash(payload):
            raise WriterError("PERMISSION_DENIED", "The confirmation token belongs to a different change.")

    def _purge(self, now: float) -> None:
        expired = [token for token, (_payload_hash, expires_at) in self._items.items() if expires_at <= now]
        for token in expired:
            del self._items[token]
