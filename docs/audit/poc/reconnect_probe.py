"""Test reconnecting to a Writer process restarted by this probe only."""

import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import uno


PORT = "20269"
PROFILE = tempfile.TemporaryDirectory(prefix="yberu-reconnect-")


def start_office():
    env = os.environ.copy()
    env.pop("SAL_USE_VCLPLUGIN", None)
    return subprocess.Popen(
        [
            "/usr/bin/soffice",
            "--headless",
            "--nodefault",
            "--norestore",
            "--nofirststartwizard",
            f"--accept=socket,host=127.0.0.1,port={PORT};urp;StarOffice.ServiceManager",
            f"-env:UserInstallation={Path(PROFILE.name).as_uri()}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


def connect(process, timeout=15):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext"
            )
        except Exception:
            if process.poll() is not None:
                raise RuntimeError(f"soffice exited with {process.returncode}")
            time.sleep(0.2)
    raise TimeoutError("UNO listener did not become ready")


def stop_group(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=7)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def main():
    first = None
    second = None
    watchdog = threading.Timer(50, lambda: first and stop_group(first))
    watchdog.daemon = True
    watchdog.start()
    try:
        first = start_office()
        context1 = connect(first)
        desktop1 = context1.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context1
        )
        doc1 = desktop1.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
        doc1.getText().setString("before restart")
        doc1.setModified(False)
        print("first_process_connection=OK")
        print("first_process_pid=", first.pid)
        stop_group(first)

        second = start_office()
        context2 = connect(second)
        desktop2 = context2.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", context2
        )
        doc2 = desktop2.loadComponentFromURL("private:factory/swriter", "_blank", 0, tuple())
        print("reconnected_after_restart=OK")
        print("second_process_pid=", second.pid)
        print("new_document_readable=", doc2.getText().getString() == "")
        doc2.setModified(False)
        doc2.close(True)
        desktop2.terminate()
    finally:
        watchdog.cancel()
        if first is not None:
            stop_group(first)
        if second is not None:
            stop_group(second)
        PROFILE.cleanup()


if __name__ == "__main__":
    main()
