"""Snapshot fingerprints, opaque anchors, and mutation idempotency."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import WriterError


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass
class RevisionState:
    revision: int = 0
    snapshot_hash: str | None = None
    dirty: bool = True
    event_sequence: int = 0
    event_count: int = 0
    anchors: dict[str, "AnchorRecord"] = field(default_factory=dict)

    def update(self, snapshot_hash: str) -> bool:
        changed = self.snapshot_hash is not None and self.snapshot_hash != snapshot_hash
        if self.snapshot_hash is None:
            self.revision = 1
        elif changed:
            self.revision += 1
            self.anchors.clear()
        self.snapshot_hash = snapshot_hash
        self.dirty = False
        return changed


@dataclass(frozen=True)
class AnchorRecord:
    document_id: str
    revision: int
    paragraph_index: int
    text: str
    style_name: str
    context_hash: str


class IdempotencyLedger:
    """Caches completed operations and prevents retries after unknown outcomes."""

    def __init__(self, capacity: int = 2_000, ttl_seconds: float = 86_400.0):
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, str, Any, float]] = {}

    @staticmethod
    def _validate_id(operation_id: str) -> str:
        try:
            return str(uuid.UUID(operation_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise WriterError("INVALID_ARGUMENT", "operation_id must be a UUID.") from exc

    def execute(self, operation_id: str, payload: Any, action: Callable[[], Any]) -> Any:
        key = self._validate_id(operation_id)
        payload_hash = stable_hash(payload)
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            current = self._items.get(key)
            if current:
                old_hash, status, result, _ = current
                if old_hash != payload_hash:
                    raise WriterError("IDEMPOTENCY_KEY_REUSED", "operation_id was already used for different arguments.")
                if status == "completed":
                    return result
                if status == "failed":
                    raise WriterError(result["code"], result["message"], bool(result.get("retryable", False)), result.get("details", {}))
                raise WriterError("UNKNOWN_OUTCOME", "This operation may already have run. Read the document before retrying.", retryable=False)
            self._items[key] = (payload_hash, "running", None, now)
        try:
            result = action()
        except WriterError as exc:
            with self._lock:
                self._items[key] = (payload_hash, "unknown" if exc.code == "UNKNOWN_OUTCOME" else "failed", exc.as_dict(), time.monotonic())
            raise
        except Exception:
            with self._lock:
                self._items[key] = (payload_hash, "unknown", None, time.monotonic())
            raise
        with self._lock:
            self._items[key] = (payload_hash, "completed", result, time.monotonic())
        return result

    def _purge(self, now: float) -> None:
        expired = [key for key, item in self._items.items() if now - item[3] > self._ttl]
        for key in expired:
            del self._items[key]
        while len(self._items) > self._capacity:
            oldest = min(self._items, key=lambda k: self._items[k][3])
            del self._items[oldest]

