from __future__ import annotations

import threading
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from yberu_writer_mcp.errors import WriterError
from yberu_writer_mcp.events import EventQueue
from yberu_writer_mcp.documents import DocumentEntry, DocumentRegistry
from yberu_writer_mcp.confirmations import ConfirmationStore
from yberu_writer_mcp.revisions import IdempotencyLedger, RevisionState
from yberu_writer_mcp.server import build_server


class FakeParagraph:
    def __init__(self, text):
        self.text = text
        self.ParaStyleName = "Text Body"
        self.ParaOutlineLevel = 0

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def getString(self):
        return self.text


class FakeEnumeration:
    def __init__(self, items):
        self.items = iter(items)
        self.next_item = None
        self._advance()

    def _advance(self):
        self.next_item = next(self.items, None)

    def hasMoreElements(self):
        return self.next_item is not None

    def nextElement(self):
        item = self.next_item
        self._advance()
        return item


class FakeText:
    def __init__(self, paragraph):
        self.paragraph = paragraph

    def createEnumeration(self):
        return FakeEnumeration([self.paragraph])


class FakeComponent:
    def __init__(self, paragraph):
        self.text = FakeText(paragraph)

    def getText(self):
        return self.text


class CoreTests(unittest.TestCase):
    def test_revision_increments_only_when_snapshot_changes(self):
        state = RevisionState()
        self.assertFalse(state.update("a"))
        self.assertEqual(state.revision, 1)
        self.assertFalse(state.update("a"))
        self.assertEqual(state.revision, 1)
        self.assertTrue(state.update("b"))
        self.assertEqual(state.revision, 2)

    def test_document_snapshot_cache_is_invalidated_by_events_or_forced_refresh(self):
        paragraph = FakeParagraph("before")
        entry = DocumentEntry("doc-test", FakeComponent(paragraph), 1)
        queue = EventQueue(lambda _record: None, debounce_seconds=0.01)
        registry = DocumentRegistry(queue, max_snapshot_chars=100)
        try:
            original = registry.snapshot(entry)
            self.assertIs(registry.snapshot(entry), original)
            paragraph.text = "changed by Writer"
            self.assertIs(registry.snapshot(entry), original)
            queue.enqueue(entry.document_id, entry.generation, "modified")
            refreshed = registry.snapshot(entry)
            self.assertNotEqual(refreshed["hash"], original["hash"])
            paragraph.text = "changed without a listener"
            forced = registry.snapshot(entry, force=True)
            self.assertNotEqual(forced["hash"], refreshed["hash"])
        finally:
            queue.close()

    def test_document_slice_stream_preserves_paragraph_and_table_separators(self):
        from yberu_writer_mcp.session import WriterService
        segments = iter(("First", "Second", "cell-a", "cell-b", ""))
        value, total = WriterService._slice_segments(segments, 3, 17)
        whole = "First\nSecond\ncell-a\ncell-b\n"
        self.assertEqual(total, len(whole))
        self.assertEqual(value, whole[3:20])

    def test_idempotency_returns_completed_result_and_rejects_key_reuse(self):
        ledger = IdempotencyLedger()
        opid = str(uuid.uuid4())
        calls = []
        action = lambda: calls.append("called") or {"ok": True}
        self.assertEqual(ledger.execute(opid, {"x": 1}, action), {"ok": True})
        self.assertEqual(ledger.execute(opid, {"x": 1}, action), {"ok": True})
        self.assertEqual(calls, ["called"])
        with self.assertRaises(WriterError) as caught:
            ledger.execute(opid, {"x": 2}, action)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_REUSED")

    def test_uncertain_outcome_is_not_replayed(self):
        ledger = IdempotencyLedger()
        opid = str(uuid.uuid4())
        def action():
            raise WriterError("UNKNOWN_OUTCOME", "uncertain")
        with self.assertRaises(WriterError):
            ledger.execute(opid, {"x": 1}, action)
        with self.assertRaises(WriterError) as caught:
            ledger.execute(opid, {"x": 1}, lambda: self.fail("must not run again"))
        self.assertEqual(caught.exception.code, "UNKNOWN_OUTCOME")

    def test_confirmation_tokens_bind_exact_payload_and_are_one_use(self):
        store = ConfirmationStore()
        payload = {"tool": "replace", "document_id": "doc", "new_content": "safe"}
        token, ttl = store.issue(payload)
        self.assertGreater(ttl, 0)
        with self.assertRaises(WriterError) as mismatch:
            store.consume(token, {**payload, "new_content": "different"})
        self.assertEqual(mismatch.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(WriterError) as replay:
            store.consume(token, payload)
        self.assertEqual(replay.exception.code, "CONFIRMATION_REQUIRED")
        second, _ = store.issue(payload)
        store.consume(second, payload)
        with self.assertRaises(WriterError) as reused:
            store.consume(second, payload)
        self.assertEqual(reused.exception.code, "CONFIRMATION_REQUIRED")

    def test_event_queue_assigns_sequence_and_coalesces(self):
        records = []
        ready = threading.Event()
        def sink(record):
            records.append(record)
            ready.set()
        queue = EventQueue(sink, debounce_seconds=0.03)
        try:
            queue.enqueue("doc", 1, "modified")
            queue.enqueue("doc", 1, "selection")
            self.assertTrue(ready.wait(1))
            time.sleep(0.05)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].sequence, 1)
            self.assertEqual(set(records[0].kinds), {"modified", "selection"})
        finally:
            queue.close()

    def test_mcp_sdk_registers_eighteen_tools(self):
        from yberu_writer_mcp.config import Settings
        server, service = build_server(Settings(connect_timeout=0.05))
        try:
            tools = server._tool_manager.list_tools()
            names = {item.name for item in tools}
            self.assertEqual(len(tools), 18)
            by_name = {item.name: item for item in tools}
            self.assertIn("writer_replace_range", names)
            self.assertIn("writer_read_text_range", names)
            self.assertIn("writer_read_table", names)
            self.assertIn("writer_replace_table_cell", names)
            self.assertIn("writer_preview_change", names)
            self.assertIn("writer_undo_last_change", names)
            self.assertIn("writer_preview_create_document", names)
            self.assertIn("writer_create_document", names)
            self.assertIn("writer_insert_table", names)
            self.assertIn("writer_insert_image", names)
            self.assertTrue(by_name["writer_get_state"].annotations.read_only_hint)
            self.assertTrue(by_name["writer_preview_change"].annotations.read_only_hint)
            self.assertTrue(by_name["writer_preview_create_document"].annotations.read_only_hint)
            self.assertFalse(by_name["writer_insert_text"].annotations.read_only_hint)
            self.assertFalse(by_name["writer_create_document"].annotations.read_only_hint)
            self.assertTrue(by_name["writer_replace_range"].annotations.destructive_hint)
            self.assertTrue(by_name["writer_save_document"].annotations.destructive_hint)
        finally:
            service.close()

    def test_document_table_and_image_arguments_are_bounded_and_allowlisted(self):
        from yberu_writer_mcp.config import Settings
        from yberu_writer_mcp.session import WriterService
        with tempfile.TemporaryDirectory(prefix="yberu-image-allow-") as allowed_dir:
            image_path = Path(allowed_dir) / "pixel.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            service = WriterService(Settings(allowed_image_roots=(Path(allowed_dir),), max_image_bytes=100))
            try:
                title, paragraphs = service._normalize_document_spec("  Draft  ", ["Heading", "Body"])
                self.assertEqual(title, "Draft")
                self.assertEqual(paragraphs, ["Heading", "Body"])
                rows, columns, cells = service._normalize_table(2, 2, [["A", "B"], ["C", "D"]])
                self.assertEqual((rows, columns), (2, 2))
                self.assertEqual(cells[1][1], "D")
                resolved, data, digest = service._read_allowed_image(str(image_path))
                self.assertEqual(resolved, image_path.resolve())
                self.assertEqual(data, image_path.read_bytes())
                self.assertTrue(digest.startswith("sha256:"))
                with self.assertRaises(WriterError) as too_many:
                    service._normalize_table(20, 30, None)
                self.assertEqual(too_many.exception.code, "SIZE_LIMIT")
                with tempfile.TemporaryDirectory(prefix="yberu-image-denied-") as outside_dir:
                    outside = Path(outside_dir) / "outside.png"
                    outside.write_bytes(image_path.read_bytes())
                    with self.assertRaises(WriterError) as denied:
                        service._read_allowed_image(str(outside))
                    self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
