"""Application service coordinating UNO, revisions, and safe document operations."""

from __future__ import annotations

import copy
import hashlib
import logging
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Settings
from .confirmations import ConfirmationStore
from .documents import DocumentRegistry
from .errors import WriterError
from .events import EventQueue
from .mcp_events import event_resource_uri
from .revisions import IdempotencyLedger, TextRangeRecord, UndoRecord, stable_hash
from .uno_worker import UnoWorker

log = logging.getLogger(__name__)


class WriterService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.events = EventQueue(self._on_event)
        self.registry = DocumentRegistry(self.events, self.settings.max_snapshot_chars)
        self.ledger = IdempotencyLedger()
        self.confirmations = ConfirmationStore()
        self._undo_records: dict[str, UndoRecord] = {}
        self.worker = UnoWorker(self.settings, on_disconnect=self._on_disconnect)
        self._event_publisher = None

    def set_event_publisher(self, publisher):
        self._event_publisher = publisher

    def _on_event(self, record):
        self.registry.on_event(record)
        if self._event_publisher is not None:
            self._event_publisher.publish_from_thread(record)

    def _on_disconnect(self):
        self.registry.reset(self.worker.generation + 1)
        self._undo_records.clear()

    def _run(self, fn, *, mutating=False):
        return self.worker.call(lambda context, desktop: fn(context, desktop), mutating=mutating)

    def _entry(self, desktop, document_id):
        return self.registry.get(document_id, desktop, self.worker.generation)

    def _refresh_before_write(self, entry, expected_revision):
        current = self.registry.snapshot(entry, force=True)
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
                                     "insert_text": True, "replace_range": True, "apply_paragraph_style": True,
                                     "apply_formatting": True,
                                     "save_existing_document": bool(self.settings.allowed_save_roots),
                                     "save_as_document": bool(self.settings.allowed_save_roots),
                                     "read_text_range": True, "read_table": True, "replace_table_cell": True,
                                     "edit_table_structure": True, "format_table_cell": True,
                                     "create_document": True, "insert_table": True,
                                     "compose_document": True,
                                     "create_from_template": bool(self.settings.allowed_template_roots),
                                     "insert_image": bool(self.settings.allowed_image_roots),
                                     "update_image": True,
                                     "events_internal": True, "mcp_subscriptions": True,
                                     "event_resources": True, "preview_changes": True,
                                     "confirmed_writes": True, "undo_latest_mcp_change": True},
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
                                     "insert_text": False, "replace_range": False, "apply_paragraph_style": False,
                                     "apply_formatting": False,
                                     "save_existing_document": False, "read_text_range": False, "read_table": False,
                                     "save_as_document": False,
                                     "replace_table_cell": False, "edit_table_structure": False,
                                     "format_table_cell": False, "create_document": False,
                                     "compose_document": False,
                                     "create_from_template": False,
                                     "insert_table": False, "insert_image": False, "events_internal": True,
                                     "update_image": False,
                                     "mcp_subscriptions": True, "event_resources": True,
                                     "preview_changes": True, "confirmed_writes": True,
                                     "undo_latest_mcp_change": True},
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
        documents = self._run(lambda _ctx, desktop: self.registry.list(desktop, self.worker.generation))
        for item in documents:
            item["event_uri"] = event_resource_uri(item["document_id"])
        return documents

    def read_event_snapshot(self, document_id):
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot_limited = False
            try:
                snapshot = self.registry.snapshot(entry)
                entry.state.update(snapshot["hash"])
                snapshot_hash = snapshot["hash"]
            except WriterError as exc:
                if exc.code != "DOCUMENT_TOO_LARGE":
                    raise
                snapshot_limited = True
                snapshot_hash = None
            return {"document_id": entry.document_id, "revision": entry.state.revision,
                    "snapshot_hash": snapshot_hash, "snapshot_limited": snapshot_limited,
                    "dirty": entry.state.dirty, "event_sequence": entry.state.event_sequence,
                    "event_count": entry.state.event_count,
                    "event_kinds": list(entry.state.last_event_kinds),
                    "session_generation": entry.generation}
        return self._run(op)

    def read_document(self, document_id, offset=0, limit=None):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WriterError("INVALID_ARGUMENT", "offset must be a non-negative integer.")
        limit = self.settings.max_read_chars if limit is None else limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.settings.max_read_chars:
            raise WriterError("INVALID_ARGUMENT", f"limit must be between 1 and {self.settings.max_read_chars}.")
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot = None
            try:
                snapshot = self.registry.snapshot(entry)
            except WriterError as exc:
                if exc.code != "DOCUMENT_TOO_LARGE":
                    raise
            if snapshot is not None:
                entry.state.update(snapshot["hash"])
                segments = self._snapshot_body_segments(snapshot)
                snapshot_hash = snapshot["hash"]
            else:
                # Large documents remain readable in bounded slices, but safe writes stay disabled.
                segments = self.registry.body_segments(entry)
                snapshot_hash = None
            chunk, total_chars = self._slice_segments(segments, offset, limit)
            snapshot_limited = snapshot is None
            return {"document_id": entry.document_id, "revision": entry.state.revision,
                    "snapshot_hash": snapshot_hash, "snapshot_limited": snapshot_limited,
                    "content_ref": stable_hash([entry.document_id, entry.state.revision, offset, len(chunk), chunk]),
                    "offset": offset, "limit": limit, "total_chars": total_chars,
                    "truncated": offset + len(chunk) < total_chars,
                    "text": chunk}
        return self._run(op)

    @staticmethod
    def _snapshot_body_segments(snapshot):
        for item in snapshot["paragraphs"]:
            if "text" in item:
                yield item["text"]
            elif "table" in item:
                for _cell_name, value in item["cells"]:
                    yield value

    @staticmethod
    def _slice_segments(segments, offset, limit):
        selected = []
        position = 0
        chunk_end = offset + limit
        has_previous = False
        for segment in segments:
            if has_previous:
                if offset <= position < chunk_end:
                    selected.append("\n")
                position += 1
            start = position
            end = start + len(segment)
            overlap_start = max(offset, start)
            overlap_end = min(chunk_end, end)
            if overlap_start < overlap_end:
                selected.append(segment[overlap_start - start:overlap_end - start])
            position = end
            has_previous = True
        return "".join(selected), position

    def read_structure(self, document_id):
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot = self.registry.snapshot(entry)
            entry.state.update(snapshot["hash"])
            anchors = self.registry.anchor_records(entry, snapshot)
            paragraphs = []
            for item in anchors:
                level = item["outline_level"]
                is_heading = level > 0 or item["style"].lower().startswith(("heading", "titulo", "título"))
                full_text = item.pop("text")
                preview = full_text[:500]
                paragraphs.append({**item, "text": preview, "text_truncated": len(full_text) > len(preview),
                                   "expected_hash": stable_hash(full_text), "kind": "heading" if is_heading else "paragraph",
                                   "heading_level": level or None})
            table_count = sum(1 for item in snapshot["paragraphs"] if "table" in item)
            tables = self.registry.table_anchor_records(entry, snapshot, self.settings.max_structure_items)
            object_records = []
            for index, item in enumerate(snapshot["objects"][:self.settings.max_structure_items]):
                record = dict(item)
                full_text = str(record.pop("text", ""))
                record.update({"object_ref": stable_hash([entry.document_id, entry.state.revision, index, item]),
                               "text": full_text[:500], "text_truncated": len(full_text) > 500})
                object_records.append(record)
            return {"document_id": entry.document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                    "structure_ref": stable_hash([entry.document_id, entry.state.revision, snapshot["hash"]]),
                    "paragraphs": paragraphs[:self.settings.max_structure_items], "tables": tables,
                    "objects": object_records, "object_count": len(snapshot["objects"]),
                    "objects_truncated": len(object_records) < len(snapshot["objects"]),
                    "tables_truncated": table_count > len(tables),
                    "truncated": len(paragraphs) > self.settings.max_structure_items
                    or table_count > len(tables) or len(object_records) < len(snapshot["objects"])}
        return self._run(op)

    def preview_create_document(self, title, initial_paragraphs=None):
        title, paragraphs = self._normalize_document_spec(title, initial_paragraphs)
        payload = {"tool": "create_document", "title": title, "initial_paragraphs": paragraphs}
        self._run(lambda _ctx, _desktop: True)
        token, ttl = self.confirmations.issue(payload)
        return {"preview_only": True, "title": title, "paragraph_count": len(paragraphs),
                "paragraphs_preview": [self._preview_text(value) for value in paragraphs[:20]],
                "paragraphs_truncated": len(paragraphs) > 20,
                "content_hash": stable_hash(paragraphs), "confirmation_token": token,
                "expires_in_seconds": ttl}

    def create_document(self, title, initial_paragraphs, operation_id, confirmation_token):
        title, paragraphs = self._normalize_document_spec(title, initial_paragraphs)
        payload = {"tool": "create_document", "title": title, "initial_paragraphs": paragraphs}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda context, desktop: self._create_document(context, desktop, title, paragraphs),
                mutating=True)))

    def _create_document(self, context, desktop, title, paragraphs):
        try:
            component = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "Writer may have created the document; inspect the open windows before retrying.", retryable=False) from exc
        if component is None:
            raise WriterError("UNKNOWN_OUTCOME", "Writer returned no document after the create request.", retryable=False)
        try:
            component.getDocumentProperties().Title = title
            text_obj = component.getText()
            if paragraphs:
                text_obj.setString(paragraphs[0])
                cursor = text_obj.createTextCursor()
                cursor.gotoEnd(False)
                import uno
                paragraph_break = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
                for paragraph in paragraphs[1:]:
                    text_obj.insertControlCharacter(cursor, paragraph_break, False)
                    text_obj.insertString(cursor, paragraph, False)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The document was created but its initial content could not be verified; inspect the new window.", retryable=False) from exc
        entries = self.registry.discover(desktop, self.worker.generation)
        entry = next((candidate for candidate in entries
                      if self.registry._same_component(candidate.component, component)), None)
        if entry is None:
            raise WriterError("UNKNOWN_OUTCOME", "Writer created the document but the MCP registry could not identify it.", retryable=False)
        snapshot = self.registry.snapshot(entry, force=True)
        entry.state.update(snapshot["hash"])
        return {"document_id": entry.document_id, "title": title, "revision": entry.state.revision,
                "snapshot_hash": snapshot["hash"],
                "paragraph_count": sum(1 for item in snapshot["paragraphs"] if "text" in item),
                "modified": bool(component.isModified()), "confirmed": True}

    def preview_compose_document(self, title, blocks):
        title, normalized = self._normalize_compose_spec(title, blocks)
        payload = {"tool": "compose_document", "title": title, "blocks": normalized}
        self._run(lambda _ctx, _desktop: True)
        token, ttl = self.confirmations.issue(payload)
        previews = []
        for block in normalized:
            if block["type"] in {"paragraph", "heading"}:
                previews.append({"type": block["type"], "text": self._preview_text(block["text"]),
                                 **({"level": block["level"]} if block["type"] == "heading" else {})})
            elif block["type"] == "table":
                previews.append({"type": "table", "rows": block["rows"], "columns": block["columns"],
                                 "cells": sum(len(row) for row in block["cell_values"])})
            else:
                previews.append({"type": "image", "image_name": Path(block["image_path"]).name,
                                 "image_hash": block["image_hash"], "alt_text": block["alt_text"]})
        return {"preview_only": True, "title": title, "block_count": len(normalized),
                "blocks_preview": previews, "confirmation_token": token,
                "expires_in_seconds": ttl, "payload_hash": stable_hash(payload)}

    def compose_document(self, title, blocks, operation_id, confirmation_token):
        title, normalized = self._normalize_compose_spec(title, blocks)
        payload = {"tool": "compose_document", "title": title, "blocks": normalized}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda context, desktop: self._compose_document(context, desktop, title, normalized),
                mutating=True)))

    def preview_create_document_from_template(self, template_path, title=None):
        path, template_hash, _mime_type = self._read_allowed_template(template_path)
        if title is None:
            title = path.stem
        title, _ = self._normalize_document_spec(title, [])
        payload = {"tool": "create_from_template", "template_path": str(path),
                   "template_hash": template_hash, "title": title}
        self._run(lambda _ctx, _desktop: True)
        token, ttl = self.confirmations.issue(payload)
        return {"preview_only": True, "title": title, "template_name": path.name,
                "template_hash": template_hash, "template_mime_type": _mime_type,
                "template_bytes": path.stat().st_size, "confirmation_token": token,
                "expires_in_seconds": ttl, "payload_hash": stable_hash(payload)}

    def create_document_from_template(self, template_path, expected_template_hash, title,
                                      operation_id, confirmation_token):
        path, current_hash, _mime_type = self._read_allowed_template(template_path)
        if not isinstance(expected_template_hash, str) or current_hash != expected_template_hash:
            raise WriterError("CONFLICT", "The template changed after its preview.")
        title, _ = self._normalize_document_spec(title, [])
        payload = {"tool": "create_from_template", "template_path": str(path),
                   "template_hash": current_hash, "title": title}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda context, desktop: self._create_from_template(
                    context, desktop, path, current_hash, title), mutating=True)))

    def _create_from_template(self, context, desktop, template_path, expected_template_hash, title):
        path, before_hash, _mime_type = self._read_allowed_template(str(template_path))
        if before_hash != expected_template_hash:
            raise WriterError("CONFLICT", "The template changed immediately before it was opened.")
        import uno
        args = []
        for name, value in (
            ("AsTemplate", True),
            ("Hidden", False),
            ("MacroExecutionMode", uno.getConstantByName("com.sun.star.document.MacroExecMode.NEVER_EXECUTE")),
            ("UpdateDocMode", uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE")),
        ):
            prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            prop.Name, prop.Value = name, value
            args.append(prop)
        component = None
        try:
            component = desktop.loadComponentFromURL(uno.systemPathToFileUrl(str(path)), "_blank", 0, tuple(args))
            if component is None:
                raise RuntimeError("LibreOffice returned no document from the template")
            if str(component.getURL() or "") == uno.systemPathToFileUrl(str(path)):
                raise WriterError("PERMISSION_DENIED", "LibreOffice opened the template itself instead of creating a new document.")
            component.getDocumentProperties().Title = title
            _still_path, after_hash, _mime = self._read_allowed_template(str(path))
            if after_hash != expected_template_hash:
                raise WriterError("CONFLICT", "The source template changed while the new document was being created.")
            entries = self.registry.discover(desktop, self.worker.generation)
            entry = next((candidate for candidate in entries
                          if self.registry._same_component(candidate.component, component)), None)
            if entry is None:
                raise RuntimeError("Document registry could not identify the template-derived document")
            snapshot = self.registry.snapshot(entry, force=True)
            entry.state.update(snapshot["hash"])
            return {"document_id": entry.document_id, "title": title,
                    "template_name": path.name, "template_hash": expected_template_hash,
                    "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                    "paragraph_count": sum(1 for item in snapshot["paragraphs"] if "text" in item),
                    "modified": bool(component.isModified()), "confirmed": True}
        except WriterError:
            if component is not None:
                try:
                    component.dispose()
                except Exception:
                    pass
            raise
        except Exception as exc:
            if component is not None:
                try:
                    component.dispose()
                except Exception:
                    pass
            raise WriterError("UNKNOWN_OUTCOME", "Template-based creation could not be verified; inspect Writer before retrying.",
                              retryable=False, details={"exception": type(exc).__name__}) from exc

    def _compose_document(self, context, desktop, title, blocks):
        component = None
        try:
            component = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
            if component is None:
                raise RuntimeError("Writer returned no document component")
            component.getDocumentProperties().Title = title
            text_obj = component.getText()
            text_obj.setString("")
            import uno
            paragraph_break = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
            for index, block in enumerate(blocks):
                cursor = text_obj.createTextCursor()
                cursor.gotoEnd(False)
                if index:
                    text_obj.insertControlCharacter(cursor, paragraph_break, False)
                    cursor = text_obj.createTextCursor()
                    cursor.gotoEnd(False)
                if block["type"] in {"paragraph", "heading"}:
                    text_obj.insertString(cursor, block["text"], False)
                    if block["type"] == "heading":
                        cursor.ParaStyleName = f"Heading {block['level']}"
                elif block["type"] == "table":
                    table = component.createInstance("com.sun.star.text.TextTable")
                    table.initialize(block["rows"], block["columns"])
                    text_obj.insertTextContent(cursor, table, False)
                    for row_index, values in enumerate(block["cell_values"], start=1):
                        for column_index, value in enumerate(values):
                            table.getCellByName(self._table_cell_name(column_index, row_index)).setString(value)
                else:
                    graphic_object = self._image_graphic_object(
                        context, component, block["image_path"], block["image_hash"],
                        block["alt_text"], block["width_mm"], block["height_mm"])
                    text_obj.insertTextContent(cursor, graphic_object, False)
            entries = self.registry.discover(desktop, self.worker.generation)
            entry = next((candidate for candidate in entries
                          if self.registry._same_component(candidate.component, component)), None)
            if entry is None:
                raise RuntimeError("Document registry could not identify the composed document")
            snapshot = self.registry.snapshot(entry, force=True)
            expected_tables = sum(block["type"] == "table" for block in blocks)
            expected_images = sum(block["type"] == "image" for block in blocks)
            actual_images = sum(item.get("kind") == "image" for item in snapshot["objects"])
            if component.getTextTables().getCount() != expected_tables or actual_images != expected_images:
                raise RuntimeError("The structural block count did not match the requested composition")
            for block in blocks:
                if block["type"] in {"paragraph", "heading"} and block["text"] not in str(text_obj.getString()):
                    raise RuntimeError("A requested text block was not present after composition")
            entry.state.update(snapshot["hash"])
            return {"document_id": entry.document_id, "title": title,
                    "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                    "block_count": len(blocks), "table_count": expected_tables,
                    "image_count": expected_images, "confirmed": True,
                    "modified": bool(component.isModified())}
        except WriterError:
            if component is not None:
                try:
                    component.dispose()
                except Exception as exc:
                    raise WriterError("UNKNOWN_OUTCOME", "Composition failed and Writer could not close the partial document; inspect the new window.", retryable=False) from exc
            raise
        except Exception as exc:
            if component is not None:
                try:
                    component.dispose()
                except Exception as close_exc:
                    raise WriterError("UNKNOWN_OUTCOME", "Composition failed and Writer could not close the partial document; inspect the new window.", retryable=False) from close_exc
            raise WriterError("UNKNOWN_OUTCOME", "The composed document could not be verified; inspect Writer before retrying.",
                              retryable=False, details={"exception": type(exc).__name__}) from exc

    def read_text_range(self, document_id, start_anchor, start_offset, end_anchor, end_offset):
        self._validate_paragraph_anchor(start_anchor)
        self._validate_paragraph_anchor(end_anchor)
        self._validate_range_offset(start_offset, "start_offset")
        self._validate_range_offset(end_offset, "end_offset")
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot = self.registry.snapshot(entry, force=True)
            entry.state.update(snapshot["hash"])
            start = entry.state.anchors.get(start_anchor)
            end = entry.state.anchors.get(end_anchor)
            if (start is None or end is None or start.document_id != entry.document_id
                    or end.document_id != entry.document_id or start.revision != entry.state.revision
                    or end.revision != entry.state.revision):
                raise WriterError("INVALID_ANCHOR", "Read the current structure before creating a text range.")
            self._validate_range_bounds(start, start_offset, end, end_offset)
            cursor = self._text_range_cursor(entry, start.paragraph_index, start_offset,
                                             end.paragraph_index, end_offset)
            value = str(cursor.getString())
            if len(value) > self.settings.max_snapshot_chars:
                raise WriterError("SIZE_LIMIT", "The selected range exceeds the snapshot size limit.")
            range_anchor = "r_" + uuid.uuid4().hex
            record = TextRangeRecord(entry.document_id, entry.state.revision, start.paragraph_index,
                                     start_offset, end.paragraph_index, end_offset, value)
            entry.state.ranges[range_anchor] = record
            preview = value[:self.settings.max_read_chars]
            return {"document_id": entry.document_id, "revision": entry.state.revision,
                    "snapshot_hash": snapshot["hash"], "range_anchor": range_anchor,
                    "start_anchor": start_anchor, "start_offset": start_offset,
                    "end_anchor": end_anchor, "end_offset": end_offset,
                    "text": preview, "text_truncated": len(value) > len(preview),
                    "expected_hash": stable_hash(value), "length": len(value)}
        return self._run(op)

    @staticmethod
    def _validate_range_offset(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WriterError("INVALID_ARGUMENT", f"{name} must be a non-negative code-point offset.")

    @staticmethod
    def _validate_range_bounds(start, start_offset, end, end_offset):
        if start_offset > len(start.text) or end_offset > len(end.text):
            raise WriterError("INVALID_ANCHOR", "A text range offset lies beyond its anchored paragraph.")
        if ((start.paragraph_index, start_offset) > (end.paragraph_index, end_offset)):
            raise WriterError("INVALID_ARGUMENT", "The start of a range must not follow its end.")

    def _text_range_cursor(self, entry, start_index, start_offset, end_index, end_offset):
        text = entry.component.getText()
        start_para = self._paragraph_at(entry.component, start_index)
        end_para = self._paragraph_at(entry.component, end_index)
        cursor = text.createTextCursorByRange(start_para.getStart())
        cursor.gotoStartOfParagraph(False)
        cursor.goRight(self._utf16_units(str(start_para.getString())[:start_offset]), False)
        end_cursor = text.createTextCursorByRange(end_para.getStart())
        end_cursor.gotoStartOfParagraph(False)
        end_cursor.goRight(self._utf16_units(str(end_para.getString())[:end_offset]), False)
        cursor.gotoRange(end_cursor, True)
        return cursor

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

    def preview_change(self, change_type, document_id, expected_revision, anchor=None, text=None,
                       position="end", offset=None, expected_text=None, expected_hash=None,
                       new_content=None, style_name=None, table_anchor=None, cell_name=None,
                       rows=None, columns=None, cell_values=None, image_path=None, alt_text="",
                       width_mm=None, height_mm=None, target_path=None, file_format="odt",
                       overwrite=False, formatting_options=None, table_operation=None,
                       index=None, count=1, image_ref=None, alignment=None,
                       image_alt_text=None):
        self._validate_revision(expected_revision)
        if change_type == "insert_text":
            self._validate_paragraph_anchor(anchor)
            self._validate_text(text)
            self._validate_inline_text(text)
            if position not in {"start", "end", "offset"}:
                raise WriterError("INVALID_ARGUMENT", "position must be start, end, or offset.")
            if position == "offset" and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
                raise WriterError("INVALID_ARGUMENT", "offset must be a non-negative code-point index.")
            payload = {"tool": "insert", "document_id": document_id, "anchor": anchor,
                       "expected_revision": expected_revision, "text": text,
                       "position": position, "offset": offset}
        elif change_type == "replace_range":
            self._validate_anchor(anchor)
            self._validate_text(new_content)
            self._validate_inline_text(new_content)
            self._validate_expected_value(expected_text, expected_hash)
            payload = {"tool": "replace", "document_id": document_id, "anchor": anchor,
                       "expected_revision": expected_revision, "expected_text": expected_text,
                       "expected_hash": expected_hash, "new_content": new_content}
        elif change_type == "apply_style":
            self._validate_paragraph_anchor(anchor)
            self._validate_text(expected_text)
            self._validate_text(style_name)
            self._validate_inline_text(style_name)
            payload = {"tool": "style", "document_id": document_id,
                       "anchor": anchor, "expected_revision": expected_revision,
                       "expected_text": expected_text, "style_name": style_name}
        elif change_type == "apply_formatting":
            self._validate_paragraph_anchor(anchor)
            self._validate_text(expected_text)
            formatting_options = self._normalize_formatting(formatting_options)
            payload = {"tool": "apply_formatting", "document_id": document_id,
                       "anchor": anchor, "expected_revision": expected_revision,
                       "expected_text": expected_text, "formatting": formatting_options}
        elif change_type == "format_table_cell":
            self._validate_table_anchor(table_anchor)
            self._validate_text(cell_name)
            self._validate_text(expected_text)
            formatting_options = self._normalize_formatting(formatting_options)
            payload = {"tool": "format_table_cell", "document_id": document_id,
                       "table_anchor": table_anchor, "cell_name": cell_name,
                       "expected_revision": expected_revision, "expected_text": expected_text,
                       "formatting": formatting_options}
        elif change_type == "edit_table_structure":
            self._validate_table_anchor(table_anchor)
            if table_operation not in {"insert_rows", "delete_rows", "insert_columns", "delete_columns"}:
                raise WriterError("INVALID_ARGUMENT", "table_operation must insert/delete rows or columns.")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise WriterError("INVALID_ARGUMENT", "index must be a non-negative zero-based integer.")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
                raise WriterError("INVALID_ARGUMENT", "count must be an integer from 1 to 100.")
            payload = {"tool": "edit_table_structure", "document_id": document_id,
                       "table_anchor": table_anchor, "expected_revision": expected_revision,
                       "table_operation": table_operation, "index": index, "count": count}
        elif change_type == "update_image":
            if not isinstance(image_ref, str) or not image_ref.startswith("sha256:"):
                raise WriterError("INVALID_ANCHOR", "image_ref must be an object_ref from writer_read_structure.")
            if image_alt_text is not None:
                self._validate_text(image_alt_text)
                self._validate_inline_text(image_alt_text)
            width_mm, height_mm = self._validate_image_size(width_mm, height_mm)
            if alignment is not None and (not isinstance(alignment, str)
                                          or alignment not in {"left", "center", "right"}):
                raise WriterError("INVALID_ARGUMENT", "image alignment must be left, center, or right.")
            if image_alt_text is None and width_mm is None and alignment is None:
                raise WriterError("INVALID_ARGUMENT", "Provide an image description, size, or alignment to update.")
            payload = {"tool": "update_image", "document_id": document_id,
                       "image_ref": image_ref, "expected_revision": expected_revision,
                       "alt_text": image_alt_text, "width_mm": width_mm,
                       "height_mm": height_mm, "alignment": alignment}
        elif change_type == "replace_table_cell":
            self._validate_table_anchor(table_anchor)
            self._validate_text(cell_name)
            self._validate_text(new_content)
            self._validate_inline_text(new_content)
            self._validate_expected_value(expected_text, expected_hash)
            payload = {"tool": "replace_table_cell", "document_id": document_id,
                       "table_anchor": table_anchor, "cell_name": cell_name,
                       "expected_revision": expected_revision, "new_content": new_content,
                       "expected_text": expected_text, "expected_hash": expected_hash}
        elif change_type == "insert_table":
            self._validate_paragraph_anchor(anchor)
            rows, columns, cell_values = self._normalize_table(rows, columns, cell_values)
            payload = {"tool": "insert_table", "document_id": document_id, "anchor": anchor,
                       "expected_revision": expected_revision, "rows": rows, "columns": columns,
                       "cell_values": cell_values}
        elif change_type == "insert_image":
            self._validate_paragraph_anchor(anchor)
            self._validate_text(alt_text)
            self._validate_inline_text(alt_text)
            width_mm, height_mm = self._validate_image_size(width_mm, height_mm)
            image, _image_bytes, image_hash = self._read_allowed_image(image_path)
            image_path = str(image)
            payload = {"tool": "insert_image", "document_id": document_id, "anchor": anchor,
                       "expected_revision": expected_revision, "image_path": image_path,
                       "image_hash": image_hash, "alt_text": alt_text,
                       "width_mm": width_mm, "height_mm": height_mm}
        elif change_type == "save_document":
            payload = {"tool": "save", "document_id": document_id,
                       "expected_revision": expected_revision}
        elif change_type == "save_as":
            if not isinstance(overwrite, bool):
                raise WriterError("INVALID_ARGUMENT", "overwrite must be a boolean.")
            payload = {"tool": "save_as", "document_id": document_id,
                       "expected_revision": expected_revision,
                       "target_path": target_path, "file_format": file_format,
                       "overwrite": overwrite}
        elif change_type == "undo_last_change":
            payload = {"tool": "undo", "document_id": document_id,
                       "expected_revision": expected_revision}
        else:
            raise WriterError("INVALID_ARGUMENT", "Unsupported change_type.")

        preview = self._run(lambda _ctx, desktop: self._preview_change(
            desktop, change_type, document_id, expected_revision, payload,
            anchor=anchor, text=text, position=position, offset=offset,
            expected_text=expected_text, expected_hash=expected_hash,
            new_content=new_content, style_name=style_name,
            table_anchor=table_anchor, cell_name=cell_name, rows=rows,
            columns=columns, cell_values=cell_values, image_path=image_path,
            image_hash=payload.get("image_hash"), alt_text=alt_text,
            width_mm=width_mm, height_mm=height_mm,
            target_path=target_path, file_format=file_format, overwrite=overwrite,
            formatting_options=payload.get("formatting"), table_operation=table_operation,
            index=index, count=count, image_ref=image_ref, alignment=alignment,
            image_alt_text=image_alt_text))
        token, ttl = self.confirmations.issue(payload)
        return {**preview, "confirmation_token": token, "expires_in_seconds": ttl,
                "payload_hash": stable_hash(payload)}

    @staticmethod
    def _validate_expected_value(expected_text, expected_hash):
        if (expected_text is None) == (expected_hash is None):
            raise WriterError("INVALID_ARGUMENT", "Provide exactly one of expected_text or expected_hash.")
        if expected_text is not None:
            if not isinstance(expected_text, str):
                raise WriterError("INVALID_ARGUMENT", "expected_text must be a string.")
        if expected_hash is not None and (not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:")):
            raise WriterError("INVALID_ARGUMENT", "expected_hash must be a sha256 snapshot hash.")

    def _preview_change(self, desktop, change_type, document_id, expected_revision, payload, **args):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        common = {"document_id": entry.document_id, "revision": entry.state.revision,
                  "change_type": change_type, "preview_only": True}
        if change_type == "insert_text":
            cursor, _target, record = self.registry.paragraph_cursor(
                entry, args["anchor"], expected_revision, record_text(entry, args["anchor"]))
            position, offset, value = args["position"], args["offset"], args["text"]
            if position == "start":
                after = value + record.text
            elif position == "end":
                after = record.text + value
            else:
                if offset > len(record.text):
                    raise WriterError("INVALID_ANCHOR", "offset lies beyond the anchored paragraph.")
                after = record.text[:offset] + value + record.text[offset:]
            return {**common, "anchor": args["anchor"], "before_text": self._preview_text(record.text),
                    "after_text": self._preview_text(after), "before_hash": stable_hash(record.text),
                    "after_hash": stable_hash(after), "truncated": len(record.text) > 1000 or len(after) > 1000}
        if change_type == "replace_range":
            record_text_value, actual_text = self._resolve_replace_target(
                entry, args["anchor"], expected_revision, args["expected_text"], args["expected_hash"])
            return {**common, "anchor": args["anchor"], "before_text": self._preview_text(actual_text),
                    "after_text": self._preview_text(args["new_content"]),
                    "before_hash": stable_hash(actual_text), "after_hash": stable_hash(args["new_content"]),
                    "truncated": len(actual_text) > 1000 or len(args["new_content"]) > 1000}
        if change_type == "apply_style":
            record = entry.state.anchors.get(args["anchor"])
            if record is None or record.revision != expected_revision:
                raise WriterError("INVALID_ANCHOR", "Read the current structure to obtain a paragraph anchor.")
            if record.text != args["expected_text"]:
                raise WriterError("CONFLICT", "expected_text does not match the anchored paragraph.")
            self._validate_style(entry.component, args["style_name"])
            return {**common, "anchor": args["anchor"], "text": self._preview_text(record.text),
                    "current_style": record.style_name, "proposed_style": args["style_name"],
                    "text_hash": stable_hash(record.text), "truncated": len(record.text) > 1000}
        if change_type == "apply_formatting":
            _cursor, _target, record = self.registry.paragraph_cursor(
                entry, args["anchor"], expected_revision, args["expected_text"])
            paragraph_items = [item for item in self.registry.snapshot(entry)["paragraphs"] if "text" in item]
            current_format = (dict(paragraph_items[record.paragraph_index].get("formatting", {}))
                              if record.paragraph_index < len(paragraph_items) else {})
            return {**common, "anchor": args["anchor"], "text": self._preview_text(record.text),
                    "text_hash": stable_hash(record.text), "current_formatting": current_format,
                    "proposed_formatting": dict(args["formatting_options"]),
                    "truncated": len(record.text) > 1000}
        if change_type == "format_table_cell":
            table, table_record = self.registry.resolve_table(entry, args["table_anchor"], expected_revision)
            if args["cell_name"] not in table_record.cell_names:
                raise WriterError("INVALID_ANCHOR", "The cell_name does not belong to the anchored table.")
            cell = table.getCellByName(args["cell_name"])
            current = str(cell.getString())
            if current != args["expected_text"]:
                raise WriterError("CONFLICT", "expected_text does not match the current table cell.")
            table_item = next(item for item in self.registry.snapshot(entry)["paragraphs"]
                              if item.get("name") == table_record.table_name)
            current_formats = dict(table_item.get("cell_formatting", ()))
            return {**common, "table_anchor": args["table_anchor"], "cell_name": args["cell_name"],
                    "text": self._preview_text(current), "text_hash": stable_hash(current),
                    "current_formatting": current_formats.get(args["cell_name"], {}),
                    "proposed_formatting": dict(args["formatting_options"])}
        if change_type == "edit_table_structure":
            table, table_record = self.registry.resolve_table(entry, args["table_anchor"], expected_revision)
            row_count, column_count, matrix = self._table_shape_and_values(table)
            operation = args["table_operation"]
            index, count = args["index"], args["count"]
            is_rows = operation.endswith("rows")
            dimension = row_count if is_rows else column_count
            deleting = operation.startswith("delete")
            if (index > dimension if not deleting else index + count > dimension):
                raise WriterError("INVALID_ARGUMENT", "The requested table index/count lies outside the current table.")
            if deleting and dimension - count < 1:
                raise WriterError("INVALID_ARGUMENT", "A table must keep at least one row and one column.")
            new_rows = row_count + (count if operation == "insert_rows" else -count if operation == "delete_rows" else 0)
            new_columns = column_count + (count if operation == "insert_columns" else -count if operation == "delete_columns" else 0)
            if new_rows * new_columns > self.settings.max_table_cells:
                raise WriterError("SIZE_LIMIT", f"A table may contain at most {self.settings.max_table_cells} cells.")
            removed_cells = []
            if deleting:
                if is_rows:
                    for row_number in range(index, index + count):
                        for column_number in range(column_count):
                            name = self._table_cell_name(column_number, row_number + 1)
                            removed_cells.append({"name": name, "text": self._preview_text(matrix[row_number][column_number])})
                else:
                    for row_number in range(row_count):
                        for column_number in range(index, index + count):
                            name = self._table_cell_name(column_number, row_number + 1)
                            removed_cells.append({"name": name, "text": self._preview_text(matrix[row_number][column_number])})
            table_hash = stable_hash({"rows": row_count, "columns": column_count, "values": matrix})
            payload["table_hash"] = table_hash
            return {**common, "table_anchor": args["table_anchor"],
                    "table_operation": operation, "index": index, "count": count,
                    "rows_before": row_count, "columns_before": column_count,
                    "rows_after": new_rows, "columns_after": new_columns,
                    "table_hash": table_hash, "removed_cells": removed_cells[:100],
                    "removed_cells_truncated": len(removed_cells) > 100}
        if change_type == "update_image":
            graphic, record = self._resolve_image_ref(entry, args["image_ref"], expected_revision)
            payload["image_record_hash"] = stable_hash(record)
            current = {"alt_text": record.get("alt_text", ""),
                       "width_mm": round(record.get("width_100mm", 0) / 100, 2) or None,
                       "height_mm": round(record.get("height_100mm", 0) / 100, 2) or None,
                       "anchor_type": record.get("anchor_type")}
            return {**common, "image_ref": args["image_ref"], "current_image": current,
                    "proposed_image": {"alt_text": args["image_alt_text"],
                                       "width_mm": args["width_mm"],
                                       "height_mm": args["height_mm"],
                                       "alignment": args["alignment"]}}
        if change_type == "replace_table_cell":
            table, record = self.registry.resolve_table(entry, args["table_anchor"], expected_revision)
            if args["cell_name"] not in record.cell_names:
                raise WriterError("INVALID_ANCHOR", "The cell_name does not belong to the anchored table.")
            current = str(table.getCellByName(args["cell_name"]).getString())
            self._check_expected_value(current, args["expected_text"], args["expected_hash"])
            return {**common, "table_anchor": args["table_anchor"], "cell_name": args["cell_name"],
                    "before_text": self._preview_text(current), "after_text": self._preview_text(args["new_content"]),
                    "before_hash": stable_hash(current), "after_hash": stable_hash(args["new_content"]),
                    "truncated": len(current) > 1000 or len(args["new_content"]) > 1000}
        if change_type == "insert_table":
            self.registry.paragraph_cursor(entry, args["anchor"], expected_revision,
                                           record_text(entry, args["anchor"]))
            rows, columns, cell_values = self._normalize_table(args["rows"], args["columns"], args["cell_values"])
            preview_cells = []
            for row_index, values in enumerate(cell_values[:5], start=1):
                for column_index, value in enumerate(values[:5]):
                    if len(preview_cells) >= 10:
                        break
                    preview_cells.append({"cell_name": self._table_cell_name(column_index, row_index),
                                          "text": self._preview_text(value)})
            return {**common, "anchor": args["anchor"], "rows": rows, "columns": columns,
                    "cell_count": rows * columns, "cells_preview": preview_cells,
                    "cells_truncated": rows * columns > len(preview_cells)}
        if change_type == "insert_image":
            self.registry.paragraph_cursor(entry, args["anchor"], expected_revision,
                                           record_text(entry, args["anchor"]))
            path, content, image_hash = self._read_allowed_image(args["image_path"])
            if image_hash != args["image_hash"]:
                raise WriterError("CONFLICT", "The image file changed after the preview was created.")
            return {**common, "anchor": args["anchor"], "image_name": path.name,
                    "image_hash": image_hash, "image_bytes": len(content),
                    "alt_text": args["alt_text"], "width_mm": args["width_mm"],
                    "height_mm": args["height_mm"]}
        if change_type == "save_document":
            path = self._allowed_save_path(entry.component)
            payload["expected_path"] = str(path)
            return {**common, "path": str(path), "modified": bool(entry.component.isModified()),
                    "will_write_file": bool(entry.component.isModified()), "overwrite_existing_file": True}
        if change_type == "save_as":
            path, filter_name, target_hash = self._save_as_target(
                args["target_path"], args["file_format"], args["overwrite"])
            payload.update({"target_path": str(path), "file_format": args["file_format"],
                            "filter_name": filter_name, "overwrite": args["overwrite"],
                            "target_hash": target_hash})
            return {**common, "path": str(path), "file_format": args["file_format"],
                    "filter_name": filter_name, "target_exists": target_hash is not None,
                    "target_hash": target_hash, "overwrite": args["overwrite"],
                    "modified": bool(entry.component.isModified())}
        if change_type == "undo_last_change":
            record = self._undo_records.get(entry.document_id)
            if record is None or record.revision != expected_revision:
                raise WriterError("UNDO_NOT_AVAILABLE", "No latest Yberu MCP operation is available to undo at this revision.")
            manager = entry.component.getUndoManager()
            title = str(manager.getCurrentUndoActionTitle())
            if title != record.action_label:
                raise WriterError("CONFLICT", "The next Writer undo action is not the latest Yberu MCP edit.",
                                  details={"expected_action": record.action_label, "current_action": title})
            payload["target_operation_id"] = record.operation_id
            return {**common, "target_operation_id": record.operation_id, "undo_action": title,
                    "current_hash": record.after_hash, "resulting_hash": record.before_hash}
        raise WriterError("INVALID_ARGUMENT", "Unsupported change_type.")

    @staticmethod
    def _preview_text(value):
        return value[:1000]

    @staticmethod
    def _check_expected_value(current, expected_text, expected_hash):
        if expected_text is not None and expected_text != current:
            raise WriterError("CONFLICT", "expected_text does not match the current target.")
        if expected_hash is not None and expected_hash != stable_hash(current):
            raise WriterError("CONFLICT", "expected_hash does not match the current target.")

    def _resolve_replace_target(self, entry, anchor, expected_revision, expected_text, expected_hash):
        if anchor.startswith("r_"):
            record = entry.state.ranges.get(anchor)
            if record is None or record.document_id != entry.document_id or record.revision != expected_revision:
                raise WriterError("INVALID_ANCHOR", "The text range anchor is unknown or stale.")
            cursor = self._text_range_cursor(entry, record.start_paragraph_index, record.start_offset,
                                             record.end_paragraph_index, record.end_offset)
            actual = str(cursor.getString())
            if actual != record.text:
                raise WriterError("CONFLICT", "The anchored text range has changed.")
            text = entry.component.getText()
            before = str(text.getString())
            prefix_cursor = text.createTextCursor()
            prefix_cursor.gotoStart(False)
            prefix_cursor.gotoRange(cursor.getStart(), True)
            prefix = str(prefix_cursor.getString())
            suffix_cursor = text.createTextCursorByRange(cursor.getEnd())
            suffix_cursor.gotoEnd(True)
            suffix = str(suffix_cursor.getString())
            if prefix + actual + suffix != before:
                raise WriterError("RANGE_UNSUPPORTED", "This range crosses a Writer structure that is not supported for replacement.")
        else:
            record = entry.state.anchors.get(anchor)
            if record is None or record.document_id != entry.document_id or record.revision != expected_revision:
                raise WriterError("INVALID_ANCHOR", "The paragraph anchor is unknown or stale.")
            actual = record.text
            self.registry.paragraph_cursor(entry, anchor, expected_revision, actual)
        self._check_expected_value(actual, expected_text, expected_hash)
        return record, actual

    def _validate_style(self, component, style_name):
        try:
            styles = component.getStyleFamilies().getByName("ParagraphStyles")
            if not styles.hasByName(style_name):
                raise WriterError("INVALID_STYLE", "The requested paragraph style does not exist.")
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("INVALID_STYLE", "Could not validate the paragraph style name.") from exc

    def _allowed_save_path(self, component):
        try:
            url = str(component.getURL() or "")
            if not url.startswith("file:"):
                raise WriterError("PERMISSION_DENIED", "In-place save requires an existing local file; use writer_save_as for a new destination.")
            import uno
            path = Path(uno.fileUrlToSystemPath(url)).resolve(strict=True)
            if not self._path_allowed(path):
                raise WriterError("PERMISSION_DENIED", "The document path is outside YBERU_WRITER_ALLOWED_SAVE_ROOTS.")
            return path
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("PERMISSION_DENIED", "Could not validate the current document path.") from exc

    def insert_text(self, document_id, anchor, expected_revision, text, position="end", offset=None,
                    operation_id="", confirmation_token=None):
        self._validate_revision(expected_revision)
        self._validate_paragraph_anchor(anchor)
        self._validate_text(text)
        self._validate_inline_text(text)
        if not isinstance(position, str) or position not in {"start", "end", "offset"}:
            raise WriterError("INVALID_ARGUMENT", "position must be start, end, or offset.")
        if position == "offset" and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise WriterError("INVALID_ARGUMENT", "offset is required and must be a non-negative code-point index for position=offset.")
        payload = {"tool": "insert", "document_id": document_id, "anchor": anchor, "expected_revision": expected_revision,
                   "text": text, "position": position, "offset": offset}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._insert_text(desktop, document_id, anchor, expected_revision, text,
                                                        position, offset, operation_id), mutating=True)))

    def _insert_text(self, desktop, document_id, anchor, expected_revision, value, position, offset, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
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
        with self._undo_context(entry.component, operation_id) as action_label:
            entry.component.getText().insertString(cursor, value, False)
        snapshot = self.registry.update_after_mutation(entry)
        self._record_undo(entry, operation_id, action_label, "insert_text", before, snapshot)
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

    def insert_table(self, document_id, anchor, expected_revision, rows, columns, cell_values,
                     operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_paragraph_anchor(anchor)
        rows, columns, values = self._normalize_table(rows, columns, cell_values)
        payload = {"tool": "insert_table", "document_id": document_id, "anchor": anchor,
                   "expected_revision": expected_revision, "rows": rows, "columns": columns,
                   "cell_values": values}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._insert_table(desktop, document_id, anchor,
                    expected_revision, rows, columns, values, operation_id), mutating=True)))

    def _insert_table(self, desktop, document_id, anchor, expected_revision,
                      rows, columns, cell_values, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        cursor, _target, record = self.registry.paragraph_cursor(
            entry, anchor, expected_revision, record_text(entry, anchor))
        component = entry.component
        try:
            table = component.createInstance("com.sun.star.text.TextTable")
            table.initialize(rows, columns)
        except Exception as exc:
            raise WriterError("UNO_ERROR", "Writer could not initialize the requested table.") from exc
        cursor.gotoEndOfParagraph(False)
        insert_attempted = False
        try:
            with self._undo_context(component, operation_id) as action_label:
                insert_attempted = True
                component.getText().insertTextContent(cursor, table, False)
                for row_index, values in enumerate(cell_values, start=1):
                    for column_index, value in enumerate(values):
                        cell_name = self._table_cell_name(column_index, row_index)
                        table.getCellByName(cell_name).setString(value)
        except WriterError:
            raise
        except Exception as exc:
            code = "UNKNOWN_OUTCOME" if insert_attempted else "UNO_ERROR"
            raise WriterError(code, "The table insertion did not finish cleanly; inspect the document before retrying.", retryable=False) from exc
        try:
            after = self.registry.snapshot(entry, force=True)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The table was inserted but its resulting snapshot could not be read.", retryable=False) from exc
        new_table_name = self._verify_inserted_table(before, after, cell_values)
        if new_table_name is None or not self._preserves_existing_content(before, after):
            raise WriterError("UNKNOWN_OUTCOME", "The table result or unrelated document content could not be verified; inspect the document.", retryable=False)
        entry.state.update(after["hash"])
        self._record_undo(entry, operation_id, action_label, "insert_table", before, after)
        return {"document_id": entry.document_id, "table_name": new_table_name,
                "rows": rows, "columns": columns, "revision": entry.state.revision,
                "snapshot_hash": after["hash"], "confirmed": True}

    def insert_image(self, document_id, anchor, expected_revision, image_path, expected_image_hash,
                     alt_text, width_mm, height_mm, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_paragraph_anchor(anchor)
        self._validate_text(alt_text)
        self._validate_inline_text(alt_text)
        width_mm, height_mm = self._validate_image_size(width_mm, height_mm)
        path, _content, image_hash = self._read_allowed_image(image_path)
        if not isinstance(expected_image_hash, str) or expected_image_hash != image_hash:
            raise WriterError("CONFLICT", "The image file no longer matches the hash returned by its preview.")
        payload = {"tool": "insert_image", "document_id": document_id, "anchor": anchor,
                   "expected_revision": expected_revision, "image_path": str(path),
                   "image_hash": image_hash, "alt_text": alt_text,
                   "width_mm": width_mm, "height_mm": height_mm}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda context, desktop: self._insert_image(context, desktop, document_id, anchor,
                    expected_revision, path, image_hash, alt_text, width_mm, height_mm, operation_id),
                mutating=True)))

    def _insert_image(self, context, desktop, document_id, anchor, expected_revision,
                      image_path, expected_image_hash, alt_text, width_mm, height_mm, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        cursor, _target, _record = self.registry.paragraph_cursor(
            entry, anchor, expected_revision, record_text(entry, anchor))
        component = entry.component
        path, _content, image_hash = self._read_allowed_image(str(image_path))
        if image_hash != expected_image_hash:
            raise WriterError("CONFLICT", "The image file changed after confirmation.")
        graphic_object = self._image_graphic_object(
            context, component, str(path), expected_image_hash, alt_text, width_mm, height_mm)
        cursor.gotoEndOfParagraph(False)
        insert_attempted = False
        try:
            with self._undo_context(component, operation_id) as action_label:
                insert_attempted = True
                component.getText().insertTextContent(cursor, graphic_object, False)
        except WriterError:
            raise
        except Exception as exc:
            code = "UNKNOWN_OUTCOME" if insert_attempted else "UNO_ERROR"
            raise WriterError(code, "The image insertion did not finish cleanly; inspect the document before retrying.", retryable=False) from exc
        try:
            after = self.registry.snapshot(entry, force=True)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The image was inserted but its resulting snapshot could not be read.", retryable=False) from exc
        before_images = [item for item in before["objects"] if item.get("kind") == "image"]
        after_images = [item for item in after["objects"] if item.get("kind") == "image"]
        image_found = (len(after_images) == len(before_images) + 1
                       and any(item.get("alt_text", "") == alt_text for item in after_images))
        if not image_found or not self._preserves_existing_content(before, after):
            raise WriterError("UNKNOWN_OUTCOME", "The image result or unrelated document content could not be verified; inspect the document.", retryable=False)
        entry.state.update(after["hash"])
        self._record_undo(entry, operation_id, action_label, "insert_image", before, after)
        return {"document_id": entry.document_id, "image_name": path.name,
                "image_hash": image_hash, "alt_text": alt_text,
                "revision": entry.state.revision, "snapshot_hash": after["hash"], "confirmed": True}

    def _image_graphic_object(self, context, component, image_path, expected_image_hash,
                              alt_text, width_mm, height_mm):
        import uno
        path, content, image_hash = self._read_allowed_image(str(image_path))
        if image_hash != expected_image_hash:
            raise WriterError("CONFLICT", "The image file changed after its preview was created.")
        graphic_object = component.createInstance("com.sun.star.text.TextGraphicObject")
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="yberu-writer-image-", suffix=path.suffix, delete=False) as staged:
                staged.write(content)
                temp_path = Path(staged.name)
            provider = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.graphic.GraphicProvider", context)
            url_prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            url_prop.Name, url_prop.Value = "URL", uno.systemPathToFileUrl(str(temp_path))
            link_prop = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            link_prop.Name, link_prop.Value = "LoadAsLink", False
            graphic = provider.queryGraphic((url_prop, link_prop))
            if graphic is None:
                raise WriterError("INVALID_ARGUMENT", "LibreOffice could not decode this image.")
            graphic_object.setPropertyValue("Graphic", graphic)
            graphic_object.setPropertyValue("AnchorType", uno.Enum(
                "com.sun.star.text.TextContentAnchorType", "AS_CHARACTER"))
            property_info = graphic_object.getPropertySetInfo()
            if property_info.hasPropertyByName("Description"):
                graphic_object.setPropertyValue("Description", alt_text)
            elif alt_text:
                raise WriterError("UNO_ERROR", "This Writer version does not expose image description text.")
            if width_mm is not None and height_mm is not None:
                graphic_object.setPropertyValue("Width", width_mm * 100)
                graphic_object.setPropertyValue("Height", height_mm * 100)
            return graphic_object
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("INVALID_ARGUMENT", "LibreOffice could not prepare the permitted image file.") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _verify_inserted_table(self, before, after, cell_values):
        old_tables = {item["name"]: item["cells"] for item in before["paragraphs"] if "table" in item}
        new_tables = [item for item in after["paragraphs"]
                      if "table" in item and item["name"] not in old_tables]
        if after["table_count"] != before["table_count"] + 1 or len(new_tables) != 1:
            return None
        expected_cells = tuple(
            (self._table_cell_name(column_index, row_index), value)
            for row_index, values in enumerate(cell_values, start=1)
            for column_index, value in enumerate(values)
        )
        if new_tables[0]["cells"] != expected_cells:
            return None
        for table in before["paragraphs"]:
            if "table" not in table:
                continue
            current = next((item for item in after["paragraphs"]
                            if item.get("name") == table["name"] and "table" in item), None)
            if current is None or current["cells"] != table["cells"]:
                return None
        return new_tables[0]["name"]

    @staticmethod
    def _preserves_existing_content(before, after):
        before_paragraphs = [(item["text"], item["style"], item["outline_level"])
                             for item in before["paragraphs"]
                             if "text" in item and item["text"]]
        after_paragraphs = [(item["text"], item["style"], item["outline_level"])
                            for item in after["paragraphs"]
                            if "text" in item and item["text"]]
        position = 0
        for paragraph in after_paragraphs:
            if position < len(before_paragraphs) and paragraph == before_paragraphs[position]:
                position += 1
        if position != len(before_paragraphs):
            return False
        after_tables = {item["name"]: item["cells"] for item in after["paragraphs"] if "table" in item}
        for table in before["paragraphs"]:
            if "table" in table and after_tables.get(table["name"]) != table["cells"]:
                return False
        before_objects = [item for item in before["objects"] if item.get("kind") != "image"]
        after_objects = [item for item in after["objects"] if item.get("kind") != "image"]
        return all(item in after_objects for item in before_objects)

    def replace_range(self, document_id, anchor, expected_revision, new_content, operation_id,
                      confirmation_token, expected_text=None, expected_hash=None):
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
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._replace_range(desktop, document_id, anchor, expected_revision,
                                                           expected_text, expected_hash, new_content, operation_id), mutating=True)))

    def _replace_range(self, desktop, document_id, anchor, expected_revision, expected_text, expected_hash,
                       new_content, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        if anchor.startswith("r_"):
            return self._replace_text_range(entry, anchor, expected_revision, expected_text, expected_hash,
                                            new_content, operation_id, before)
        record = entry.state.anchors.get(anchor)
        if record is None:
            raise WriterError("INVALID_ANCHOR", "Read the document structure to obtain a current paragraph anchor.")
        if expected_text is not None and expected_text != record.text:
            raise WriterError("CONFLICT", "expected_text does not match the anchored paragraph.")
        if expected_hash is not None and expected_hash != stable_hash(record.text):
            raise WriterError("CONFLICT", "expected_hash does not match the anchored paragraph.")
        cursor, _target, _record = self.registry.paragraph_cursor(entry, anchor, expected_revision, record.text)
        with self._undo_context(entry.component, operation_id) as action_label:
            cursor.setString(new_content)
        snapshot = self.registry.update_after_mutation(entry)
        self._record_undo(entry, operation_id, action_label, "replace_range", before, snapshot)
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

    def _replace_text_range(self, entry, range_anchor, expected_revision, expected_text, expected_hash,
                            new_content, operation_id, before_snapshot):
        record = entry.state.ranges.get(range_anchor)
        if (record is None or record.document_id != entry.document_id or record.revision != expected_revision):
            raise WriterError("INVALID_ANCHOR", "The text range anchor is unknown or stale. Read the range again.")
        if expected_text is not None and expected_text != record.text:
            raise WriterError("CONFLICT", "expected_text does not match the anchored range.")
        if expected_hash is not None and expected_hash != stable_hash(record.text):
            raise WriterError("CONFLICT", "expected_hash does not match the anchored range.")
        cursor = self._text_range_cursor(entry, record.start_paragraph_index, record.start_offset,
                                         record.end_paragraph_index, record.end_offset)
        actual_range = str(cursor.getString())
        if actual_range != record.text:
            raise WriterError("CONFLICT", "The anchored text range has changed.")
        text = entry.component.getText()
        try:
            before = str(text.getString())
            prefix_cursor = text.createTextCursor()
            prefix_cursor.gotoStart(False)
            prefix_cursor.gotoRange(cursor.getStart(), True)
            prefix = str(prefix_cursor.getString())
            suffix_cursor = text.createTextCursorByRange(cursor.getEnd())
            suffix_cursor.gotoEnd(True)
            suffix = str(suffix_cursor.getString())
        except Exception as exc:
            raise WriterError("INVALID_ANCHOR", "Could not resolve the range boundaries safely.") from exc
        if prefix + actual_range + suffix != before:
            raise WriterError("RANGE_UNSUPPORTED", "This range crosses a Writer structure that is not supported for text replacement.")
        with self._undo_context(entry.component, operation_id) as action_label:
            cursor.setString(new_content)
        if str(text.getString()) != prefix + new_content + suffix:
            raise WriterError("UNKNOWN_OUTCOME", "The range replacement changed surrounding document text; inspect the document.", retryable=False)
        try:
            snapshot = self.registry.snapshot(entry, force=True)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The range changed, but the resulting revision could not be read; inspect the document.", retryable=False) from exc
        entry.state.update(snapshot["hash"])
        self._record_undo(entry, operation_id, action_label, "replace_range", before_snapshot, snapshot)
        return {"document_id": entry.document_id, "revision": entry.state.revision,
                "snapshot_hash": snapshot["hash"], "range_anchor": range_anchor,
                "replaced_chars": len(record.text), "new_chars": len(new_content),
                "surrounding_text_verified": True, "confirmed": True}

    def read_table(self, document_id, table_anchor, expected_revision):
        self._validate_revision(expected_revision)
        self._validate_table_anchor(table_anchor)
        def op(_ctx, desktop):
            entry = self._entry(desktop, document_id)
            snapshot = self.registry.snapshot(entry)
            entry.state.update(snapshot["hash"])
            if expected_revision != entry.state.revision:
                raise WriterError("CONFLICT", "The document changed since the table reference was read.",
                                  details={"expected_revision": expected_revision, "current_revision": entry.state.revision})
            table, record = self.registry.resolve_table(entry, table_anchor, expected_revision)
            row_count = int(table.getRows().getCount())
            column_count = int(table.getColumns().getCount())
            cells = []
            rows_by_number = {}
            total_chars = 0
            table_snapshot = next((item for item in snapshot["paragraphs"]
                                   if item.get("name") == record.table_name), {})
            formatting_by_cell = dict(table_snapshot.get("cell_formatting", ()))
            remaining = self.settings.max_read_chars
            for cell_name in record.cell_names[:self.settings.max_structure_items]:
                value = str(table.getCellByName(cell_name).getString())
                total_chars += len(value)
                preview = value[:remaining]
                remaining = max(0, remaining - len(value))
                cell = {"name": cell_name, "text": preview, "text_truncated": len(preview) < len(value),
                        "expected_hash": stable_hash(value), "length": len(value),
                        "formatting": dict(formatting_by_cell.get(cell_name, {}))}
                cells.append(cell)
                suffix = cell_name.rsplit(":", 1)[-1]
                row_number = "".join(character for character in suffix if character.isdigit())
                rows_by_number.setdefault(row_number or "?", []).append(cell)
            rows = [rows_by_number[key] for key in sorted(rows_by_number, key=lambda value: int(value) if value.isdigit() else 0)]
            return {"document_id": entry.document_id, "table_anchor": table_anchor,
                    "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                    "rows": rows, "row_count": row_count, "column_count": column_count,
                    "cell_count": len(record.cell_names), "returned_cell_count": len(cells),
                    "cells_truncated": len(cells) < len(record.cell_names), "total_chars": total_chars,
                    "truncated": total_chars > self.settings.max_read_chars or len(cells) < len(record.cell_names)}
        return self._run(op)

    def replace_table_cell(self, document_id, table_anchor, cell_name, expected_revision,
                           new_content, operation_id, confirmation_token, expected_text=None, expected_hash=None):
        self._validate_revision(expected_revision)
        self._validate_table_anchor(table_anchor)
        self._validate_text(cell_name)
        self._validate_text(new_content)
        self._validate_inline_text(new_content)
        if (expected_text is None) == (expected_hash is None):
            raise WriterError("INVALID_ARGUMENT", "Provide exactly one of expected_text or expected_hash.")
        if expected_text is not None:
            self._validate_text(expected_text)
        if expected_hash is not None and (not isinstance(expected_hash, str) or not expected_hash.startswith("sha256:")):
            raise WriterError("INVALID_ARGUMENT", "expected_hash must be a sha256 snapshot hash.")
        payload = {"tool": "replace_table_cell", "document_id": document_id, "table_anchor": table_anchor,
                   "cell_name": cell_name, "expected_revision": expected_revision,
                   "new_content": new_content, "expected_text": expected_text, "expected_hash": expected_hash}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._replace_table_cell(desktop, document_id, table_anchor, cell_name,
                                                                expected_revision, new_content, expected_text, expected_hash,
                                                                operation_id), mutating=True)))

    def _replace_table_cell(self, desktop, document_id, table_anchor, cell_name,
                            expected_revision, new_content, expected_text, expected_hash, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        table, record = self.registry.resolve_table(entry, table_anchor, expected_revision)
        if cell_name not in record.cell_names:
            raise WriterError("INVALID_ANCHOR", "The cell_name does not belong to the anchored table.")
        cell = table.getCellByName(cell_name)
        current = str(cell.getString())
        if expected_text is not None and expected_text != current:
            raise WriterError("CONFLICT", "expected_text does not match the table cell.")
        if expected_hash is not None and expected_hash != stable_hash(current):
            raise WriterError("CONFLICT", "expected_hash does not match the table cell.")
        before_tables = [item for item in before["paragraphs"] if item.get("name") == record.table_name]
        if len(before_tables) != 1:
            raise WriterError("INVALID_ANCHOR", "The table identity is ambiguous in the current snapshot.")
        expected_cells = dict(before_tables[0]["cells"])
        if expected_cells.get(cell_name) != current:
            raise WriterError("CONFLICT", "The table cell changed while resolving its anchor.")
        expected_cells[cell_name] = new_content
        expected_paragraphs = copy.deepcopy(before["paragraphs"])
        for item in expected_paragraphs:
            if item.get("name") == record.table_name:
                item["cells"] = tuple((name, expected_cells[name]) for name, _value in item["cells"])
                break
        with self._undo_context(entry.component, operation_id) as action_label:
            cell.setString(new_content)
        try:
            after = self.registry.snapshot(entry, force=True)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The table cell changed, but the resulting snapshot could not be read.", retryable=False) from exc
        if after["paragraphs"] != expected_paragraphs:
            raise WriterError("UNKNOWN_OUTCOME", "The table edit changed content outside the requested cell; inspect the document.", retryable=False)
        entry.state.update(after["hash"])
        self._record_undo(entry, operation_id, action_label, "replace_table_cell", before, after)
        return {"document_id": entry.document_id, "table_anchor": table_anchor, "cell_name": cell_name,
                "revision": entry.state.revision, "snapshot_hash": after["hash"],
                "previous_hash": stable_hash(current), "new_hash": stable_hash(new_content), "confirmed": True}

    @staticmethod
    def _table_cell_name(column_index, row_index):
        value = column_index + 1
        label = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            label = chr(65 + remainder) + label
        return f"{label}{row_index}"

    def _table_shape_and_values(self, table):
        rows = int(table.getRows().getCount())
        columns = int(table.getColumns().getCount())
        values = [[str(table.getCellByName(self._table_cell_name(column, row + 1)).getString())
                   for column in range(columns)] for row in range(rows)]
        return rows, columns, values

    def edit_table_structure(self, document_id, table_anchor, expected_revision,
                             table_operation, index, count, expected_table_hash,
                             operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_table_anchor(table_anchor)
        if table_operation not in {"insert_rows", "delete_rows", "insert_columns", "delete_columns"}:
            raise WriterError("INVALID_ARGUMENT", "Unsupported table structural operation.")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise WriterError("INVALID_ARGUMENT", "index must be a non-negative zero-based integer.")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            raise WriterError("INVALID_ARGUMENT", "count must be an integer from 1 to 100.")
        if not isinstance(expected_table_hash, str) or not expected_table_hash.startswith("sha256:"):
            raise WriterError("INVALID_ARGUMENT", "expected_table_hash must come from the structural edit preview.")
        payload = {"tool": "edit_table_structure", "document_id": document_id,
                   "table_anchor": table_anchor, "expected_revision": expected_revision,
                   "table_operation": table_operation, "index": index, "count": count,
                   "table_hash": expected_table_hash}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._edit_table_structure(
                    desktop, document_id, table_anchor, expected_revision,
                    table_operation, index, count, expected_table_hash, operation_id),
                mutating=True)))

    def _edit_table_structure(self, desktop, document_id, table_anchor, expected_revision,
                              operation, index, count, expected_table_hash, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        table, record = self.registry.resolve_table(entry, table_anchor, expected_revision)
        old_rows, old_columns, old_values = self._table_shape_and_values(table)
        actual_hash = stable_hash({"rows": old_rows, "columns": old_columns, "values": old_values})
        if actual_hash != expected_table_hash:
            raise WriterError("CONFLICT", "The table contents or dimensions changed after the preview.")
        row_operation = operation.endswith("rows")
        dimension = old_rows if row_operation else old_columns
        deleting = operation.startswith("delete")
        if (index > dimension if not deleting else index + count > dimension):
            raise WriterError("INVALID_ARGUMENT", "The requested index/count lies outside the current table.")
        if deleting and dimension - count < 1:
            raise WriterError("INVALID_ARGUMENT", "A table must keep at least one row and one column.")
        new_rows = old_rows + (count if operation == "insert_rows" else -count if operation == "delete_rows" else 0)
        new_columns = old_columns + (count if operation == "insert_columns" else -count if operation == "delete_columns" else 0)
        if new_rows * new_columns > self.settings.max_table_cells:
            raise WriterError("SIZE_LIMIT", f"A table may contain at most {self.settings.max_table_cells} cells.")
        if operation == "insert_rows":
            expected_values = old_values[:index] + [["" for _ in range(old_columns)] for _ in range(count)] + old_values[index:]
        elif operation == "delete_rows":
            expected_values = old_values[:index] + old_values[index + count:]
        elif operation == "insert_columns":
            expected_values = [row[:index] + [""] * count + row[index:] for row in old_values]
        else:
            expected_values = [row[:index] + row[index + count:] for row in old_values]
        try:
            with self._undo_context(entry.component, operation_id) as action_label:
                table_access = table.getRows() if row_operation else table.getColumns()
                if deleting:
                    table_access.removeByIndex(index, count)
                else:
                    table_access.insertByIndex(index, count)
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The table structure may have changed; reread the table before retrying.", retryable=False) from exc
        try:
            after = self.registry.snapshot(entry, force=True)
            updated_table = entry.component.getTextTables().getByName(record.table_name)
            actual_rows, actual_columns, actual_values = self._table_shape_and_values(updated_table)
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "The table changed but its new structure could not be verified.", retryable=False) from exc
        if ((actual_rows, actual_columns, actual_values) != (new_rows, new_columns, expected_values)
                or after["table_count"] != before["table_count"]):
            raise WriterError("UNKNOWN_OUTCOME", "The table result did not match the preview; inspect the document.", retryable=False)
        before_non_target = [item for item in before["paragraphs"]
                             if item.get("name") != record.table_name]
        after_non_target = [item for item in after["paragraphs"]
                            if item.get("name") != record.table_name]
        if before_non_target != after_non_target:
            raise WriterError("UNKNOWN_OUTCOME", "The table edit changed unrelated document content.", retryable=False)
        entry.state.update(after["hash"])
        self._record_undo(entry, operation_id, action_label, operation, before, after)
        return {"document_id": entry.document_id, "table_name": record.table_name,
                "table_operation": operation, "rows": actual_rows, "columns": actual_columns,
                "revision": entry.state.revision, "snapshot_hash": after["hash"], "confirmed": True}

    def format_table_cell(self, document_id, table_anchor, cell_name, expected_revision,
                          expected_text, formatting_options, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_table_anchor(table_anchor)
        self._validate_text(cell_name)
        self._validate_text(expected_text)
        formatting = self._normalize_formatting(formatting_options)
        payload = {"tool": "format_table_cell", "document_id": document_id,
                   "table_anchor": table_anchor, "cell_name": cell_name,
                   "expected_revision": expected_revision, "expected_text": expected_text,
                   "formatting": formatting}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._format_table_cell(
                    desktop, document_id, table_anchor, cell_name, expected_revision,
                    expected_text, formatting, operation_id), mutating=True)))

    def _format_table_cell(self, desktop, document_id, table_anchor, cell_name,
                           expected_revision, expected_text, formatting, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        table, record = self.registry.resolve_table(entry, table_anchor, expected_revision)
        if cell_name not in record.cell_names:
            raise WriterError("INVALID_ANCHOR", "The cell_name does not belong to the anchored table.")
        cell = table.getCellByName(cell_name)
        if str(cell.getString()) != expected_text:
            raise WriterError("CONFLICT", "The table cell changed after the preview.")
        cursor = cell.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        with self._undo_context(entry.component, operation_id) as action_label:
            self._apply_formatting_cursor(cursor, formatting)
        after = self.registry.update_after_mutation(entry)
        after_table_record = next((item for item in after["paragraphs"]
                                   if item.get("name") == record.table_name), None)
        after_cells = dict(after_table_record.get("cells", ())) if after_table_record else {}
        after_formats = dict(after_table_record.get("cell_formatting", ())) if after_table_record else {}
        before_table_record = next(item for item in before["paragraphs"]
                                   if item.get("name") == record.table_name)
        before_cells = dict(before_table_record["cells"])
        before_formats = dict(before_table_record.get("cell_formatting", ()))
        unaffected = all(after_cells.get(name) == value for name, value in before_cells.items())
        unaffected = unaffected and all(after_formats.get(name) == value
                                        for name, value in before_formats.items() if name != cell_name)
        if (not unaffected or after_cells.get(cell_name) != expected_text
                or not self._formatting_matches(after_formats.get(cell_name, {}), formatting)):
            raise WriterError("UNKNOWN_OUTCOME", "The cell format or preservation could not be verified.", retryable=False)
        self._record_undo(entry, operation_id, action_label, "format_table_cell", before, after)
        return {"document_id": entry.document_id, "table_anchor": table_anchor,
                "cell_name": cell_name, "revision": entry.state.revision,
                "snapshot_hash": after["hash"], "formatting": dict(formatting), "confirmed": True}

    def _resolve_image_ref(self, entry, image_ref, expected_revision):
        if expected_revision != entry.state.revision:
            raise WriterError("CONFLICT", "The document revision changed before resolving the image.")
        snapshot = self.registry.snapshot(entry)
        for ordinal, record in enumerate(snapshot["objects"]):
            if record.get("kind") != "image":
                continue
            expected_ref = stable_hash([entry.document_id, expected_revision, ordinal, record])
            if expected_ref == image_ref:
                return entry.component.getGraphicObjects().getByIndex(int(record["index"])), record
        raise WriterError("INVALID_ANCHOR", "The image reference is stale or does not identify an image in this document.")

    def update_image(self, document_id, image_ref, expected_revision, expected_image_hash,
                     new_alt_text, width_mm, height_mm, alignment, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        if new_alt_text is not None:
            self._validate_text(new_alt_text)
            self._validate_inline_text(new_alt_text)
        width_mm, height_mm = self._validate_image_size(width_mm, height_mm)
        if alignment is not None and (not isinstance(alignment, str)
                                      or alignment not in {"left", "center", "right"}):
            raise WriterError("INVALID_ARGUMENT", "alignment must be left, center, or right.")
        if new_alt_text is None and width_mm is None and alignment is None:
            raise WriterError("INVALID_ARGUMENT", "Provide an alt text, size, or alignment change.")
        payload = {"tool": "update_image", "document_id": document_id,
                   "image_ref": image_ref, "expected_revision": expected_revision,
                   "image_record_hash": expected_image_hash, "alt_text": new_alt_text,
                   "width_mm": width_mm, "height_mm": height_mm, "alignment": alignment}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._update_image(
                    desktop, document_id, image_ref, expected_revision, expected_image_hash,
                    new_alt_text, width_mm, height_mm, alignment, operation_id), mutating=True)))

    def _update_image(self, desktop, document_id, image_ref, expected_revision,
                      expected_image_hash, new_alt_text, width_mm, height_mm,
                      alignment, operation_id):
        import uno
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        graphic, record = self._resolve_image_ref(entry, image_ref, expected_revision)
        if stable_hash(record) != expected_image_hash:
            raise WriterError("CONFLICT", "The image properties changed after preview.")
        with self._undo_context(entry.component, operation_id) as action_label:
            if new_alt_text is not None:
                graphic.setPropertyValue("Description", new_alt_text)
            if width_mm is not None:
                graphic.setPropertyValue("Width", width_mm * 100)
                graphic.setPropertyValue("Height", height_mm * 100)
            if alignment is not None:
                anchor = graphic.getAnchor()
                try:
                    anchor_text = anchor.getText()
                    anchor_cursor = anchor_text.createTextCursorByRange(anchor)
                    anchor_cursor.gotoStartOfParagraph(False)
                    anchor_cursor.ParaAdjust = uno.Enum("com.sun.star.style.ParagraphAdjust", {
                        "left": "LEFT", "center": "CENTER", "right": "RIGHT"}[alignment])
                except Exception as exc:
                    raise WriterError("INVALID_ANCHOR", "The image is not anchored in an editable Writer paragraph.") from exc
        after = self.registry.update_after_mutation(entry)
        image_records = [item for item in after["objects"] if item.get("kind") == "image"]
        if len(image_records) != sum(item.get("kind") == "image" for item in before["objects"]):
            raise WriterError("UNKNOWN_OUTCOME", "The image count changed during its update.", retryable=False)
        updated_graphic = entry.component.getGraphicObjects().getByIndex(int(record["index"]))
        if new_alt_text is not None and str(updated_graphic.getPropertyValue("Description")) != new_alt_text:
            raise WriterError("UNKNOWN_OUTCOME", "The image description could not be verified.", retryable=False)
        if width_mm is not None and (int(updated_graphic.getPropertyValue("Width")) != width_mm * 100
                                     or int(updated_graphic.getPropertyValue("Height")) != height_mm * 100):
            raise WriterError("UNKNOWN_OUTCOME", "The image dimensions could not be verified.", retryable=False)
        self._record_undo(entry, operation_id, action_label, "update_image", before, after)
        return {"document_id": entry.document_id, "image_ref": image_ref,
                "revision": entry.state.revision, "snapshot_hash": after["hash"],
                "alt_text": new_alt_text, "width_mm": width_mm,
                "height_mm": height_mm, "alignment": alignment, "confirmed": True}

    def apply_style(self, document_id, anchor, expected_revision, expected_text, style_name,
                    operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_paragraph_anchor(anchor)
        self._validate_text(expected_text)
        self._validate_text(style_name)
        self._validate_inline_text(style_name)
        payload = {"tool": "style", "document_id": document_id, "anchor": anchor, "expected_revision": expected_revision,
                   "expected_text": expected_text, "style_name": style_name}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._apply_style(desktop, document_id, anchor, expected_revision,
                                                         expected_text, style_name, operation_id), mutating=True)))

    def _apply_style(self, desktop, document_id, anchor, expected_revision, expected_text, style_name, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        self._validate_style(entry.component, style_name)
        cursor, _target, record = self.registry.paragraph_cursor(entry, anchor, expected_revision, expected_text)
        with self._undo_context(entry.component, operation_id) as action_label:
            cursor.ParaStyleName = style_name
        snapshot = self.registry.update_after_mutation(entry)
        self._record_undo(entry, operation_id, action_label, "apply_style", before, snapshot)
        confirmed = self._paragraph_at(entry.component, record.paragraph_index)
        if str(confirmed.getString()) != expected_text or str(confirmed.ParaStyleName) != style_name:
            raise WriterError("UNKNOWN_OUTCOME", "The style result could not be verified; inspect the document.", retryable=False)
        return {"document_id": document_id, "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                "style_name": style_name, "confirmed": True}

    def _normalize_formatting(self, options):
        import re
        if not isinstance(options, dict):
            raise WriterError("INVALID_ARGUMENT", "formatting must be an object of supported properties.")
        allowed = {"bold", "italic", "underline", "font_name", "font_size_pt", "font_color", "alignment"}
        if not options or set(options) - allowed:
            raise WriterError("INVALID_ARGUMENT", "Provide at least one supported formatting property and no unknown keys.")
        normalized = {}
        for name in ("bold", "italic", "underline"):
            if name in options:
                if not isinstance(options[name], bool):
                    raise WriterError("INVALID_ARGUMENT", f"{name} must be a boolean.")
                normalized[name] = options[name]
        if "font_name" in options:
            value = options["font_name"]
            self._validate_text(value)
            self._validate_inline_text(value)
            if not value.strip() or len(value) > 100:
                raise WriterError("INVALID_ARGUMENT", "font_name must contain 1 to 100 non-blank characters.")
            normalized["font_name"] = value.strip()
        if "font_size_pt" in options:
            value = options["font_size_pt"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 6 <= value <= 96:
                raise WriterError("INVALID_ARGUMENT", "font_size_pt must be a number from 6 to 96.")
            normalized["font_size_pt"] = float(value)
        if "font_color" in options:
            value = options["font_color"]
            if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
                raise WriterError("INVALID_ARGUMENT", "font_color must use #RRGGBB notation.")
            normalized["font_color"] = value.upper()
        if "alignment" in options:
            value = options["alignment"]
            if not isinstance(value, str) or value not in {"left", "center", "right", "justify"}:
                raise WriterError("INVALID_ARGUMENT", "alignment must be left, center, right, or justify.")
            normalized["alignment"] = value
        return normalized

    def apply_formatting(self, document_id, anchor, expected_revision, expected_text,
                         formatting_options, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_paragraph_anchor(anchor)
        self._validate_text(expected_text)
        formatting = self._normalize_formatting(formatting_options)
        payload = {"tool": "apply_formatting", "document_id": document_id,
                   "anchor": anchor, "expected_revision": expected_revision,
                   "expected_text": expected_text, "formatting": formatting}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._apply_formatting(
                    desktop, document_id, anchor, expected_revision, expected_text,
                    formatting, operation_id), mutating=True)))

    def _apply_formatting(self, desktop, document_id, anchor, expected_revision,
                          expected_text, formatting, operation_id):
        entry = self._entry(desktop, document_id)
        before = self._refresh_before_write(entry, expected_revision)
        cursor, _target, record = self.registry.paragraph_cursor(
            entry, anchor, expected_revision, expected_text)
        with self._undo_context(entry.component, operation_id) as action_label:
            self._apply_formatting_cursor(cursor, formatting)
        snapshot = self.registry.update_after_mutation(entry)
        self._record_undo(entry, operation_id, action_label, "apply_formatting", before, snapshot)
        paragraph_items = [item for item in snapshot["paragraphs"] if "text" in item]
        current = paragraph_items[record.paragraph_index] if record.paragraph_index < len(paragraph_items) else None
        if (current is None or str(current["text"]) != expected_text
                or not self._formatting_matches(current.get("formatting", {}), formatting)):
            raise WriterError("UNKNOWN_OUTCOME", "Formatting was applied but could not be verified; inspect the paragraph.", retryable=False)
        return {"document_id": document_id, "revision": entry.state.revision,
                "snapshot_hash": snapshot["hash"], "formatting": dict(formatting),
                "confirmed": True}

    @staticmethod
    def _apply_formatting_cursor(cursor, formatting):
        import uno
        if "bold" in formatting:
            cursor.CharWeight = uno.getConstantByName(
                "com.sun.star.awt.FontWeight.BOLD" if formatting["bold"]
                else "com.sun.star.awt.FontWeight.NORMAL")
        if "italic" in formatting:
            cursor.CharPosture = uno.Enum("com.sun.star.awt.FontSlant",
                                          "ITALIC" if formatting["italic"] else "NONE")
        if "underline" in formatting:
            cursor.CharUnderline = uno.getConstantByName(
                "com.sun.star.awt.FontUnderline.SINGLE" if formatting["underline"]
                else "com.sun.star.awt.FontUnderline.NONE")
        if "font_name" in formatting:
            cursor.CharFontName = formatting["font_name"]
        if "font_size_pt" in formatting:
            cursor.CharHeight = formatting["font_size_pt"]
        if "font_color" in formatting:
            cursor.CharColor = int(formatting["font_color"].lstrip("#"), 16)
        if "alignment" in formatting:
            alignment = {"left": "LEFT", "center": "CENTER",
                         "right": "RIGHT", "justify": "BLOCK"}[formatting["alignment"]]
            cursor.ParaAdjust = uno.Enum("com.sun.star.style.ParagraphAdjust", alignment)

    @staticmethod
    def _formatting_matches(current, desired):
        def contains(value, word):
            return word in str(value).upper()
        checks = {
            "bold": lambda: float(current.get("CharWeight", 0)) >= 150 if desired["bold"] else float(current.get("CharWeight", 0)) < 150,
            "italic": lambda: contains(current.get("CharPosture", ""), "ITALIC") if desired["italic"] else contains(current.get("CharPosture", ""), "NONE"),
            "underline": lambda: int(current.get("CharUnderline", 0)) > 0 if desired["underline"] else int(current.get("CharUnderline", 0)) == 0,
            "font_name": lambda: current.get("CharFontName") == desired["font_name"],
            "font_size_pt": lambda: abs(float(current.get("CharHeight", 0)) - desired["font_size_pt"]) < 0.1,
            "font_color": lambda: int(current.get("CharColor", -1)) == int(desired["font_color"].lstrip("#"), 16),
            "alignment": lambda: contains(current.get("ParaAdjust", ""), {
                "left": "LEFT", "center": "CENTER", "right": "RIGHT", "justify": "BLOCK"
            }[desired["alignment"]]),
        }
        for name in desired:
            try:
                if not checks[name]():
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def save_document(self, document_id, expected_revision, expected_path, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_text(expected_path)
        payload = {"tool": "save", "document_id": document_id,
                   "expected_revision": expected_revision, "expected_path": expected_path}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._save_document(desktop, document_id, expected_revision, expected_path), mutating=True)))

    def _save_document(self, desktop, document_id, expected_revision, expected_path):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        component = entry.component
        if self._readonly(component):
            raise WriterError("PERMISSION_DENIED", "The document is read-only.")
        try:
            path = self._allowed_save_path(component)
            if str(path) != expected_path:
                raise WriterError("CONFLICT", "The document path changed after the save preview.")
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

    _SAVE_FILTERS = {"odt": ("writer8", ".odt"),
                     "docx": ("Office Open XML Text", ".docx"),
                     "rtf": ("Rich Text Format", ".rtf"),
                     "txt": ("Text", ".txt")}

    def _save_as_target(self, target_path, file_format, overwrite):
        if not isinstance(target_path, str) or not target_path.strip():
            raise WriterError("INVALID_ARGUMENT", "target_path must be a non-empty absolute local path.")
        if not isinstance(file_format, str) or file_format not in self._SAVE_FILTERS:
            raise WriterError("INVALID_ARGUMENT", "file_format must be one of odt, docx, rtf, or txt.")
        if not isinstance(overwrite, bool):
            raise WriterError("INVALID_ARGUMENT", "overwrite must be a boolean.")
        raw = Path(target_path).expanduser()
        if not raw.is_absolute():
            raise WriterError("INVALID_ARGUMENT", "target_path must be absolute.")
        try:
            path = raw.resolve(strict=False)
            root_allowed = any(
                self._path_within_roots(path.parent, (root,))
                for root in self.settings.allowed_save_roots
            )
            if not root_allowed:
                raise WriterError("PERMISSION_DENIED", "The destination is outside YBERU_WRITER_ALLOWED_SAVE_ROOTS.")
            if not path.parent.is_dir():
                raise WriterError("INVALID_ARGUMENT", "The destination directory must already exist.")
            expected_filter, suffix = self._SAVE_FILTERS[file_format]
            if path.suffix.lower() != suffix:
                raise WriterError("INVALID_ARGUMENT", "The destination extension must match file_format.")
            if path.exists():
                if not path.is_file():
                    raise WriterError("INVALID_ARGUMENT", "The destination must be a regular file.")
                if not overwrite:
                    raise WriterError("CONFLICT", "The destination already exists; preview again with overwrite=true to replace it.")
                target_hash = self._hash_file(path)
            else:
                target_hash = None
            return path, expected_filter, target_hash
        except WriterError:
            raise
        except OSError as exc:
            raise WriterError("INVALID_ARGUMENT", "Could not inspect the destination path.") from exc

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def save_as(self, document_id, expected_revision, target_path, file_format,
                overwrite, expected_target_hash, operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        path, filter_name, current_hash = self._save_as_target(target_path, file_format, overwrite)
        if current_hash != expected_target_hash:
            raise WriterError("CONFLICT", "The destination changed after the save-as preview.")
        payload = {"tool": "save_as", "document_id": document_id,
                   "expected_revision": expected_revision, "target_path": str(path),
                   "file_format": file_format, "filter_name": filter_name,
                   "overwrite": overwrite, "target_hash": expected_target_hash}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._save_as_document(
                    desktop, document_id, expected_revision, path, file_format,
                    filter_name, overwrite, expected_target_hash), mutating=True)))

    def _save_as_document(self, desktop, document_id, expected_revision, path,
                          file_format, filter_name, overwrite, expected_target_hash):
        entry = self._entry(desktop, document_id)
        self._refresh_before_write(entry, expected_revision)
        if self._readonly(entry.component):
            raise WriterError("PERMISSION_DENIED", "The document is read-only.")
        try:
            checked_path, checked_filter, current_hash = self._save_as_target(
                str(path), file_format, overwrite)
            if checked_path != path or checked_filter != filter_name or current_hash != expected_target_hash:
                raise WriterError("CONFLICT", "The destination or its contents changed after the save-as preview.")
            import uno
            filter_property = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            filter_property.Name, filter_property.Value = "FilterName", filter_name
            overwrite_property = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            overwrite_property.Name, overwrite_property.Value = "Overwrite", overwrite
            entry.component.storeAsURL(uno.systemPathToFileUrl(str(path)),
                                       (filter_property, overwrite_property))
            result_path = Path(uno.fileUrlToSystemPath(entry.component.getURL())).resolve(strict=True)
            if result_path != path or not result_path.is_file() or bool(entry.component.isModified()):
                raise WriterError("UNKNOWN_OUTCOME", "Writer saved the document but the resulting path/state could not be verified.", retryable=False)
            snapshot = self.registry.update_after_mutation(entry)
            return {"document_id": entry.document_id, "path": str(result_path),
                    "file_format": file_format, "revision": entry.state.revision,
                    "snapshot_hash": snapshot["hash"], "saved": True, "confirmed": True}
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "Save As may have completed; inspect the destination and document before retrying.",
                              retryable=False, details={"exception": type(exc).__name__}) from exc

    def _path_allowed(self, path: Path):
        return self._path_within_roots(path, self.settings.allowed_save_roots)

    @staticmethod
    def _path_within_roots(path: Path, roots):
        for root in roots:
            try:
                path.relative_to(root.resolve(strict=True))
                return True
            except (ValueError, OSError):
                pass
        return False

    def _read_allowed_image(self, image_path):
        if not isinstance(image_path, str) or not image_path.strip():
            raise WriterError("INVALID_ARGUMENT", "image_path must be a local file path.")
        try:
            path = Path(image_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WriterError("INVALID_ARGUMENT", "The image file does not exist or cannot be resolved.") from exc
        if not path.is_file():
            raise WriterError("INVALID_ARGUMENT", "image_path must point to a regular image file.")
        if not self.settings.allowed_image_roots or not self._path_within_roots(path, self.settings.allowed_image_roots):
            raise WriterError("PERMISSION_DENIED", "The image path is outside YBERU_WRITER_ALLOWED_IMAGE_ROOTS.")
        suffix = path.suffix.lower()
        signatures = {
            ".png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
            ".jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
            ".gif": lambda data: data[:6] in {b"GIF87a", b"GIF89a"},
            ".bmp": lambda data: data.startswith(b"BM"),
        }
        if suffix not in signatures:
            raise WriterError("INVALID_ARGUMENT", "Image format must be PNG, JPEG, GIF, or BMP.")
        try:
            size = path.stat().st_size
            if size <= 0 or size > self.settings.max_image_bytes:
                raise WriterError("SIZE_LIMIT", f"Image must be between 1 and {self.settings.max_image_bytes} bytes.")
            content = path.read_bytes()
        except WriterError:
            raise
        except OSError as exc:
            raise WriterError("PERMISSION_DENIED", "The image file could not be read.") from exc
        if len(content) != size or not signatures[suffix](content):
            raise WriterError("INVALID_ARGUMENT", "The image file content does not match its supported format.")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        return path, content, digest

    def _read_allowed_template(self, template_path):
        import zipfile
        if not isinstance(template_path, str) or not template_path.strip():
            raise WriterError("INVALID_ARGUMENT", "template_path must be a local ODT or OTT file path.")
        try:
            path = Path(template_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WriterError("INVALID_ARGUMENT", "The template file does not exist or cannot be resolved.") from exc
        if not path.is_file() or path.suffix.lower() not in {".odt", ".ott"}:
            raise WriterError("INVALID_ARGUMENT", "Templates must be regular .odt or .ott files.")
        if (not self.settings.allowed_template_roots
                or not self._path_within_roots(path, self.settings.allowed_template_roots)):
            raise WriterError("PERMISSION_DENIED", "The template path is outside YBERU_WRITER_ALLOWED_TEMPLATE_ROOTS.")
        try:
            size = path.stat().st_size
            if size <= 0 or size > self.settings.max_template_bytes:
                raise WriterError("SIZE_LIMIT", f"Template must be between 1 and {self.settings.max_template_bytes} bytes.")
            with zipfile.ZipFile(path) as package:
                uncompressed = sum(info.file_size for info in package.infolist())
                if uncompressed > self.settings.max_template_bytes * 5:
                    raise WriterError("SIZE_LIMIT", "The uncompressed template package exceeds the safety limit.")
                mime_type = package.read("mimetype").decode("ascii")
        except WriterError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
            raise WriterError("INVALID_ARGUMENT", "The template is not a valid OpenDocument package.") from exc
        expected_mime = ("application/vnd.oasis.opendocument.text" if path.suffix.lower() == ".odt"
                         else "application/vnd.oasis.opendocument.text-template")
        if mime_type != expected_mime:
            raise WriterError("INVALID_ARGUMENT", "The file extension and OpenDocument package type do not match.")
        return path, self._hash_file(path), mime_type

    def _normalize_table(self, rows, columns, cell_values):
        if (isinstance(rows, bool) or not isinstance(rows, int) or rows < 1 or rows > 100
                or isinstance(columns, bool) or not isinstance(columns, int) or columns < 1 or columns > 100):
            raise WriterError("INVALID_ARGUMENT", "rows and columns must be integers from 1 to 100.")
        if rows * columns > self.settings.max_table_cells:
            raise WriterError("SIZE_LIMIT", f"A table may contain at most {self.settings.max_table_cells} cells.")
        if cell_values is None:
            values = [["" for _ in range(columns)] for _ in range(rows)]
        else:
            if not isinstance(cell_values, list) or len(cell_values) != rows:
                raise WriterError("INVALID_ARGUMENT", "cell_values must contain exactly one list for each row.")
            values = []
            total_chars = 0
            for row in cell_values:
                if not isinstance(row, list) or len(row) != columns:
                    raise WriterError("INVALID_ARGUMENT", "Every cell_values row must contain exactly the requested number of columns.")
                normalized_row = []
                for value in row:
                    self._validate_text(value)
                    self._validate_inline_text(value)
                    total_chars += len(value)
                    normalized_row.append(value)
                values.append(normalized_row)
            if total_chars > self.settings.max_text_chars:
                raise WriterError("SIZE_LIMIT", "The combined table text exceeds the configured text limit.")
        return rows, columns, values

    @staticmethod
    def _validate_image_size(width_mm, height_mm):
        if width_mm is None and height_mm is None:
            return None, None
        if (isinstance(width_mm, bool) or not isinstance(width_mm, int)
                or isinstance(height_mm, bool) or not isinstance(height_mm, int)
                or not 1 <= width_mm <= 500 or not 1 <= height_mm <= 500):
            raise WriterError("INVALID_ARGUMENT", "Specify both width_mm and height_mm as integers from 1 to 500.")
        return width_mm, height_mm

    def _execute_confirmed(self, token, payload, action):
        self.confirmations.consume(token, payload)
        return action()

    @contextmanager
    def _undo_context(self, component, operation_id):
        try:
            canonical_id = str(uuid.UUID(operation_id))
            manager = component.getUndoManager()
            label = f"Yberu Writer MCP {canonical_id}"
            manager.enterUndoContext(label)
        except Exception as exc:
            raise WriterError("UNDO_UNAVAILABLE", "Writer could not create an undo context for this change.") from exc
        try:
            yield label
        finally:
            manager.leaveUndoContext()

    def _record_undo(self, entry, operation_id, action_label, change_type, before, after):
        self._undo_records[entry.document_id] = UndoRecord(
            document_id=entry.document_id,
            operation_id=str(uuid.UUID(operation_id)),
            action_label=action_label,
            change_type=change_type,
            before_hash=before["hash"],
            after_hash=after["hash"],
            revision=entry.state.revision,
        )

    def undo_last_change(self, document_id, expected_revision, target_operation_id,
                         operation_id, confirmation_token):
        self._validate_revision(expected_revision)
        self._validate_text(target_operation_id)
        payload = {"tool": "undo", "document_id": document_id,
                   "expected_revision": expected_revision,
                   "target_operation_id": target_operation_id}
        return self.ledger.execute(operation_id, payload, lambda: self._execute_confirmed(
            confirmation_token, payload, lambda: self._run(
                lambda _ctx, desktop: self._undo_last_change(desktop, document_id, expected_revision,
                                                             target_operation_id), mutating=True)))

    def _undo_last_change(self, desktop, document_id, expected_revision, target_operation_id):
        entry = self._entry(desktop, document_id)
        current = self._refresh_before_write(entry, expected_revision)
        record = self._undo_records.get(entry.document_id)
        if (record is None or record.operation_id != target_operation_id
                or record.revision != expected_revision or record.after_hash != current["hash"]):
            raise WriterError("UNDO_NOT_AVAILABLE", "The requested Yberu MCP action is no longer the latest reversible change.")
        try:
            manager = entry.component.getUndoManager()
            title = str(manager.getCurrentUndoActionTitle())
            if not manager.isUndoPossible() or title != record.action_label:
                raise WriterError("CONFLICT", "The next Writer undo action is not the latest Yberu MCP edit.",
                                  details={"expected_action": record.action_label, "current_action": title})
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("UNDO_UNAVAILABLE", "Could not safely inspect the Writer undo stack.") from exc
        del self._undo_records[entry.document_id]
        try:
            manager.undo()
            snapshot = self.registry.snapshot(entry, force=True)
            entry.state.update(snapshot["hash"])
        except Exception as exc:
            raise WriterError("UNKNOWN_OUTCOME", "Undo may have run; inspect the document before retrying.", retryable=False) from exc
        if snapshot["hash"] != record.before_hash:
            raise WriterError("UNKNOWN_OUTCOME", "Undo completed but the previous document snapshot was not restored.", retryable=False)
        return {"document_id": entry.document_id, "undone_operation_id": record.operation_id,
                "revision": entry.state.revision, "snapshot_hash": snapshot["hash"],
                "confirmed": True, "undo_action": record.action_label}

    def _validate_text(self, value):
        if not isinstance(value, str):
            raise WriterError("INVALID_ARGUMENT", "Text parameters must be strings.")
        if len(value) > self.settings.max_text_chars:
            raise WriterError("SIZE_LIMIT", f"Text exceeds the {self.settings.max_text_chars} character limit.")
        if "\x00" in value:
            raise WriterError("INVALID_ARGUMENT", "Text must not contain NUL characters.")

    def _normalize_document_spec(self, title, initial_paragraphs):
        self._validate_text(title)
        title = title.strip() or "Untitled Writer document"
        if len(title) > 255 or "\n" in title or "\r" in title:
            raise WriterError("INVALID_ARGUMENT", "Document title must be a single line of at most 255 characters.")
        if initial_paragraphs is None:
            paragraphs = []
        else:
            if not isinstance(initial_paragraphs, list) or len(initial_paragraphs) > 500:
                raise WriterError("INVALID_ARGUMENT", "initial_paragraphs must be a list containing at most 500 paragraphs.")
            paragraphs = list(initial_paragraphs)
        total_chars = 0
        for paragraph in paragraphs:
            self._validate_text(paragraph)
            self._validate_inline_text(paragraph)
            total_chars += len(paragraph)
        if total_chars > self.settings.max_text_chars:
            raise WriterError("SIZE_LIMIT", "The initial document text exceeds the configured text limit.")
        return title, paragraphs

    def _normalize_compose_spec(self, title, blocks):
        title, _ = self._normalize_document_spec(title, [])
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 100:
            raise WriterError("INVALID_ARGUMENT", "blocks must be a list containing from 1 to 100 items.")
        normalized = []
        total_chars = 0
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise WriterError("INVALID_ARGUMENT", "Every block must be an object with a supported type.")
            kind = block["type"]
            if kind in {"paragraph", "heading"}:
                expected_keys = {"type", "text"} | ({"level"} if kind == "heading" else set())
                if set(block) != expected_keys:
                    raise WriterError("INVALID_ARGUMENT", f"{kind} blocks require exactly {sorted(expected_keys)}.")
                value = block.get("text")
                self._validate_text(value)
                self._validate_inline_text(value)
                total_chars += len(value)
                item = {"type": kind, "text": value}
                if kind == "heading":
                    level = block.get("level")
                    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 10:
                        raise WriterError("INVALID_ARGUMENT", "heading level must be an integer from 1 to 10.")
                    item["level"] = level
                normalized.append(item)
            elif kind == "table":
                if set(block) != {"type", "rows"} or not isinstance(block.get("rows"), list) or not block["rows"]:
                    raise WriterError("INVALID_ARGUMENT", "table blocks require a non-empty rows matrix.")
                rows = block["rows"]
                columns = len(rows[0]) if isinstance(rows[0], list) else 0
                row_count, column_count, values = self._normalize_table(len(rows), columns, rows)
                total_chars += sum(len(value) for row in values for value in row)
                normalized.append({"type": "table", "rows": row_count,
                                   "columns": column_count, "cell_values": values})
            elif kind == "image":
                allowed_keys = {"type", "image_path", "alt_text", "width_mm", "height_mm"}
                if not {"type", "image_path"}.issubset(block) or set(block) - allowed_keys:
                    raise WriterError("INVALID_ARGUMENT", "image blocks require image_path and accept alt_text, width_mm, height_mm.")
                alt_text = block.get("alt_text", "")
                self._validate_text(alt_text)
                self._validate_inline_text(alt_text)
                width_mm, height_mm = self._validate_image_size(block.get("width_mm"), block.get("height_mm"))
                image_path, _content, image_hash = self._read_allowed_image(block["image_path"])
                total_chars += len(alt_text)
                normalized.append({"type": "image", "image_path": str(image_path),
                                   "image_hash": image_hash, "alt_text": alt_text,
                                   "width_mm": width_mm, "height_mm": height_mm})
            else:
                raise WriterError("INVALID_ARGUMENT", "Supported block types are paragraph, heading, table, and image.")
            if total_chars > self.settings.max_text_chars:
                raise WriterError("SIZE_LIMIT", "Combined text in the composition exceeds the configured text limit.")
        return title, normalized

    @staticmethod
    def _validate_revision(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise WriterError("INVALID_ARGUMENT", "expected_revision must be a positive integer.")

    @staticmethod
    def _validate_anchor(value):
        if not isinstance(value, str) or value[:2] not in {"p_", "r_"} or len(value) != 34:
            raise WriterError("INVALID_ANCHOR", "anchor must be an opaque paragraph or text-range anchor returned by a read tool.")

    @staticmethod
    def _validate_paragraph_anchor(value):
        if not isinstance(value, str) or not value.startswith("p_") or len(value) != 34:
            raise WriterError("INVALID_ANCHOR", "anchor must be an opaque paragraph anchor returned by writer_read_structure.")

    @staticmethod
    def _validate_table_anchor(value):
        if not isinstance(value, str) or not value.startswith("t_") or len(value) != 34:
            raise WriterError("INVALID_ANCHOR", "table_anchor must be returned by writer_read_structure.")

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
