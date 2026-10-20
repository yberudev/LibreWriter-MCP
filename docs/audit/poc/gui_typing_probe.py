"""Safely test live keyboard input and UNO events in an isolated Writer window."""

import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uno
import unohelper
from Xlib import X, XK
from Xlib.display import Display
from Xlib.ext import xtest
from com.sun.star.util import XModifyListener


PORT = "20268"
PROFILE = tempfile.TemporaryDirectory(prefix="yberu-gui-typing-")
ENV = os.environ.copy()
ENV.pop("SAL_USE_VCLPLUGIN", None)
PROCESS = subprocess.Popen(
    [
        "/usr/bin/soffice",
        "--nodefault",
        "--norestore",
        "--nofirststartwizard",
        f"--accept=socket,host=127.0.0.1,port={PORT};urp;StarOffice.ServiceManager",
        f"-env:UserInstallation={Path(PROFILE.name).as_uri()}",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    env=ENV,
    start_new_session=True,
)


class ModifyProbe(unohelper.Base, XModifyListener):
    def __init__(self):
        self.count = 0
        self.threads = set()

    def modified(self, event):
        self.count += 1
        self.threads.add(threading.current_thread().name)

    def disposing(self, event):
        pass


def connect():
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            context = resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext"
            )
            return context
        except Exception:
            if PROCESS.poll() is not None:
                raise RuntimeError(f"soffice exited with {PROCESS.returncode}")
            time.sleep(0.2)
    raise TimeoutError("UNO listener did not become ready")


def own_writer_window():
    result = subprocess.run(
        ["wmctrl", "-lp"], check=True, capture_output=True, text=True
    )
    candidates = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5 or "LibreOffice Writer" not in fields[4]:
            continue
        try:
            window_pid = int(fields[2])
            if os.getpgid(window_pid) == PROCESS.pid:
                candidates.append(fields[0])
        except (ProcessLookupError, PermissionError, ValueError):
            continue
    if not candidates:
        raise RuntimeError("Could not identify the isolated Writer window by process group")
    return candidates[-1]


def type_ascii(text):
    display = Display()
    for character in text:
        keycode = display.keysym_to_keycode(XK.string_to_keysym(character))
        if not keycode:
            raise ValueError(f"No X11 keycode for {character!r}")
        xtest.fake_input(display, X.KeyPress, detail=keycode)
        xtest.fake_input(display, X.KeyRelease, detail=keycode)
    display.sync()
    display.close()


def main():
    desktop = None
    doc = None
    listener = ModifyProbe()
    watchdog = threading.Timer(
        35, lambda: os.killpg(PROCESS.pid, signal.SIGTERM)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        context = connect()
        desktop = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context
        )
        doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
        controller = doc.getCurrentController()
        window = controller.getFrame().getContainerWindow()
        window.toFront()
        window.setFocus()
        controller.getViewCursor().gotoStart(False)
        doc.addModifyListener(listener)

        window_id = own_writer_window()
        subprocess.run(["wmctrl", "-ia", window_id], check=True)
        time.sleep(0.3)
        type_ascii("livetestaudit")
        time.sleep(0.5)

        visible = bool(window.isVisible())
        content = doc.getText().getString()
        print(f"writer_window_visible={visible}")
        print(f"keyboard_input_content={content!r}")
        print(f"modify_callbacks={listener.count}")
        print(f"callback_threads={sorted(listener.threads)}")
        if content != "livetestaudit" or listener.count < 1:
            raise AssertionError("Visible keyboard input was not observed through UNO")
    finally:
        watchdog.cancel()
        if doc is not None:
            try:
                doc.removeModifyListener(listener)
            except Exception:
                pass
            try:
                doc.setModified(False)
            except Exception:
                pass
            try:
                doc.close(True)
            except Exception:
                pass
        if desktop is not None:
            try:
                desktop.terminate()
            except Exception:
                pass
        try:
            PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(PROCESS.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                PROCESS.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(PROCESS.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                PROCESS.wait(timeout=5)
        PROFILE.cleanup()


if __name__ == "__main__":
    main()
