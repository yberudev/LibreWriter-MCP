"""Application service coordinating UNO, revisions, and safe document operations."""

from __future__ import annotations

import copy
import logging
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
                                     "save_existing_document": bool(self.settings.allowed_save_roots),
                                     "read_text_range": True, "read_table": True, "replace_table_cell": True,
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
                                     "save_existing_document": False, "read_text_range": False, "read_table": False,
                                     "replace_table_cell": False, "events_internal": True,
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
                       new_content=None, style_name=None, table_anchor=None, cell_name=None):
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
        elif change_type == "save_document":
            payload = {"tool": "save", "document_id": document_id,
                       "expected_revision": expected_revision}
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
            table_anchor=table_anchor, cell_name=cell_name))
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
        if change_type == "save_document":
            path = self._allowed_save_path(entry.component)
            payload["expected_path"] = str(path)
            return {**common, "path": str(path), "modified": bool(entry.component.isModified()),
                    "will_write_file": bool(entry.component.isModified()), "overwrite_existing_file": True}
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
                raise WriterError("PERMISSION_DENIED", "Save is limited to existing local file documents; Save As is not supported.")
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
            remaining = self.settings.max_read_chars
            for cell_name in record.cell_names[:self.settings.max_structure_items]:
                value = str(table.getCellByName(cell_name).getString())
                total_chars += len(value)
                preview = value[:remaining]
                remaining = max(0, remaining - len(value))
                cell = {"name": cell_name, "text": preview, "text_truncated": len(preview) < len(value),
                        "expected_hash": stable_hash(value), "length": len(value)}
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

    def _path_allowed(self, path: Path):
        for root in self.settings.allowed_save_roots:
            try:
                path.relative_to(root.resolve(strict=True))
                return True
            except (ValueError, OSError):
                pass
        return False

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
