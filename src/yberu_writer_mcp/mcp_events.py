"""Bridge coalesced UNO notifications to the MCP subscription bus."""

from __future__ import annotations

import asyncio
import logging
import threading

from mcp.server.subscriptions import SubscriptionBus
from mcp.shared.subscriptions import ResourceUpdated

from .events import EventRecord

log = logging.getLogger(__name__)


def event_resource_uri(document_id: str) -> str:
    return f"yberu-writer://documents/{document_id}/events"


class MCPEventPublisher:
    """Coalesce thread-originated event records and publish on the MCP event loop."""

    def __init__(self, bus: SubscriptionBus):
        self._bus = bus
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, EventRecord] = {}
        self._drain_task: asyncio.Task[None] | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            self._loop = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._clear_pending)

    def publish_from_thread(self, record: EventRecord) -> None:
        with self._lock:
            loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, record)
        except RuntimeError:
            log.debug("dropping MCP event after event loop shutdown")

    def _enqueue(self, record: EventRecord) -> None:
        self._pending[record.document_id] = record
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain(), name="yberu-mcp-event-publisher")

    async def _drain(self) -> None:
        try:
            while self._pending:
                pending, self._pending = self._pending, {}
                for document_id in pending:
                    try:
                        await self._bus.publish(ResourceUpdated(uri=event_resource_uri(document_id)))
                    except Exception:
                        log.exception("Could not publish MCP document event")
        finally:
            self._drain_task = None
            if self._pending:
                self._drain_task = asyncio.create_task(self._drain(), name="yberu-mcp-event-publisher")

    def _clear_pending(self) -> None:
        self._pending.clear()
