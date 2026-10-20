"""Application service coordinating UNO, revisions, and safe document operations."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .documents import DocumentRegistry
from .errors import WriterError
from .events import EventQueue
from .revisions import IdempotencyLedger, stable_hash
from .uno_worker import UnoWorker

log = logging.getLogger(__name__)


class WriterService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.events = EventQueue(self._on_event)
        self.registry = DocumentRegistry(self.events, self.settings.max_snapshot_chars)
        self.ledger = IdempotencyLedger()
        self.worker = UnoWorker(self.settings, on_disconnect=self._on_disconnect)

    def _on_event(self, record):
        self.registry.on_event(record)

    def _on_disconnect(self):
        self.registry.reset(self.worker.generation + 1)

    def _run(self, fn, *, mutating=False):
        return self.worker.call(lambda context, desktop: fn(context, desktop), mutating=mutating)

    def _entry(self, desktop, document_id):
        return self.registry.get(document_id, desktop, self.worker.generation)

    def _refresh_before_write(self, entry, expected_revision):
        current = self.registry.snapshot(entry)
        entry.state.update(current["hash"])
        if expected_revision != entry.state.revision:
            raise WriterError("CONFLICT", "The document changed since it was read.", details={"expected_revision": expected_revision, "current_revision": entry.state.revision})
        if self._readonly(entry.component):
            raise WriterError("PERMISSION_DENIED", "The document is read-only.")
        return current

    @staticmethod
    def _readonly(component):
        try:
            return bool(component.isReadonly())
        except Exception:
            try:
                return bool(component.isReadOnly())
            except Exception:
                return False

    def get_state(self):
        def op(context, desktop):
            documents = self.registry.list(desktop, self.worker.generation)
            active_id = self.registry.active_id(desktop, self.worker.generation)
            version = self._office_version(context)
            return {"connected": True, "host": self.settings.host, "port": self.settings.port,
                    "session_generation": self.worker.generation, "libreoffice_version": version,
                    "active_document_id": active_id, "writer_document_count": len(documents),
                    "capabilities": {"read_document": True, "read_selection": True, "read_structure": True,
                                     "insert_text": True, "replace_paragraph": True, "apply_paragraph_style": True,
                                     "save_existing_document": bool(self.settings.allowed_save_roots),
                                     "events_internal": True, "mcp_subscriptions": False},
                    "events": self.events.stats()}
        try:
            return self._run(op)
        except WriterError as exc:
            if exc.code != "NOT_CONNECTED":
                raise
            return {"connected": False, "host": self.settings.host, "port": self.settings.port,
                    "session_generation": self.worker.generation, "libreoffice_version": None,
                    "active_document_id": None, "writer_document_count": 0,
                    "capabilities": {"read_document": False, "read_selection": False, "read_structure": False,
                                     "insert_text": False, "replace_paragraph": False, "apply_paragraph_style": False,
                                     "save_existing_document": False, "events_internal": True, "mcp_subscriptions": False},
                    "events": self.events.stats()}

    @staticmethod
    def _office_version(context):
        try:
            import uno
            provider = context.ServiceManager.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", context)
            node = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            node.Name, node.Value = "nodepath", "/org.openoffice.Setup/Product"
            access = provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (node,))
            return str(access.getPropertyValue("ooSetupVersionAboutBox"))
        except Exception:
            return None

    def list_documents(self):
        return self._run(lambda _ctx, desktop: self.registry.list(desktop, self.worker.generation))

    def read_document(self, document_id, offset=0, limit=None):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WriterError("INVALID_ARGUMENT", "offset must be a non-negative integer.")
        limit = self.settings.max_read_chars if limit is None else limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.settings.max_read_chars:
            raise WriterError("INVALID_ARGUMENT", f"limit must be between 1 and {self.settings.max_read_chars}.")
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            whole = str(entry.component.getText().getString())
            snapshot_hash = entry.state.snapshot_hash
            snapshot_limited = len(whole) > self.settings.max_snapshot_chars
            if not snapshot_limited:
                snapshot = self.registry.snapshot(entry)
                entry.state.update(snapshot["hash"])
                snapshot_hash = snapshot["hash"]
            chunk = whole[offset:offset + limit]
            return {"document_id": entry.document_id, "revision": entry.state.revision,
                    "snapshot_hash": snapshot_hash, "snapshot_limited": snapshot_limited,
                    "content_ref": stable_hash([entry.document_id, entry.state.revision, offset, len(chunk), chunk]),
                    "offset": offset, "limit": limit, "total_chars": len(whole), "truncated": offset + len(chunk) < len(whole),
                    "text": chunk}
        return self._run(op)

    def read_structure(self, document_id):
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot = self.registry.snapshot(entry)
            entry.state.update(snapshot["hash"])
            anchors = self.registry.anchor_records(entry, snapshot)
            paragraphs = []
            for item in anchors:
                para = self._paragraph_at(entry.component, item["paragraph_index"])
                try:
                    level = int(para.ParaOutlineLevel)
                except Exception:
                    level = 0
                is_heading = level > 0 or item["style"].lower().startswith(("heading", "titulo", "título"))
                full_text = item.pop("text")
                preview = full_text[:500]
                paragraphs.append({**item, "text": preview, "text_truncated": len(full_text) > len(preview),
                                   "expected_hash": stable_hash(full_text), "kind": "heading" if is_heading else "paragraph",
                                   "heading_level": level or None})
            tables = [{"reference": f"table:{index + 1}", "cell_count": len(item.get("cells", ())),
                       "cells": [name for name, _value in item.get("cells", ())]}
                      for index, item in enumerate(x for x in snapshot["paragraphs"] if "table" in x)]
            return {"document_id": entry.document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                    "structure_ref": stable_hash([entry.document_id, entry.state.revision, snapshot["hash"]]),
                    "paragraphs": paragraphs[:self.settings.max_structure_items], "tables": tables,
                    "truncated": len(paragraphs) > self.settings.max_structure_items}
        return self._run(op)

    @staticmethod
    def _paragraph_at(component, index):
        enum = component.getText().createEnumeration()
        seen = 0
        while enum.hasMoreElements():
            item = enum.nextElement()
            try:
                if item.supportsService("com.sun.star.text.Paragraph"):
                    if seen == index:
                        return item
                    seen += 1
            except Exception:
                pass
        raise WriterError("INVALID_ANCHOR", "The paragraph anchor no longer exists.")

    def read_selection(self, document_id=None):
        def op(_ctx, desktop):
            entries = self.registry.discover(desktop, self.worker.generation)
            if document_id:
                entry = self._entry(desktop, document_id)
            else:
                active_id = self.registry.active_id(desktop, self.worker.generation)
                entry = next((item for item in entries if item.document_id == active_id), None)
            if entry is None:
                raise WriterError("DOCUMENT_NOT_FOUND", "No active Writer document is available.")
            selection = entry.component.getCurrentController().getSelection()
            if selection is None:
                return {"document_id": entry.document_id, "kind": "none", "text": "", "range_count": 0}
            ranges = self._selection_ranges(selection)
            values = []
            nontext = []
            for item in ranges:
                try:
                    if hasattr(item, "getString") and hasattr(item, "getStart") and hasattr(item, "getEnd"):
                        values.append(str(item.getString()))
                    else:
                        try:
                            nontext.append(str(item.getImplementationName()))
                        except Exception:
                            nontext.append(type(item).__name__)
                except Exception:
                    nontext.append("unknown")
            if values and not nontext:
                if len(values) == 1 and values[0] == "":
                    return {"document_id": entry.document_id, "kind": "none", "text": "", "range_count": 0}
                total = sum(map(len, values))
                remaining = self.settings.max_read_chars
                bounded = []
                for value in values:
                    bounded.append(value[:remaining])
                    remaining = max(0, remaining - len(value))
                kind = "text" if len(values) == 1 else "text_ranges"
                return {"document_id": entry.document_id, "kind": kind, "text": bounded[0] if len(bounded) == 1 else bounded,
                        "range_count": len(values), "total_chars": total, "truncated": total > self.settings.max_read_chars}
            if nontext and not values:
                return {"document_id": entry.document_id, "kind": "non_text", "text": None,
                        "range_count": len(nontext), "selection_types": nontext}
            if values or nontext:
                return {"document_id": entry.document_id, "kind": "mixed", "text_ranges": values,
                        "selection_types": nontext, "range_count": len(values) + len(nontext)}
            return {"document_id": entry.document_id, "kind": "none", "text": "", "range_count": 0}
        return self._run(op)

    @staticmethod
    def _selection_ranges(selection):
        try:
            if hasattr(selection, "getString"):
                return [selection]
        except Exception:
            pass
        try:
            count = selection.getCount()
            return [selection.getByIndex(i) for i in range(count)]
        except Exception:
            pass
        try:
            if isinstance(selection, (tuple, list)):
                return list(selection)
        except Exception:
            pass
        return [selection]

    def insert_text(self, document_id, anchor, expected_revision, text, position="end", offset=None, operation_id=""):
        self._validate_revision(expected_revision)
        self._validate_anchor(anchor)
        self._validate_text(text)
        self._validate_inline_text(text)
        if not isinstance(position, str) or position not in {"start", "end", "offset"}:
            raise WriterError("INVALID_ARGUMENT", "position must be start, end, or offset.")
        if position == "offset" and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise WriterError("INVALID_ARGUMENT", "offset is required and must be a non-negative code-point index for position=offset.")
        payload = {"tool": "insert", "document_id": document_id, "anchor": anchor, "expected_revision": expected_revision,
                   "text": text, "position": position, "offset": offset}
        return self.ledger.execute(operation_id, payload, lambda: self._run(
            lambda _ctx, desktop: self._insert_text(desktop, document_id, anchor, expected_revision, text, position, offset), mutating=True))

    def _insert_text(self, desktop, document_id, anchor, expected_revision, value, position, offset):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        cursor, _target, record = self.registry.paragraph_cursor(entry, anchor, expected_revision, record_text(entry, anchor))
        if position == "start":
            cursor.gotoStartOfParagraph(False)
        elif position == "end":
            cursor.gotoEndOfParagraph(False)
        else:
            if offset > len(record.text):
                raise WriterError("INVALID_ANCHOR", "offset lies beyond the anchored paragraph.")
            cursor.gotoStartOfParagraph(False)
            cursor.goRight(self._utf16_units(record.text[:offset]), False)
        entry.component.getText().insertString(cursor, value, False)
        snapshot = self.registry.update_after_mutation(entry)
        if position == "start":
            expected_result = value + record.text
        elif position == "end":
            expected_result = record.text + value
        else:
            expected_result = record.text[:offset] + value + record.text[offset:]
        confirmed = self._paragraph_at(entry.component, record.paragraph_index)
        if str(confirmed.getString()) != expected_result:
            raise WriterError("UNKNOWN_OUTCOME", "The insertion result could not be verified; inspect the document before retrying.", retryable=False)
        return {"document_id": document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                "inserted_chars": len(value), "confirmed": True}

    def replace_range(self, document_id, anchor, expected_revision, new_content, expected_text=None,
                      expected_hash=None, operation_id=""):
        self._validate_revision(expected_revision)
        self._validate_anchor(anchor)
        self._validate_text(new_content)
        self._validate_inline_text(new_content)
        if (expected_text is None) == (expected_hash is None):
            raise WriterError("INVALID_ARGUMENT", "Provide exactly one of expected_text or expected_hash.")
        if expected_text is not None:
            self._validate_text(expected_text)
        if expected_hash is not None and (not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:")):
            raise WriterError("INVALID_ARGUMENT", "expected_hash must be a sha256 snapshot hash.")
        payload = {"tool": "replace", "document_id": document_id, "anchor": anchor, "expected_revision": expected_revision,
                   "expected_text": expected_text, "expected_hash": expected_hash, "new_content": new_content}
        return self.ledger.execute(operation_id, payload, lambda: self._run(
            lambda _ctx, desktop: self._replace_range(desktop, document_id, anchor, expected_revision, expected_text, expected_hash, new_content), mutating=True))

    def _replace_range(self, desktop, document_id, anchor, expected_revision, expected_text, expected_hash, new_content):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        record = entry.state.anchors.get(anchor)
        if record is None:
            raise WriterError("INVALID_ANCHOR", "Read the document structure to obtain a current paragraph anchor.")
        if expected_text is not None and expected_text != record.text:
            raise WriterError("CONFLICT", "expected_text does not match the anchored paragraph.")
        if expected_hash is not None and expected_hash != stable_hash(record.text):
            raise WriterError("CONFLICT", "expected_hash does not match the anchored paragraph.")
        cursor, _target, _record = self.registry.paragraph_cursor(entry, anchor, expected_revision, record.text)
        cursor.setString(new_content)
        snapshot = self.registry.update_after_mutation(entry)
        confirmed = self._paragraph_at(entry.component, record.paragraph_index)
        if str(confirmed.getString()) != new_content:
            raise WriterError("UNKNOWN_OUTCOME", "The replacement result could not be verified; inspect the document before retrying.", retryable=False)
        try:
            if str(confirmed.ParaStyleName) != record.style_name:
                raise WriterError("UNKNOWN_OUTCOME", "The replacement did not preserve the paragraph style; inspect the document.", retryable=False)
        except WriterError:
            raise
        except Exception:
            pass
        return {"document_id": document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                "replaced_chars": len(record.text), "new_chars": len(new_content), "confirmed": True}

    def apply_style(self, document_id, anchor, expected_revision, expected_text, style_name, operation_id=""):
        self._validate_revision(expected_revision)
        self._validate_anchor(anchor)
        self._validate_text(expected_text)
        self._validate_text(style_name)
        self._validate_inline_text(style_name)
        payload = {"tool": "style", "document_id": document_id, "anchor": anchor, "expected_revision": expected_revision,
                   "expected_text": expected_text, "style_name": style_name}
        return self.ledger.execute(operation_id, payload, lambda: self._run(
            lambda _ctx, desktop: self._apply_style(desktop, document_id, anchor, expected_revision, expected_text, style_name), mutating=True))

    def _apply_style(self, desktop, document_id, anchor, expected_revision, expected_text, style_name):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        try:
            styles = entry.component.getStyleFamilies().getByName("ParagraphStyles")
            if not styles.hasByName(style_name):
                raise WriterError("INVALID_STYLE", "The requested paragraph style does not exist.")
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("INVALID_STYLE", "Could not validate the paragraph style name.") from exc
        cursor, _target, record = self.registry.paragraph_cursor(entry, anchor, expected_revision, expected_text)
        cursor.ParaStyleName = style_name
        snapshot = self.registry.update_after_mutation(entry)
        confirmed = self._paragraph_at(entry.component, record.paragraph_index)
        if str(confirmed.getString()) != expected_text or str(confirmed.ParaStyleName) != style_name:
            raise WriterError("UNKNOWN_OUTCOME", "The style result could not be verified; inspect the document.", retryable=False)
        return {"document_id": document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                "style_name": style_name, "confirmed": True}

    def save_document(self, document_id, operation_id=""):
        payload = {"tool": "save", "document_id": document_id}
        return self.ledger.execute(operation_id, payload, lambda: self._run(
            lambda _ctx, desktop: self._save_document(desktop, document_id), mutating=True))

    def _save_document(self, desktop, document_id):
        entry = self._entry(desktop, document_id)
        component = entry.component
        if self._readonly(component):
            raise WriterError("PERMISSION_DENIED", "The document is read-only.")
        try:
            url = str(component.getURL() or "")
            if not url.startswith("file:"):
                raise WriterError("PERMISSION_DENIED", "Save is limited to existing local file documents; Save As is not supported.")
            import uno
            path = Path(uno.fileUrlToSystemPath(url)).resolve(strict=True)
            if not self._path_allowed(path):
                raise WriterError("PERMISSION_DENIED", "The document path is outside YBERU_WRITER_ALLOWED_SAVE_ROOTS.")
            if not bool(component.isModified()):
                return {"document_id": document_id, "saved": False, "reason": "not_modified"}
            component.store()
            snapshot = self.registry.update_after_mutation(entry)
            return {"document_id": document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"], "saved": True}
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "Saving failed or its result is unknown; inspect the file before retrying.", retryable=False,
                              details={"exception": type(exc).__name__}) from exc

    def _path_allowed(self, path: Path):
        for root in self.settings.allowed_save_roots:
            try:
                path.relative_to(root.resolve(strict=True))
                return True
            except (ValueError, OSError):
                pass
        return False

    def _validate_text(self, value):
        if not isinstance(value, str):
            raise WriterError("INVALID_ARGUMENT", "Text parameters must be strings.")
        if len(value) > self.settings.max_text_chars:
            raise WriterError("SIZE_LIMIT", f"Text exceeds the {self.settings.max_text_chars} character limit.")
        if "\x00" in value:
            raise WriterError("INVALID_ARGUMENT", "Text must not contain NUL characters.")

    @staticmethod
    def _validate_revision(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise WriterError("INVALID_ARGUMENT", "expected_revision must be a positive integer.")

    @staticmethod
    def _validate_anchor(value):
        if not isinstance(value, str) or not value.startswith("p_") or len(value) != 34:
            raise WriterError("INVALID_ANCHOR", "anchor must be an opaque paragraph anchor returned by writer_read_structure.")

    @staticmethod
    def _validate_inline_text(value):
        if "\n" in value or "\r" in value:
            raise WriterError("INVALID_ARGUMENT", "This operation accepts one paragraph of text and does not insert paragraph breaks.")

    @staticmethod
    def _utf16_units(value):
        return len(value.encode("utf-16-le")) // 2

    def close(self):
        try:
            self.worker.call_if_connected(lambda _ctx, _desktop: self.registry.reset(self.worker.generation), timeout=3)
        except Exception:
            pass
        self.events.close()
        self.worker.close()


def record_text(entry, anchor):
    record = entry.state.anchors.get(anchor)
    if record is None:
        raise WriterError("INVALID_ANCHOR", "Read the document structure to obtain a current paragraph anchor.")
    return record.text
