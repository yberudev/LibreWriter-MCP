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
from .revisions import AnchorRecord, RevisionState, TableAnchorRecord, stable_hash

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
    snapshot_cache: dict[str, Any] | None = None
    snapshot_event_version: int = -1


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
                self.events.forget_document(entry.document_id, entry.generation)
            self._entries.clear()
            self._generation = generation

    def on_event(self, record: EventRecord):
        with self._lock:
            entry = self._entries.get(record.document_id)
            if entry is None or entry.generation != record.generation:
                return
            entry.state.event_sequence = record.sequence
            entry.state.event_count += 1
            entry.state.last_event_kinds = record.kinds
            if "modified" in record.kinds or "disposed" in record.kinds:
                entry.state.dirty = True
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
                    self.events.forget_document(entry.document_id, entry.generation)
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

    def snapshot(self, entry: DocumentEntry, *, force: bool = False) -> dict[str, Any]:
        event_version = self.events.document_version(entry.document_id, entry.generation)
        if (not force and entry.snapshot_cache is not None
                and entry.snapshot_event_version == event_version):
            return entry.snapshot_cache
        if force:
            entry.snapshot_cache = None
            entry.snapshot_event_version = -1
        snapshot_event_version = event_version
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
                try:
                    outline_level = int(item.ParaOutlineLevel)
                except Exception:
                    outline_level = 0
                char_count += len(value)
                paragraphs.append({"text": value, "style": style, "outline_level": outline_level})
            elif _supports(item, "com.sun.star.text.TextTable"):
                table_count += 1
                # Preserve identity and contents for conflict detection without flattening tables into body text.
                try:
                    names = tuple(item.getCellNames())
                    cells = tuple((name, str(item.getCellByName(name).getString())) for name in names)
                except Exception:
                    cells = ()
                try:
                    table_name = str(item.getName())
                except Exception:
                    table_name = ""
                if not table_name:
                    try:
                        table_names = entry.component.getTextTables().getElementNames()
                        table_name = str(table_names[table_count - 1])
                    except Exception:
                        table_name = f"Table{table_count}"
                char_count += sum(len(value) for _, value in cells)
                paragraphs.append({"table": table_count, "name": table_name, "cells": cells})
            if char_count > self.max_snapshot_chars:
                raise WriterError("DOCUMENT_TOO_LARGE", "The document exceeds the configured snapshot limit; safe writes are disabled.")
        objects = self._structural_objects(entry.component)
        char_count += sum(len(item.get("text", "")) for item in objects)
        if char_count > self.max_snapshot_chars:
            raise WriterError("DOCUMENT_TOO_LARGE", "The document exceeds the configured snapshot limit; safe writes are disabled.")
        structure = {"paragraphs": paragraphs, "objects": objects}
        digest = stable_hash(structure)
        snapshot = {"paragraphs": paragraphs, "objects": objects, "hash": digest,
                    "char_count": char_count, "table_count": table_count}
        entry.snapshot_cache = snapshot
        entry.snapshot_event_version = snapshot_event_version
        return snapshot

    @staticmethod
    def body_segments(entry: DocumentEntry):
        """Yield body paragraphs and table cells in Writer text order without joining the whole body."""
        enum = entry.component.getText().createEnumeration()
        while enum.hasMoreElements():
            item = enum.nextElement()
            if _supports(item, "com.sun.star.text.Paragraph"):
                yield str(item.getString())
            elif _supports(item, "com.sun.star.text.TextTable"):
                try:
                    cell_names = tuple(item.getCellNames())
                except Exception:
                    cell_names = ()
                for cell_name in cell_names:
                    yield str(item.getCellByName(cell_name).getString())

    @staticmethod
    def _structural_objects(component: Any) -> list[dict[str, Any]]:
        """Capture named Writer objects as separate records instead of flattening them into body text."""
        objects: list[dict[str, Any]] = []

        def add_named_collection(kind: str, getter_name: str, *, include_text: bool = True):
            try:
                collection = getattr(component, getter_name)()
                names = sorted(str(name) for name in collection.getElementNames())
            except Exception:
                return
            for name in names:
                try:
                    item = collection.getByName(name)
                    value = ""
                    if include_text:
                        try:
                            value = str(item.getString())
                        except Exception:
                            try:
                                value = str(item.getText().getString())
                            except Exception:
                                pass
                    row = {"kind": kind, "name": name}
                    if kind == "frame":
                        try:
                            row["anchor_type"] = str(item.AnchorType)
                        except Exception:
                            pass
                    if kind == "section":
                        try:
                            row["protected"] = bool(item.IsProtected)
                        except Exception:
                            pass
                    if include_text:
                        row["text"] = value
                        row["text_hash"] = stable_hash(value)
                    objects.append(row)
                except Exception:
                    continue

        add_named_collection("frame", "getTextFrames")
        add_named_collection("bookmark", "getBookmarks")
        add_named_collection("section", "getTextSections")

        try:
            fields = component.getTextFields().createEnumeration()
            while fields.hasMoreElements():
                field = fields.nextElement()
                try:
                    field_type = str(field.getImplementationName())
                except Exception:
                    field_type = type(field).__name__
                try:
                    value = str(field.getPresentation(True))
                except Exception:
                    value = ""
                objects.append({"kind": "field", "field_type": field_type,
                                "text": value, "text_hash": stable_hash(value)})
        except Exception:
            pass

        for getter_name, kind in (("getFootnotes", "footnote"), ("getEndnotes", "endnote")):
            try:
                notes = getattr(component, getter_name)()
                for index in range(int(notes.getCount())):
                    note = notes.getByIndex(index)
                    value = str(note.getString())
                    objects.append({"kind": kind, "index": index,
                                    "text": value, "text_hash": stable_hash(value)})
            except Exception:
                continue

        try:
            page_styles = component.getStyleFamilies().getByName("PageStyles")
            for style_name in sorted(str(name) for name in page_styles.getElementNames()):
                style = page_styles.getByName(style_name)
                for region, enabled_property, text_property in (
                    ("header", "HeaderIsOn", "HeaderText"),
                    ("footer", "FooterIsOn", "FooterText"),
                ):
                    try:
                        if not bool(style.getPropertyValue(enabled_property)):
                            continue
                        region_text = style.getPropertyValue(text_property)
                        value = str(region_text.getString())
                    except Exception:
                        continue
                    objects.append({"kind": region, "page_style": style_name,
                                    "text": value, "text_hash": stable_hash(value)})
        except Exception:
            pass

        return objects

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
            records.append({"anchor": anchor, "paragraph_index": visible_index, "text": paragraph["text"],
                            "style": paragraph["style"], "outline_level": paragraph.get("outline_level", 0),
                            "context_hash": context_hash})
        return records

    def table_anchor_records(self, entry: DocumentEntry, snapshot: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
        entry.state.tables.clear()
        records = []
        tables = [item for item in snapshot["paragraphs"] if "table" in item]
        for table in tables[:max_items]:
            anchor = "t_" + uuid.uuid4().hex
            names = tuple(name for name, _value in table["cells"])
            entry.state.tables[anchor] = TableAnchorRecord(
                document_id=entry.document_id,
                revision=entry.state.revision,
                table_name=table["name"],
                cell_names=names,
            )
            records.append({"table_anchor": anchor, "reference": f"table:{table['table']}",
                            "cell_count": len(names), "cells": list(names[:max_items]),
                            "cells_truncated": len(names) > max_items})
        return records

    def resolve_table(self, entry: DocumentEntry, table_anchor: str, expected_revision: int):
        record = entry.state.tables.get(table_anchor)
        if (record is None or record.document_id != entry.document_id
                or record.revision != expected_revision or expected_revision != entry.state.revision):
            raise WriterError("INVALID_ANCHOR", "The table anchor is unknown or stale. Read the structure again.")
        try:
            return entry.component.getTextTables().getByName(record.table_name), record
        except Exception as exc:
            raise WriterError("INVALID_ANCHOR", "The anchored table no longer exists.") from exc

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
        snapshot = self.snapshot(entry, force=True)
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
