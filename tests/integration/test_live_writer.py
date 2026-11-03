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


def find_document_by_title(desktop, title):
    components = desktop.getComponents().createEnumeration()
    while components.hasMoreElements():
        component = components.nextElement()
        try:
            if component.getDocumentProperties().Title == title:
                return component
        except Exception:
            continue
    return None


def property_value(name, value):
    item = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    item.Name, item.Value = name, value
    return item


def write_test_png(path):
    import struct
    import zlib

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    width, height = 2, 2
    rows = (b"\x00\xff\x20\x20\xff\x20\xc0\x20\xff"
            b"\x00\x20\x60\xff\xff\x20\x20\xff\xff")
    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))
    path.write_bytes(data)


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
            test_image = root / "writer-mcp-test.png"
            write_test_png(test_image)
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
                template_path = root / "approved-template.odt"
                template_document = create_writer_doc(
                    desktop, "Template Source", "Template heading\nTemplate body paragraph.")
                template_document.storeAsURL(uno.systemPathToFileUrl(str(template_path)),
                                             (property_value("FilterName", "writer8"),))
                template_document.dispose()
                template_bytes_before = template_path.read_bytes()
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
                env["YBERU_WRITER_ALLOWED_IMAGE_ROOTS"] = str(root)
                env["YBERU_WRITER_ALLOWED_TEMPLATE_ROOTS"] = str(root)
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
                            self.assertEqual(len(names), 27)
                            self.assertIn("writer_read_document", names)
                            self.assertIn("writer_replace_range", names)
                            self.assertIn("writer_read_text_range", names)
                            self.assertIn("writer_replace_table_cell", names)
                            self.assertIn("writer_preview_change", names)
                            self.assertIn("writer_preview_create_document", names)
                            self.assertIn("writer_create_document", names)
                            self.assertIn("writer_insert_table", names)
                            self.assertIn("writer_insert_image", names)
                            self.assertIn("writer_save_as", names)
                            self.assertIn("writer_preview_compose_document", names)
                            self.assertIn("writer_compose_document", names)
                            self.assertIn("writer_apply_formatting", names)
                            self.assertIn("writer_edit_table_structure", names)
                            self.assertIn("writer_format_table_cell", names)
                            self.assertIn("writer_update_image", names)
                            self.assertIn("writer_preview_create_from_template", names)
                            self.assertIn("writer_create_from_template", names)
                            self.assertIn("writer_undo_last_change", names)

                            # A and J: real MCP handshake, tool discovery, and read call.
                            state = await call_tool(session, "writer_get_state")
                            self.assertTrue(state["connected"])
                            self.assertTrue(state["capabilities"]["mcp_subscriptions"])
                            self.assertTrue(state["capabilities"]["create_from_template"])
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

                            # Create a new editable document from an allowlisted ODT template.
                            template_preview = await call_tool(session, "writer_preview_create_from_template", {
                                "template_path": str(template_path), "title": "Created from approved template"})
                            self.assertTrue(template_preview["preview_only"])
                            self.assertEqual(template_preview["template_mime_type"],
                                             "application/vnd.oasis.opendocument.text")
                            from_template = await call_tool(session, "writer_create_from_template", {
                                "template_path": str(template_path),
                                "expected_template_hash": template_preview["template_hash"],
                                "title": "Created from approved template",
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": template_preview["confirmation_token"]})
                            self.assertTrue(from_template["confirmed"])
                            template_copy = await call_tool(session, "writer_read_document", {
                                "document_id": from_template["document_id"]})
                            self.assertIn("Template heading", template_copy["text"])
                            self.assertIn("Template body paragraph.", template_copy["text"])
                            self.assertEqual(from_template["title"], "Created from approved template")
                            template_copy_component = find_document_by_title(
                                desktop, "Created from approved template")
                            self.assertIsNotNone(template_copy_component)
                            self.assertNotEqual(template_copy_component.getURL(),
                                                uno.systemPathToFileUrl(str(template_path)))
                            self.assertEqual(template_path.read_bytes(), template_bytes_before)

                            # Verify each additional allowlisted export filter through the MCP tool.
                            for export_format in ("docx", "rtf", "txt"):
                                title = f"Yberu Export {export_format.upper()}"
                                create_preview = await call_tool(session, "writer_preview_create_document", {
                                    "title": title, "initial_paragraphs": ["Export filter check."]})
                                export_doc = await call_tool(session, "writer_create_document", {
                                    "title": title, "initial_paragraphs": ["Export filter check."],
                                    "operation_id": str(uuid.uuid4()),
                                    "confirmation_token": create_preview["confirmation_token"]})
                                export_path = root / f"export-check.{export_format}"
                                export_args = {"document_id": export_doc["document_id"],
                                               "expected_revision": export_doc["revision"],
                                               "target_path": str(export_path),
                                               "file_format": export_format, "overwrite": False}
                                export_preview = await preview("save_as", export_args)
                                export_result = await call_tool(session, "writer_save_as", {
                                    **export_args, "expected_target_hash": export_preview["target_hash"],
                                    "operation_id": str(uuid.uuid4()),
                                    "confirmation_token": export_preview["confirmation_token"]})
                                self.assertTrue(export_result["saved"])
                                self.assertTrue(export_path.is_file())
                                self.assertGreater(export_path.stat().st_size, 0)

                            # Create a new document and compose text, a table, and an embedded image through MCP.
                            create_preview = await call_tool(session, "writer_preview_create_document", {
                                "title": "Yberu MCP Created Document",
                                "initial_paragraphs": ["Project Summary", "Created through the MCP tools."]})
                            self.assertTrue(create_preview["preview_only"])
                            created = await call_tool(session, "writer_create_document", {
                                "title": "Yberu MCP Created Document",
                                "initial_paragraphs": ["Project Summary", "Created through the MCP tools."],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": create_preview["confirmation_token"]})
                            self.assertTrue(created["confirmed"])
                            new_document_id = created["document_id"]
                            created_structure = await call_tool(session, "writer_read_structure", {
                                "document_id": new_document_id})
                            self.assertEqual(created_structure["revision"], created["revision"])
                            body_anchor = next(p["anchor"] for p in created_structure["paragraphs"]
                                               if p["text"] == "Created through the MCP tools.")
                            table_args = {"document_id": new_document_id, "anchor": body_anchor,
                                          "expected_revision": created_structure["revision"],
                                          "rows": 2, "columns": 2,
                                          "cell_values": [["Item", "Value"], ["Images", "1"]]}
                            table_preview = await preview("insert_table", table_args)
                            table_created = await call_tool(session, "writer_insert_table", {
                                **table_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": table_preview["confirmation_token"]})
                            self.assertTrue(table_created["confirmed"])
                            after_table = await call_tool(session, "writer_read_structure", {
                                "document_id": new_document_id})
                            image_anchor = next(p["anchor"] for p in after_table["paragraphs"]
                                                if p["text"] == "Created through the MCP tools.")
                            image_args = {"document_id": new_document_id, "anchor": image_anchor,
                                          "expected_revision": after_table["revision"],
                                          "image_path": str(test_image),
                                          "alt_text": "Two color test image", "width_mm": 20, "height_mm": 20}
                            image_preview = await preview("insert_image", image_args)
                            self.assertEqual(image_preview["image_name"], test_image.name)
                            image_inserted = await call_tool(session, "writer_insert_image", {
                                **image_args, "expected_image_hash": image_preview["image_hash"],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": image_preview["confirmation_token"]})
                            self.assertTrue(image_inserted["confirmed"])
                            final_structure = await call_tool(session, "writer_read_structure", {
                                "document_id": new_document_id})
                            self.assertEqual(len(final_structure["tables"]), 1)
                            table_content = await call_tool(session, "writer_read_table", {
                                "document_id": new_document_id,
                                "table_anchor": final_structure["tables"][0]["table_anchor"],
                                "expected_revision": final_structure["revision"]})
                            self.assertEqual(table_content["rows"][1][0]["text"], "Images")
                            self.assertEqual(table_content["rows"][1][1]["text"], "1")
                            images = [item for item in final_structure["objects"] if item["kind"] == "image"]
                            self.assertEqual(len(images), 1)
                            self.assertEqual(images[0]["alt_text"], "Two color test image")
                            created_component = find_document_by_title(desktop, "Yberu MCP Created Document")
                            self.assertIsNotNone(created_component)
                            self.assertEqual(created_component.getTextTables().getCount(), 1)
                            self.assertEqual(created_component.getGraphicObjects().getCount(), 1)
                            self.assertIn("Created through the MCP tools.", created_component.getText().getString())

                            # Save As requires an allowlisted destination and explicit overwrite confirmation.
                            save_as_target = root / "created-document.odt"
                            save_as_target.write_bytes(b"existing destination must survive a default preview")
                            save_as_args = {"document_id": new_document_id,
                                            "expected_revision": final_structure["revision"],
                                            "target_path": str(save_as_target), "file_format": "odt",
                                            "overwrite": False}
                            denied_overwrite = await session.call_tool("writer_preview_change", {
                                "change_type": "save_as", **save_as_args})
                            self.assertTrue(denied_overwrite.is_error)
                            self.assertIn("CONFLICT", denied_overwrite.content[0].text)
                            self.assertEqual(save_as_target.read_bytes(), b"existing destination must survive a default preview")
                            save_as_args["overwrite"] = True
                            save_as_preview = await preview("save_as", save_as_args)
                            self.assertTrue(save_as_preview["target_exists"])
                            saved_as = await call_tool(session, "writer_save_as", {
                                **save_as_args,
                                "expected_target_hash": save_as_preview["target_hash"],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": save_as_preview["confirmation_token"]})
                            self.assertTrue(saved_as["saved"])
                            self.assertTrue(save_as_target.is_file())
                            self.assertGreater(save_as_target.stat().st_size, 100)
                            self.assertEqual(uno.fileUrlToSystemPath(created_component.getURL()), str(save_as_target))
                            self.assertFalse(created_component.isModified())

                            # Compose a second new document from typed blocks in one confirmed operation.
                            composition_blocks = [
                                {"type": "heading", "level": 1, "text": "Quarterly Brief"},
                                {"type": "paragraph", "text": "Created with structured composition."},
                                {"type": "table", "rows": [["Key", "Value"], ["Status", "Ready"]]},
                                {"type": "image", "image_path": str(test_image),
                                 "alt_text": "Composition preview image", "width_mm": 20, "height_mm": 20},
                            ]
                            compose_preview = await call_tool(session, "writer_preview_compose_document", {
                                "title": "Yberu Composed Blueprint", "blocks": composition_blocks})
                            self.assertTrue(compose_preview["preview_only"])
                            self.assertEqual(compose_preview["block_count"], 4)
                            composed = await call_tool(session, "writer_compose_document", {
                                "title": "Yberu Composed Blueprint", "blocks": composition_blocks,
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": compose_preview["confirmation_token"]})
                            self.assertTrue(composed["confirmed"])
                            self.assertEqual(composed["table_count"], 1)
                            self.assertEqual(composed["image_count"], 1)
                            composed_structure = await call_tool(session, "writer_read_structure", {
                                "document_id": composed["document_id"]})
                            self.assertTrue(any(p["text"] == "Quarterly Brief"
                                                and p["heading_level"] == 1
                                                for p in composed_structure["paragraphs"]))
                            self.assertTrue(any(p["text"] == "Created with structured composition."
                                                for p in composed_structure["paragraphs"]))
                            self.assertEqual(len(composed_structure["tables"]), 1)
                            self.assertTrue(any(item.get("kind") == "image"
                                                and item["alt_text"] == "Composition preview image"
                                                for item in composed_structure["objects"]))
                            body_paragraph = next(p for p in composed_structure["paragraphs"]
                                                  if p["text"] == "Created with structured composition.")
                            format_args = {"document_id": composed["document_id"],
                                           "anchor": body_paragraph["anchor"],
                                           "expected_revision": composed_structure["revision"],
                                           "expected_text": body_paragraph["text"],
                                           "bold": True, "italic": True, "font_size_pt": 14,
                                           "font_color": "#1F4E79", "alignment": "center"}
                            format_preview = await preview("apply_formatting", format_args)
                            self.assertEqual(format_preview["proposed_formatting"], {
                                "bold": True, "italic": True, "font_size_pt": 14.0,
                                "font_color": "#1F4E79", "alignment": "center"})
                            formatted = await call_tool(session, "writer_apply_formatting", {
                                **format_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": format_preview["confirmation_token"]})
                            self.assertTrue(formatted["confirmed"])
                            formatted_structure = await call_tool(session, "writer_read_structure", {
                                "document_id": composed["document_id"]})
                            formatted_paragraph = next(p for p in formatted_structure["paragraphs"]
                                                       if p["text"] == "Created with structured composition.")
                            self.assertGreaterEqual(float(formatted_paragraph["formatting"]["CharWeight"]), 150)
                            self.assertIn("ITALIC", formatted_paragraph["formatting"]["CharPosture"])
                            self.assertEqual(float(formatted_paragraph["formatting"]["CharHeight"]), 14.0)
                            self.assertEqual(int(formatted_paragraph["formatting"]["CharColor"]), 0x1F4E79)
                            self.assertIn("CENTER", formatted_paragraph["formatting"]["ParaAdjust"])

                            async def edit_table(operation, index):
                                current_structure = await call_tool(session, "writer_read_structure", {
                                    "document_id": composed["document_id"]})
                                args = {"document_id": composed["document_id"],
                                        "table_anchor": current_structure["tables"][0]["table_anchor"],
                                        "expected_revision": current_structure["revision"],
                                        "table_operation": operation, "index": index, "count": 1}
                                table_preview = await preview("edit_table_structure", args)
                                result = await call_tool(session, "writer_edit_table_structure", {
                                    **args, "expected_table_hash": table_preview["table_hash"],
                                    "operation_id": str(uuid.uuid4()),
                                    "confirmation_token": table_preview["confirmation_token"]})
                                return result, await call_tool(session, "writer_read_structure", {
                                    "document_id": composed["document_id"]})

                            inserted_row, structure_after_row = await edit_table("insert_rows", 2)
                            self.assertEqual(inserted_row["rows"], 3)
                            inserted_column, structure_after_column = await edit_table("insert_columns", 2)
                            self.assertEqual(inserted_column["columns"], 3)
                            deleted_row, structure_after_delete_row = await edit_table("delete_rows", 2)
                            self.assertEqual(deleted_row["rows"], 2)
                            deleted_column, structure_after_delete_column = await edit_table("delete_columns", 2)
                            self.assertEqual(deleted_column["columns"], 2)
                            table_read_after_shape = await call_tool(session, "writer_read_table", {
                                "document_id": composed["document_id"],
                                "table_anchor": structure_after_delete_column["tables"][0]["table_anchor"],
                                "expected_revision": structure_after_delete_column["revision"]})
                            self.assertEqual(table_read_after_shape["rows"][1][1]["text"], "Ready")
                            self.assertEqual((table_read_after_shape["row_count"],
                                              table_read_after_shape["column_count"]), (2, 2))

                            cell_args = {"document_id": composed["document_id"],
                                         "table_anchor": structure_after_delete_column["tables"][0]["table_anchor"],
                                         "cell_name": "B2",
                                         "expected_revision": structure_after_delete_column["revision"],
                                         "expected_text": "Ready", "bold": True,
                                         "font_color": "#008000", "alignment": "center"}
                            cell_preview = await preview("format_table_cell", cell_args)
                            formatted_cell = await call_tool(session, "writer_format_table_cell", {
                                **cell_args, "operation_id": str(uuid.uuid4()),
                                "confirmation_token": cell_preview["confirmation_token"]})
                            self.assertTrue(formatted_cell["confirmed"])
                            structure_after_cell = await call_tool(session, "writer_read_structure", {
                                "document_id": composed["document_id"]})
                            formatted_table = await call_tool(session, "writer_read_table", {
                                "document_id": composed["document_id"],
                                "table_anchor": structure_after_cell["tables"][0]["table_anchor"],
                                "expected_revision": structure_after_cell["revision"]})
                            self.assertEqual(formatted_table["rows"][1][1]["text"], "Ready")
                            self.assertGreaterEqual(float(formatted_table["rows"][1][1]["formatting"]["CharWeight"]), 150)
                            self.assertEqual(int(formatted_table["rows"][1][1]["formatting"]["CharColor"]), 0x008000)

                            image_record = next(item for item in structure_after_cell["objects"]
                                                if item.get("kind") == "image")
                            image_args = {"document_id": composed["document_id"],
                                          "image_ref": image_record["object_ref"],
                                          "expected_revision": structure_after_cell["revision"],
                                          "image_alt_text": "Updated composition image",
                                          "width_mm": 30, "height_mm": 30,
                                          "alignment": "center"}
                            image_preview = await preview("update_image", image_args)
                            updated_image = await call_tool(session, "writer_update_image", {
                                "document_id": composed["document_id"],
                                "image_ref": image_record["object_ref"],
                                "expected_revision": structure_after_cell["revision"],
                                "expected_image_hash": image_preview["image_record_hash"],
                                "new_alt_text": image_args["image_alt_text"],
                                "width_mm": image_args["width_mm"],
                                "height_mm": image_args["height_mm"],
                                "alignment": image_args["alignment"],
                                "operation_id": str(uuid.uuid4()),
                                "confirmation_token": image_preview["confirmation_token"]})
                            self.assertTrue(updated_image["confirmed"])
                            final_composed_structure = await call_tool(session, "writer_read_structure", {
                                "document_id": composed["document_id"]})
                            updated_image_record = next(item for item in final_composed_structure["objects"]
                                                        if item.get("kind") == "image")
                            self.assertEqual(updated_image_record["alt_text"], "Updated composition image")
                            self.assertEqual(updated_image_record["width_100mm"], 3000)
                            composed_component = find_document_by_title(desktop, "Yberu Composed Blueprint")
                            graphic = composed_component.getGraphicObjects().getByIndex(0)
                            image_anchor = graphic.getAnchor()
                            image_cursor = image_anchor.getText().createTextCursorByRange(image_anchor)
                            image_cursor.gotoStartOfParagraph(False)
                            self.assertIn("CENTER", str(image_cursor.ParaAdjust))

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
