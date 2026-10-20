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
                            tools = await session.list_tools()
                            names = {tool.name for tool in tools.tools}
                            self.assertEqual(len(names), 9)
                            self.assertIn("writer_read_document", names)
                            self.assertIn("writer_replace_range", names)

                            # A and J: real MCP handshake, tool discovery, and read call.
                            state = await call_tool(session, "writer_get_state")
                            self.assertTrue(state["connected"])
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

                            # 9th tool: save an existing modified test document under its allowlisted root.
                            second.setModified(True)
                            saved = await call_tool(session, "writer_save_document", {
                                "document_id": second_info["document_id"], "operation_id": str(uuid.uuid4())})
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
                            paragraph = next((p for p in structure["paragraphs"] if p["text"] == "First paragraph."), None)
                            self.assertIsNotNone(paragraph, structure)
                            inserted = await call_tool(session, "writer_insert_text", {
                                "document_id": first_info["document_id"], "anchor": paragraph["anchor"],
                                "expected_revision": structure["revision"], "text": " MCP", "position": "end",
                                "operation_id": str(uuid.uuid4())})
                            self.assertTrue(inserted["confirmed"])
                            structure2 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            paragraph2 = next(p for p in structure2["paragraphs"] if p["text"] == "First paragraph. MCP")
                            replaced = await call_tool(session, "writer_replace_range", {
                                "document_id": first_info["document_id"], "anchor": paragraph2["anchor"],
                                "expected_revision": structure2["revision"], "expected_text": paragraph2["text"],
                                "new_content": "Replaced safely.", "operation_id": str(uuid.uuid4())})
                            self.assertTrue(replaced["confirmed"])
                            self.assertIn("Replaced safely.", first.getText().getString())
                            self.assertIn("Second paragraph.", first.getText().getString())

                            # G/K: formatting and preservation of table/cell data and unrelated paragraph.
                            structure3 = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            second_para = next(p for p in structure3["paragraphs"] if p["text"] == "Second paragraph.")
                            styled = await call_tool(session, "writer_apply_style", {
                                "document_id": first_info["document_id"], "anchor": second_para["anchor"],
                                "expected_revision": structure3["revision"], "expected_text": second_para["text"],
                                "style_name": "Heading 1", "operation_id": str(uuid.uuid4())})
                            self.assertTrue(styled["confirmed"])
                            self.assertEqual(first.getTextTables().getCount(), 1)
                            self.assertEqual(table.getCellByName("A1").getString(), "keep-cell")
                            self.assertIn("Replaced safely.", first.getText().getString())
                            self.assertIn("Second paragraph.", first.getText().getString())

                            # F: a GUI-side edit after a read invalidates the expected revision.
                            stale_structure = await call_tool(session, "writer_read_structure", {"document_id": first_info["document_id"]})
                            stale_para = stale_structure["paragraphs"][0]
                            first.getText().setString(first.getText().getString() + " external change")
                            stale = await session.call_tool("writer_insert_text", {
                                "document_id": first_info["document_id"], "anchor": stale_para["anchor"],
                                "expected_revision": stale_structure["revision"], "text": "should not apply",
                                "position": "end", "operation_id": str(uuid.uuid4())})
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
