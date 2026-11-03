"""Opt-in end-to-end tests against a private graphical Writer and a real MCP stdio client."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import importlib.util
import shutil
from pathlib import Path

try:
    import uno
except ModuleNotFoundError:
    uno = None
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.subscriptions import listen
from mcp.shared.subscriptions import ResourceUpdated

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
RUN = (os.environ.get("YBERU_RUN_LIVE_TESTS") == "1" and bool(os.environ.get("DISPLAY"))
       and uno is not None and importlib.util.find_spec("Xlib") is not None and shutil.which("wmctrl") is not None)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def launch_writer(port, profile):
    command = [str(PYTHON), str(ROOT / "scripts/launch_writer.py"), "--port", str(port),
               "--profile", str(profile), "--wait", "30", "--json"]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=35)
    if result.returncode:
        try:
            stop_owned_writer(json.loads(result.stdout)["pid"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        raise RuntimeError(f"Writer launcher failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)["pid"]


def stop_owned_writer(pid):
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def connect_writer(port):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    return resolver.resolve(f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")


def create_writer_doc(desktop, title, text):
    doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
    doc.getDocumentProperties().Title = title
    parts = text.split("\n")
    doc.getText().setString(parts[0])
    cursor = doc.getText().createTextCursor()
    cursor.gotoEnd(False)
    for part in parts[1:]:
        doc.getText().insertControlCharacter(cursor, uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK"), False)
        doc.getText().insertString(cursor, part, False)
    doc.setModified(False)
    return doc


def property_value(name, value):
    item = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    item.Name, item.Value = name, value
    return item


def own_writer_window(process_group_id):
    result = subprocess.run(["wmctrl", "-lp"], check=True, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5 or "LibreOffice Writer" not in fields[4]:
            continue
        try:
            if os.getpgid(int(fields[2])) == process_group_id:
                return fields[0]
        except (ProcessLookupError, PermissionError, ValueError):
            continue
    raise RuntimeError("Could not identify the private Writer test window")


def type_ascii(text):
    from Xlib import X, XK
    from Xlib.display import Display
    from Xlib.ext import xtest
    display = Display()
    try:
        for character in text:
            keycode = display.keysym_to_keycode(XK.string_to_keysym(character))
            if not keycode:
                raise ValueError(f"No X11 keycode for {character!r}")
            xtest.fake_input(display, X.KeyPress, detail=keycode)
            xtest.fake_input(display, X.KeyRelease, detail=keycode)
        display.sync()
    finally:
        display.close()


async def call_tool(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    if result.is_error:
        message = result.content[0].text if result.content else "tool error"
        raise AssertionError(f"{name} failed: {message}")
    return json.loads(result.content[0].text)


@unittest.skipUnless(RUN, "requires YBERU_RUN_LIVE_TESTS=1, DISPLAY, wmctrl, and Python Xlib")
class LiveWriterTests(unittest.TestCase):
    def test_writer_mcp_end_to_end(self):
        port = free_port()
        with tempfile.TemporaryDirectory(prefix="yberu-mcp-live-") as temporary:
            root = Path(temporary)
            profile = root / "lo-profile"
            pid = launch_writer(port, profile)
            owned_pid = [pid]
            documents = []
            try:
                context = connect_writer(port)
                desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
                # Launcher test session only; these temporary documents are never saved.
                first = create_writer_doc(desktop, "Yberu Integration First", "First paragraph.\nSecond paragraph.")
                second = create_writer_doc(desktop, "Yberu Integration Second", "Independent document.")
                documents.extend([first, second])
                save_path = root / "allowed-save.odt"
                second.storeAsURL(uno.systemPathToFileUrl(str(save_path)), (property_value("FilterName", "writer8"),))
                second.setModified(False)
                table = first.createInstance("com.sun.star.text.TextTable")
                table.initialize(1, 2)
                cursor = first.getText().createTextCursor()
                cursor.gotoEnd(False)
                first.getText().insertTextContent(cursor, table, False)
                table.getCellByName("A1").setString("keep-cell")
                table.getCellByName("B1").setString("keep-value")
                bookmark = first.createInstance("com.sun.star.text.Bookmark")
                bookmark.Name = "YberuMcpBookmark"
                bookmark_cursor = first.getText().createTextCursor()
                bookmark_cursor.gotoStart(False)
                bookmark_cursor.goRight(5, True)
                first.getText().insertTextContent(bookmark_cursor, bookmark, True)
                page_style = first.getStyleFamilies().getByName("PageStyles").getByName("Default Page Style")
                page_style.setPropertyValue("HeaderIsOn", True)
                page_style.getPropertyValue("HeaderText").setString("Private test header")
                frame = first.createInstance("com.sun.star.text.TextFrame")
                frame.setPropertyValue("Width", 3000)
                frame.setPropertyValue("Height", 1000)
                frame.setName("YberuMcpFrame")
                frame_cursor = first.getText().createTextCursor()
                frame_cursor.gotoEnd(False)
                first.getText().insertTextContent(frame_cursor, frame, False)
                frame.getText().setString("Private frame content")
                first.setModified(False)

                env = os.environ.copy()
                env["PYTHONPATH"] = str(ROOT / "src")
                env["YBERU_WRITER_PORT"] = str(port)
                env["YBERU_WRITER_ALLOWED_SAVE_ROOTS"] = str(root)
                async def scenario():
                    params = StdioServerParameters(command=str(PYTHON), args=["-m", "yberu_writer_mcp.server"],
                                                   cwd=str(ROOT), env=env)
                    async with stdio_client(params) as (read_stream, write_stream):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            async def preview(change_type, arguments):
                                result = await call_tool(session, "writer_preview_change", {
                                    "change_type": change_type, **arguments})
                                self.assertTrue(result["preview_only"])
                                return result
                            tools = await session.list_tools()
                            names = {tool.name for tool in tools.tools}
                            self.assertEqual(len(names), 14)
                            self.assertIn("writer_read_document", names)
                            self.assertIn("writer_replace_range", names)
                            self.assertIn("writer_read_text_range", names)
                            self.assertIn("writer_replace_table_cell", names)
                            self.assertIn("writer_preview_change", names)
                            self.assertIn("writer_undo_last_change", names)

                            # A and J: real MCP handshake, tool discovery, and read call.
                            state = await call_tool(session, "writer_get_state")
                            self.assertTrue(state["connected"])
                            self.assertTrue(state["capabilities"]["mcp_subscriptions"])
                            self.assertTrue(state["libreoffice_version"].startswith("24.2"), state)
                            docs = await call_tool(session, "writer_list_documents")
                            self.assertGreaterEqual(len(docs), 2)
                            with_contents = []
                            for row in docs:
                                result = await call_tool(session, "writer_read_document", {"document_id": row["document_id"]})
                                with_contents.append((row, result["text"]))
                            first_info = next(row for row, text in with_contents if "First paragraph." in text)
                            second_info = next(row for row, text in with_contents if "Independent document." in text)
                            self.assertNotEqual(first_info["document_id"], second_info["document_id"])
                            self.assertEqual(first_info["title"], "Yberu Integration First")
                            self.assertFalse(first_info["read_only"])
                            self.assertGreaterEqual(first_info["revision"], 1)
                            read = await call_tool(session, "writer_read_document", {"document_id": first_info["document_id"]})
                            self.assertIn("First paragraph.", read["text"])
                            self.assertEqual(read["text"], first.getText().getString())
                            self.assertTrue(first_info["event_uri"].startswith("yberu-writer://documents/"))

                            # 9th tool: save an existing modified test document under its allowlisted root.
                            second.setModified(True)
                            save_preview = await preview("save_document", {
                                "document_id": second_info["document_id"],
                                "expected_revision": second_info["revision"]})
                            self.assertTrue(save_preview["modified"])
                            saved = await call_tool(session, "writer_save_document", {
                                "document_id": second_info["document_id"],
                                "expected_revision": second_info["revision"],
                                "expected_path": save_preview["path"],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": save_preview["confirmation_token"]})
                            self.assertTrue(saved["saved"])
                            self.assertTrue(save_path.is_file())

                            # D: select through Writer's view controller, then query through MCP.
                            selected = first.getText().createTextCursor()
                            selected.gotoStart(False)
                            selected.goRight(5, True)
                            first.getCurrentController().select(selected)
                            selection = await call_tool(session, "writer_read_selection", {"document_id": first_info["document_id"]})
                            self.assertEqual(selection["kind"], "text")
                            self.assertEqual(selection["text"], "First")

                            # E: exact anchored paragraph replacement; result is verified in live Writer.
                            structure = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            object_kinds = {item["kind"] for item in structure["objects"]}
                            self.assertIn("bookmark", object_kinds)
                            self.assertIn("header", object_kinds)
                            self.assertIn("frame", object_kinds)
                            self.assertTrue(any(item["text"] == "Private test header" for item in structure["objects"]))
                            self.assertTrue(any(item["text"] == "Private frame content" for item in structure["objects"]))
                            paragraph = next((p for p in structure["paragraphs"] if p["text"] == "First paragraph."), None)
                            self.assertIsNotNone(paragraph, structure)
                            insert_args = {"document_id": first_info["document_id"], "anchor": paragraph["anchor"],
                                           "expected_revision": structure["revision"], "text": " MCP", "position": "end"}
                            unconfirmed = await session.call_tool("writer_insert_text", {
                                **insert_args, "operation_id": str(uuid.uuid4()), "confirmation_token": ""})
                            self.assertTrue(unconfirmed.is_error)
                            self.assertIn("CONFIRMATION_REQUIRED", unconfirmed.content[0].text)
                            self.assertIn("First paragraph.", first.getText().getString())
                            insert_preview = await preview("insert_text", insert_args)
                            self.assertIn("First paragraph.", first.getText().getString())
                            async with listen(session, resource_subscriptions=[first_info["event_uri"]]) as events:
                                insert_operation_id = str(uuid.uuid4())
                                inserted = await call_tool(session, "writer_insert_text", {
                                    **insert_args, "operation_id": insert_operation_id,
                                    "confirmation_token": insert_preview["confirmation_token"]})
                                replay = await call_tool(session, "writer_insert_text", {
                                    **insert_args, "operation_id": insert_operation_id,
                                    "confirmation_token": insert_preview["confirmation_token"]})
                                self.assertEqual(replay, inserted)
                                event = await asyncio.wait_for(anext(events), timeout=5)
                                self.assertIsInstance(event, ResourceUpdated)
                                self.assertEqual(event.uri, first_info["event_uri"])
                                event_resource = await session.read_resource(first_info["event_uri"])
                                event_snapshot = json.loads(event_resource.contents[0].text)
                                self.assertEqual(event_snapshot["document_id"], first_info["document_id"])
                                self.assertIn("modified", event_snapshot["event_kinds"])
                            self.assertTrue(inserted["confirmed"])
                            structure2 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            paragraph2 = next(p for p in structure2["paragraphs"] if p["text"] == "First paragraph. MCP")
                            text_range = await call_tool(session, "writer_read_text_range", {
                                "document_id": first_info["document_id"], "start_anchor": paragraph2["anchor"],
                                "start_offset": 0, "end_anchor": paragraph2["anchor"], "end_offset": 5})
                            self.assertEqual(text_range["text"], "First")
                            range_args = {"document_id": first_info["document_id"], "anchor": text_range["range_anchor"],
                                          "expected_revision": text_range["revision"], "expected_text": "First",
                                          "new_content": "Opening"}
                            range_preview = await preview("replace_range", range_args)
                            range_replaced = await call_tool(session, "writer_replace_range", {
                                **range_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": range_preview["confirmation_token"]})
                            self.assertTrue(range_replaced["confirmed"])
                            self.assertIn("Opening paragraph. MCP", first.getText().getString())
                            structure3 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            paragraph3 = next(p for p in structure3["paragraphs"] if p["text"] == "Opening paragraph. MCP")
                            replace_args = {"document_id": first_info["document_id"], "anchor": paragraph3["anchor"],
                                            "expected_revision": structure3["revision"], "expected_text": paragraph3["text"],
                                            "new_content": "Replaced safely."}
                            replace_preview = await preview("replace_range", replace_args)
                            replaced = await call_tool(session, "writer_replace_range", {
                                **replace_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": replace_preview["confirmation_token"]})
                            self.assertTrue(replaced["confirmed"])
                            self.assertIn("Replaced safely.", first.getText().getString())
                            self.assertIn("Second paragraph.", first.getText().getString())

                            # G/K: formatting and preservation of table/cell data and unrelated paragraph.
                            structure4 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            self.assertEqual(len(structure4["tables"]), 1)
                            table_anchor = structure4["tables"][0]["table_anchor"]
                            table_read = await call_tool(session, "writer_read_table", {
                                "document_id": first_info["document_id"], "table_anchor": table_anchor,
                                "expected_revision": structure4["revision"]})
                            self.assertEqual(table_read["rows"][0][1]["text"], "keep-value")
                            cell_args = {"document_id": first_info["document_id"], "table_anchor": table_anchor,
                                         "cell_name": "B1", "expected_revision": structure4["revision"],
                                         "expected_text": "keep-value", "new_content": "updated-value"}
                            cell_preview = await preview("replace_table_cell", cell_args)
                            cell_replaced = await call_tool(session, "writer_replace_table_cell", {
                                **cell_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": cell_preview["confirmation_token"]})
                            self.assertTrue(cell_replaced["confirmed"])
                            undo_preview = await preview("undo_last_change", {
                                "document_id": first_info["document_id"],
                                "expected_revision": cell_replaced["revision"]})
                            undone = await call_tool(session, "writer_undo_last_change", {
                                "document_id": first_info["document_id"],
                                "expected_revision": cell_replaced["revision"],
                                "target_operation_id": undo_preview["target_operation_id"],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": undo_preview["confirmation_token"]})
                            self.assertTrue(undone["confirmed"])
                            self.assertEqual(table.getCellByName("B1").getString(), "keep-value")
                            structure4b = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            table_anchor = structure4b["tables"][0]["table_anchor"]
                            cell_args = {"document_id": first_info["document_id"], "table_anchor": table_anchor,
                                         "cell_name": "B1", "expected_revision": structure4b["revision"],
                                         "expected_text": "keep-value", "new_content": "updated-value"}
                            cell_preview = await preview("replace_table_cell", cell_args)
                            cell_replaced = await call_tool(session, "writer_replace_table_cell", {
                                **cell_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": cell_preview["confirmation_token"]})
                            self.assertTrue(cell_replaced["confirmed"])
                            structure5 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            second_para = next(p for p in structure5["paragraphs"] if p["text"] == "Second paragraph.")
                            style_args = {"document_id": first_info["document_id"], "anchor": second_para["anchor"],
                                          "expected_revision": structure5["revision"], "expected_text": second_para["text"],
                                          "style_name": "Heading 1"}
                            style_preview = await preview("apply_style", style_args)
                            styled = await call_tool(session, "writer_apply_style", {
                                **style_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": style_preview["confirmation_token"]})
                            self.assertTrue(styled["confirmed"])
                            self.assertEqual(first.getTextTables().getCount(), 1)
                            self.assertEqual(table.getCellByName("A1").getString(), "keep-cell")
                            self.assertEqual(table.getCellByName("B1").getString(), "updated-value")
                            self.assertIn("Replaced safely.", first.getText().getString())
                            self.assertIn("Second paragraph.", first.getText().getString())

                            # F: a GUI-side edit after a read invalidates the expected revision.
                            stale_structure = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            stale_para = stale_structure["paragraphs"][0]
                            stale_args = {"document_id": first_info["document_id"], "anchor": stale_para["anchor"],
                                          "expected_revision": stale_structure["revision"],
                                          "text": "should not apply", "position": "end"}
                            stale_preview = await preview("insert_text", stale_args)
                            first.getText().setString(first.getText().getString() + " external change")
                            stale = await session.call_tool("writer_insert_text", {
                                **stale_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": stale_preview["confirmation_token"]})
                            self.assertTrue(stale.is_error)
                            self.assertIn("CONFLICT", stale.content[0].text)

                            # H: send actual keyboard input to the Writer window owned by this test.
                            before = (await call_tool(session, "writer_get_state"))["events"]["sequence"]
                            first.getCurrentController().getViewCursor().gotoEnd(False)
                            first.getCurrentController().getFrame().getContainerWindow().toFront()
                            window_id = own_writer_window(pid)
                            subprocess.run(["wmctrl", "-ia", window_id], check=True)
                            time.sleep(0.3)
                            type_ascii("eventprobe")
                            deadline = time.monotonic() + 4
                            after = before
                            while time.monotonic() < deadline and after <= before:
                                await asyncio.sleep(0.1)
                                after = (await call_tool(session, "writer_get_state"))["events"]["sequence"]
                            self.assertGreater(after, before)
                            self.assertIn("eventprobe", first.getText().getString())

                            # I: restart Writer while the same MCP process/session remains connected.
                            old_id = first_info["document_id"]
                            stop_owned_writer(owned_pid[0])
                            owned_pid[0] = None
                            owned_pid[0] = await asyncio.to_thread(launch_writer, port, profile)
                            deadline = time.monotonic() + 12
                            reconnected = None
                            while time.monotonic() < deadline:
                                reconnected = await call_tool(session, "writer_get_state")
                                if reconnected["connected"] and reconnected["session_generation"] >= 2:
                                    break
                                await asyncio.sleep(0.2)
                            self.assertIsNotNone(reconnected)
                            self.assertTrue(reconnected["connected"])
                            self.assertGreaterEqual(reconnected["session_generation"], 2)
                            stale = await session.call_tool("writer_read_document", {"document_id": old_id})
                            self.assertTrue(stale.is_error)
                            self.assertIn("DOCUMENT_NOT_FOUND", stale.content[0].text)
                            # J: this same real stdio session already performed confirmed read/write calls.
            finally:
                for doc in documents:
                    try:
                        doc.setModified(False)
                        doc.close(True)
                    except Exception:
                        pass
                stop_owned_writer(owned_pid[0])


if __name__ == "__main__":
    unittest.main()
