"""Non-blocking UNO callbacks and a coalescing event queue."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    document_id: str
    generation: int
    kinds: tuple[str, ...]
    timestamp: float


class EventQueue:
    """Callbacks enqueue small records; this thread coalesces without UNO calls."""

    def __init__(self, sink: Callable[[EventRecord], None], debounce_seconds: float = 0.06):
        self._sink = sink
        self._debounce = debounce_seconds
        self._queue: queue.Queue[tuple[str, int, str, float]] = queue.Queue(maxsize=10_000)
        self._lock = threading.Lock()
        self._sequence = 0
        self._dropped = 0
        self._versions: dict[tuple[str, int], int] = {}
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="yberu-event-coalescer", daemon=True)
        self._thread.start()

    def enqueue(self, document_id: str, generation: int, kind: str) -> None:
        if kind in {"modified", "disposed"}:
            with self._lock:
                key = (document_id, generation)
                self._versions[key] = self._versions.get(key, 0) + 1
        try:
            self._queue.put_nowait((document_id, generation, kind, time.monotonic()))
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def document_version(self, document_id: str, generation: int) -> int:
        """Return an immediate invalidation counter, including events still in debounce."""
        with self._lock:
            return self._versions.get((document_id, generation), 0)

    def forget_document(self, document_id: str, generation: int) -> None:
        with self._lock:
            self._versions.pop((document_id, generation), None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"sequence": self._sequence, "dropped": self._dropped}

    def close(self) -> None:
        self._closed.set()
        try:
            self._queue.put_nowait(("", -1, "__stop__", time.monotonic()))
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                first = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if first[2] == "__stop__":
                break
            records = [first]
            deadline = time.monotonic() + self._debounce
            while not self._closed.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item[2] == "__stop__":
                    self._closed.set()
                    break
                records.append(item)
                deadline = time.monotonic() + self._debounce
            grouped: dict[tuple[str, int], set[str]] = {}
            timestamps: dict[tuple[str, int], float] = {}
            for document_id, generation, kind, timestamp in records:
                key = (document_id, generation)
                grouped.setdefault(key, set()).add(kind)
                timestamps[key] = max(timestamp, timestamps.get(key, 0.0))
            for (document_id, generation), kinds in grouped.items():
                with self._lock:
                    self._sequence += 1
                    sequence = self._sequence
                try:
                    self._sink(EventRecord(sequence, document_id, generation, tuple(sorted(kinds)), timestamps[(document_id, generation)]))
                except Exception:
                    log.exception("Event sink failed for document notification")

