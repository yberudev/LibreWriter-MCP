"""Writer component registry, listeners, snapshots, and guarded mutations."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import unohelper
from com.sun.star.util import XModifyListener
from com.sun.star.view import XSelectionChangeListener
from com.sun.star.lang import XEventListener

from .errors import WriterError
from .events import EventQueue, EventRecord
from .revisions import AnchorRecord, RevisionState, stable_hash

log = logging.getLogger(__name__)


def _supports(obj: Any, service: str) -> bool:
    try:
        return bool(obj.supportsService(service))
    except Exception:
        return False


def _url(obj: Any) -> str:
    try:
        return str(obj.getURL() or "")
    except Exception:
        return ""


def _title(obj: Any) -> str:
    try:
        title = str(obj.getDocumentProperties().Title or "")
        if title.strip():
            return title
    except Exception:
        pass
    try:
        title = str(obj.getTitle() or "")
        if title.strip():
            return title
    except Exception:
        pass
    try:
        title = str(obj.getCurrentController().getFrame().getTitle() or "")
        if title.strip():
            return title
    except Exception:
        pass
    return "Untitled Writer document"


def _readonly(obj: Any) -> bool:
    try:
        return bool(obj.isReadonly())
    except Exception:
        try:
            return bool(obj.isReadOnly())
        except Exception:
            return False


@dataclass
class DocumentEntry:
    document_id: str
    component: Any
    generation: int
    state: RevisionState = field(default_factory=RevisionState)
    listeners: list[tuple[Any, str, Any]] = field(default_factory=list)


class _ModifyListener(unohelper.Base, XModifyListener):
    def __init__(self, events: EventQueue, document_id: str, generation: int):
        self.events, self.document_id, self.generation = events, document_id, generation
    def modified(self, event):
        self.events.enqueue(self.document_id, self.generation, "modified")
    def disposing(self, event):
        self.events.enqueue(self.document_id, self.generation, "disposed")


class _SelectionListener(unohelper.Base, XSelectionChangeListener):
    def __init__(self, events: EventQueue, document_id: str, generation: int):
        self.events, self.document_id, self.generation = events, document_id, generation
    def selectionChanged(self, event):
        self.events.enqueue(self.document_id, self.generation, "selection")
    def disposing(self, event):
        self.events.enqueue(self.document_id, self.generation, "disposed")


class _LifecycleListener(unohelper.Base, XEventListener):
    def __init__(self, events: EventQueue, document_id: str, generation: int):
        self.events, self.document_id, self.generation = events, document_id, generation
    def disposing(self, event):
        self.events.enqueue(self.document_id, self.generation, "disposed")


class DocumentRegistry:
    def __init__(self, events: EventQueue, max_snapshot_chars: int):
        self.events = events
        self.max_snapshot_chars = max_snapshot_chars
        self._lock = threading.RLock()
        self._entries: dict[str, DocumentEntry] = {}
        self._generation = 0

    def reset(self, generation: int):
        with self._lock:
            for entry in self._entries.values():
                self._remove_listeners(entry)
            self._entries.clear()
            self._generation = generation

    def on_event(self, record: EventRecord):
        with self._lock:
            entry = self._entries.get(record.document_id)
            if entry is None or entry.generation != record.generation:
                return
            entry.state.dirty = True
            entry.state.event_sequence = record.sequence
            entry.state.event_count += 1
            if "disposed" in record.kinds:
                # Cleanup is deferred until discover() runs on the serialized UNO worker.
                entry.state.dirty = True

    def discover(self, desktop: Any, generation: int) -> list[DocumentEntry]:
        if generation != self._generation:
            self.reset(generation)
        components = desktop.getComponents().createEnumeration()
        seen: list[Any] = []
        while components.hasMoreElements():
            component = components.nextElement()
            if _supports(component, "com.sun.star.text.TextDocument") and _supports(component, "com.sun.star.text.GenericTextDocument"):
                seen.append(component)
        with self._lock:
            existing = list(self._entries.values())
            found: list[DocumentEntry] = []
            for component in seen:
                entry = next((item for item in existing if self._same_component(item.component, component)), None)
                if entry is None:
                    entry = DocumentEntry(str(uuid.uuid4()), component, generation)
                    self._entries[entry.document_id] = entry
                    self._attach_listeners(entry)
                else:
                    entry.component = component
                found.append(entry)
            active = set(item.document_id for item in found)
            for entry in existing:
                if entry.document_id not in active:
                    self._remove_listeners(entry)
                    self._entries.pop(entry.document_id, None)
            return found

    @staticmethod
    def _same_component(left: Any, right: Any) -> bool:
        try:
            return bool(left == right)
        except Exception:
            return left is right

    def get(self, document_id: str, desktop: Any, generation: int) -> DocumentEntry:
        if not isinstance(document_id, str):
            raise WriterError("DOCUMENT_NOT_FOUND", "Unknown document_id.")
        self.discover(desktop, generation)
        with self._lock:
            entry = self._entries.get(document_id)
        if entry is None or entry.generation != generation:
            raise WriterError("DOCUMENT_NOT_FOUND", "The document_id is stale or does not identify an open Writer document.")
        return entry

    def list(self, desktop: Any, generation: int) -> list[dict[str, Any]]:
        entries = self.discover(desktop, generation)
        result = []
        for entry in entries:
            limited = False
            try:
                snapshot = self.snapshot(entry)
                entry.state.update(snapshot["hash"])
            except WriterError as exc:
                if exc.code != "DOCUMENT_TOO_LARGE":
                    raise
                limited = True
            result.append({"document_id": entry.document_id, "title": _title(entry.component),
                           "modified": bool(entry.component.isModified()), "read_only": _readonly(entry.component),
                           "revision": entry.state.revision, "snapshot_limited": limited})
        return result

    def active_id(self, desktop: Any, generation: int) -> str | None:
        entries = self.discover(desktop, generation)
        try:
            active = desktop.getCurrentComponent()
        except Exception:
            return None
        for entry in entries:
            if self._same_component(entry.component, active):
                return entry.document_id
        return None

    def snapshot(self, entry: DocumentEntry) -> dict[str, Any]:
        text_obj = entry.component.getText()
        enum = text_obj.createEnumeration()
        paragraphs: list[dict[str, Any]] = []
        table_count = 0
        char_count = 0
        while enum.hasMoreElements():
            item = enum.nextElement()
            if _supports(item, "com.sun.star.text.Paragraph"):
                value = str(item.getString())
                try:
                    style = str(item.ParaStyleName)
                except Exception:
                    style = ""
                char_count += len(value)
                paragraphs.append({"text": value, "style": style})
            elif _supports(item, "com.sun.star.text.TextTable"):
                table_count += 1
                # Preserve identity and contents for conflict detection without flattening tables into body text.
                try:
                    names = tuple(item.getCellNames())
                    cells = tuple((name, str(item.getCellByName(name).getString())) for name in names)
                except Exception:
                    cells = ()
                char_count += sum(len(value) for _, value in cells)
                paragraphs.append({"table": table_count, "cells": cells})
            if char_count > self.max_snapshot_chars:
                raise WriterError("DOCUMENT_TOO_LARGE", "The document exceeds the configured snapshot limit; safe writes are disabled.")
        digest = stable_hash(paragraphs)
        return {"paragraphs": paragraphs, "hash": digest, "char_count": char_count, "table_count": table_count}

    def anchor_records(self, entry: DocumentEntry, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        entry.state.anchors.clear()
        paras = [item for item in snapshot["paragraphs"] if "text" in item]
        for visible_index, paragraph in enumerate(paras):
            prior = paras[visible_index - 1]["text"] if visible_index else ""
            following = paras[visible_index + 1]["text"] if visible_index + 1 < len(paras) else ""
            context_hash = stable_hash([prior[-80:], paragraph["text"], following[:80]])
            anchor = "p_" + uuid.uuid4().hex
            record = AnchorRecord(entry.document_id, entry.state.revision, visible_index, paragraph["text"], paragraph["style"], context_hash)
            entry.state.anchors[anchor] = record
            records.append({"anchor": anchor, "paragraph_index": visible_index, "text": paragraph["text"], "style": paragraph["style"], "context_hash": context_hash})
        return records

    def paragraph_cursor(self, entry: DocumentEntry, anchor: str, expected_revision: int, expected_text: str | None = None):
        if expected_revision != entry.state.revision:
            raise WriterError("CONFLICT", "expected_revision does not match the latest known revision.", details={"current_revision": entry.state.revision})
        record = entry.state.anchors.get(anchor)
        if record is None or record.document_id != entry.document_id or record.revision != expected_revision:
            raise WriterError("INVALID_ANCHOR", "The anchor is unknown or no longer valid. Read the structure again.")
        if expected_text is not None and record.text != expected_text:
            raise WriterError("CONFLICT", "expected_text does not match the anchored paragraph.")
        text_obj = entry.component.getText()
        enum = text_obj.createEnumeration()
        paragraph_index = 0
        target = None
        while enum.hasMoreElements():
            item = enum.nextElement()
            if _supports(item, "com.sun.star.text.Paragraph"):
                if paragraph_index == record.paragraph_index:
                    target = item
                    break
                paragraph_index += 1
        if target is None or str(target.getString()) != record.text:
            raise WriterError("CONFLICT", "The anchored paragraph changed or moved.")
        cursor = text_obj.createTextCursorByRange(target.getStart())
        cursor.gotoEndOfParagraph(True)
        return cursor, target, record

    def update_after_mutation(self, entry: DocumentEntry) -> dict[str, Any]:
        snapshot = self.snapshot(entry)
        entry.state.update(snapshot["hash"])
        return snapshot

    def _attach_listeners(self, entry: DocumentEntry):
        component = entry.component
        modify = _ModifyListener(self.events, entry.document_id, entry.generation)
        try:
            component.addModifyListener(modify)
            entry.listeners.append((component, "removeModifyListener", modify))
        except Exception:
            pass
        try:
            controller = component.getCurrentController()
            selection = _SelectionListener(self.events, entry.document_id, entry.generation)
            controller.addSelectionChangeListener(selection)
            entry.listeners.append((controller, "removeSelectionChangeListener", selection))
        except Exception:
            pass
        try:
            lifecycle = _LifecycleListener(self.events, entry.document_id, entry.generation)
            component.addEventListener(lifecycle)
            entry.listeners.append((component, "removeEventListener", lifecycle))
        except Exception:
            pass

    @staticmethod
    def _remove_listeners(entry: DocumentEntry):
        for target, method_name, listener in entry.listeners:
            try:
                getattr(target, method_name)(listener)
            except Exception:
                pass
        entry.listeners.clear()
