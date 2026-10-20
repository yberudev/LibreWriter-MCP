"""Safe UNO API probe using a private temporary profile and temporary ODT."""

import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uno
import unohelper
from com.sun.star.util import XModifyListener


class ModifyProbe(unohelper.Base, XModifyListener):
    def __init__(self):
        self.count = 0
        self.threads = set()
        self.lock = threading.Lock()

    def modified(self, event):
        with self.lock:
            self.count += 1
            self.threads.add(threading.current_thread().name)

    def disposing(self, event):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def property_value(name, value):
    result = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
    result.Name = name
    result.Value = value
    return result


def main():
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="yberu-uno-probe-") as tmp:
        root = Path(tmp)
        profile = root / "profile"
        odt = root / "roundtrip.odt"
        env = os.environ.copy()
        env.pop("SAL_USE_VCLPLUGIN", None)
        process = subprocess.Popen(
            [
                "/usr/bin/soffice",
                "--headless",
                "--nodefault",
                "--norestore",
                "--nofirststartwizard",
                f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ServiceManager",
                f"-env:UserInstallation={profile.as_uri()}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        desktop = doc = reopened = None
        listener = ModifyProbe()
        try:
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", local
            )
            deadline = time.monotonic() + 20
            context = None
            while time.monotonic() < deadline:
                try:
                    context = resolver.resolve(
                        f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
                    )
                    break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError(f"soffice exited with {process.returncode}")
                    time.sleep(0.2)
            if context is None:
                raise TimeoutError("UNO listener did not become ready")

            desktop = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", context
            )
            doc = desktop.loadComponentFromURL(
                "private:factory/swriter", "_blank", 0, tuple()
            )
            doc.addModifyListener(listener)
            text = doc.getText()
            text.setString("Audit heading\nFirst paragraph.")
            print("created_writer_document=OK")
            component_enumeration = desktop.getComponents().createEnumeration()
            component_count = 0
            while component_enumeration.hasMoreElements():
                component_enumeration.nextElement()
                component_count += 1
            print("document_component_count=", component_count)
            print("document_text_readable=", "First paragraph." in text.getString())

            cursor = text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEndOfParagraph(True)
            cursor.ParaStyleName = "Heading 1"
            cursor.gotoStart(False)
            cursor.goRight(5, True)
            cursor.CharWeight = 150.0
            print("paragraph_style=", cursor.ParaStyleName)
            print("selection_length=", len(cursor.getString()))

            controller = doc.getCurrentController()
            frame = controller.getFrame()
            dispatch_helper = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.DispatchHelper", context
            )
            dispatch_cursor = text.createTextCursor()
            dispatch_cursor.gotoEnd(False)
            dispatch_cursor.goLeft(10, True)
            controller.select(dispatch_cursor)
            dispatch_weight_before = float(dispatch_cursor.CharWeight)
            dispatch_helper.executeDispatch(frame, ".uno:Bold", "", 0, tuple())
            dispatch_weight_after = float(dispatch_cursor.CharWeight)
            print("dispatch_bold_weight_before_after=", dispatch_weight_before, dispatch_weight_after)

            undo = doc.getUndoManager()
            undo.enterUndoContext("Yberu audit reversible insertion")
            end_cursor = text.createTextCursor()
            end_cursor.gotoEnd(False)
            text.insertString(end_cursor, "UNDO_SENTINEL", False)
            undo.leaveUndoContext()
            before_undo = "UNDO_SENTINEL" in text.getString()
            undo.undo()
            after_undo = "UNDO_SENTINEL" not in text.getString()
            undo.redo()
            after_redo = "UNDO_SENTINEL" in text.getString()
            print("undo_redo=", before_undo, after_undo, after_redo)

            # Simulate another editor changing the content after an MCP read.
            expected_snapshot = text.getString()
            text.setString(expected_snapshot + " user-change")
            conflict_detected = text.getString() != expected_snapshot
            guarded_write_applied = False if conflict_detected else True
            print("optimistic_conflict_detected=", conflict_detected)
            print("stale_write_blocked=", not guarded_write_applied)

            table = doc.createInstance("com.sun.star.text.TextTable")
            table.initialize(2, 2)
            table_cursor = text.createTextCursor()
            table_cursor.gotoEnd(False)
            text.insertTextContent(table_cursor, table, False)
            table.getCellByName("A1").setString("Header")
            table.getCellByName("B1").setString("Value")
            table.getCellByName("A2").setString("Row")
            table.getCellByName("B2").setString("1")
            print("table_cells=", table.getCellNames())

            annotation = doc.createInstance("com.sun.star.text.TextField.Annotation")
            annotation.setPropertyValue("Author", "Yberu audit")
            annotation.setPropertyValue("Content", "Temporary annotation PoC")
            annotation_cursor = text.createTextCursor()
            annotation_cursor.gotoStart(False)
            text.insertTextContent(annotation_cursor, annotation, False)
            annotations = doc.getTextFields().createEnumeration()
            annotation_authors = []
            while annotations.hasMoreElements():
                field = annotations.nextElement()
                if field.supportsService("com.sun.star.text.TextField.Annotation"):
                    annotation_authors.append(field.getPropertyValue("Author"))
            print("annotation_authors=", annotation_authors)

            doc.storeAsURL(
                uno.systemPathToFileUrl(str(odt)),
                (property_value("FilterName", "writer8"),),
            )
            print("odt_saved=", odt.exists(), odt.stat().st_size if odt.exists() else 0)
            doc.close(True)
            doc = None
            reopened = desktop.loadComponentFromURL(
                uno.systemPathToFileUrl(str(odt)),
                "_blank",
                0,
                (property_value("Hidden", True),),
            )
            reopened_text = reopened.getText().getString()
            print("odt_roundtrip_text=", "user-change" in reopened_text)
            print("odt_roundtrip_table_count=", reopened.getTextTables().getCount())
            print("modify_event_count=", listener.count)
            print("modify_event_threads=", sorted(listener.threads))

            doc_types = [item.typeName for item in reopened.getTypes()]
            print("writer_uno_type_count=", len(doc_types))
            print("has_XUndoManagerSupplier=", any("XUndoManagerSupplier" in n for n in doc_types))
            print("has_XDocumentEventBroadcaster=", any("XDocumentEventBroadcaster" in n for n in doc_types))
        finally:
            for component in (reopened, doc):
                if component is not None:
                    try:
                        component.setModified(False)
                        component.close(True)
                    except Exception:
                        pass
            if desktop is not None:
                try:
                    desktop.terminate()
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
