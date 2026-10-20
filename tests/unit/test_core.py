from __future__ import annotations

import threading
import time
import unittest
import uuid

from yberu_writer_mcp.errors import WriterError
from yberu_writer_mcp.events import EventQueue
from yberu_writer_mcp.revisions import IdempotencyLedger, RevisionState
from yberu_writer_mcp.server import build_server


class CoreTests(unittest.TestCase):
    def test_revision_increments_only_when_snapshot_changes(self):
        state = RevisionState()
        self.assertFalse(state.update("a"))
        self.assertEqual(state.revision, 1)
        self.assertFalse(state.update("a"))
        self.assertEqual(state.revision, 1)
        self.assertTrue(state.update("b"))
        self.assertEqual(state.revision, 2)

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

    def test_mcp_sdk_registers_nine_tools(self):
        from yberu_writer_mcp.config import Settings
        server, service = build_server(Settings(connect_timeout=0.05))
        try:
            tools = server._tool_manager.list_tools()
            self.assertEqual(len(tools), 9)
            self.assertIn("writer_replace_range", {item.name for item in tools})
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
