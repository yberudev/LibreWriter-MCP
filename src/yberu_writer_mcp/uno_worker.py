"""Single-threaded gateway for every PyUNO operation."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any, Callable

from .config import Settings
from .errors import WriterError

log = logging.getLogger(__name__)


class UnoWorker:
    def __init__(self, settings: Settings, on_disconnect: Callable[[], None] | None = None):
        self.settings = settings
        self._on_disconnect = on_disconnect
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="yberu-uno")
        self._lock = threading.Lock()
        self._local = None
        self._context = None
        self._desktop = None
        self._generation = 0
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    def call(self, operation: Callable[[Any, Any], Any], *, mutating: bool = False, timeout: float | None = None) -> Any:
        if self._closed:
            raise WriterError("NOT_CONNECTED", "The UNO worker is closed.")
        future = self._executor.submit(self._execute, operation)
        try:
            return future.result(timeout=timeout or self.settings.operation_timeout)
        except concurrent.futures.TimeoutError as exc:
            if mutating:
                raise WriterError("UNKNOWN_OUTCOME", "The write exceeded its deadline; inspect the document before retrying.", retryable=False) from exc
            raise WriterError("OPERATION_TIMEOUT", "The Writer operation exceeded its deadline.", retryable=True) from exc
        except WriterError:
            raise
        except Exception as exc:
            if self._looks_disconnected(exc):
                self._disconnect()
                if mutating:
                    raise WriterError("UNKNOWN_OUTCOME", "The Writer connection was lost during a write. Inspect the document before retrying.", retryable=False) from exc
                raise WriterError("NOT_CONNECTED", "The Writer connection was lost.", retryable=True) from exc
            log.exception("UNO operation failed")
            if mutating:
                raise WriterError("UNKNOWN_OUTCOME", "A write failed after it started. Inspect the document before retrying.", retryable=False,
                                  details={"exception": type(exc).__name__}) from exc
            raise WriterError("UNO_ERROR", "The Writer operation failed.", details={"exception": type(exc).__name__}) from exc

    def call_if_connected(self, operation: Callable[[Any, Any], Any], *, timeout: float = 3.0) -> Any:
        """Run a cleanup action without establishing a new Writer connection."""
        if self._closed:
            return None
        def invoke():
            if self._context is None:
                return None
            return operation(self._context, self._desktop)
        future = self._executor.submit(invoke)
        try:
            return future.result(timeout=timeout)
        except Exception:
            log.debug("Best-effort cleanup operation failed", exc_info=True)
            return None

    def _execute(self, operation: Callable[[Any, Any], Any]) -> Any:
        context, desktop = self._connect()
        try:
            return operation(context, desktop)
        except Exception as exc:
            if self._looks_disconnected(exc):
                self._disconnect()
            raise

    def _connect(self):
        if self._context is not None:
            return self._context, self._desktop
        try:
            import uno
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
            endpoint = f"uno:socket,host={self.settings.host},port={self.settings.port};urp;StarOffice.ComponentContext"
            deadline = time.monotonic() + self.settings.connect_timeout
            last_error = None
            while time.monotonic() < deadline:
                try:
                    context = resolver.resolve(endpoint)
                    desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
                    self._local, self._context, self._desktop = local, context, desktop
                    self._generation += 1
                    log.info("Connected to Writer listener at %s:%s (generation %s)", self.settings.host, self.settings.port, self._generation)
                    return context, desktop
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.15)
            raise WriterError("NOT_CONNECTED", "No Writer listener answered on the configured localhost port.", retryable=True,
                              details={"host": self.settings.host, "port": self.settings.port}) from last_error
        except WriterError:
            raise
        except Exception as exc:
            raise WriterError("NOT_CONNECTED", "PyUNO could not connect to Writer.", retryable=True,
                              details={"exception": type(exc).__name__}) from exc

    def _disconnect(self):
        was_connected = self._context is not None
        self._local = self._context = self._desktop = None
        if was_connected:
            log.warning("Writer UNO session disconnected")
            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception:
                    log.exception("Disconnect cleanup failed")

    @staticmethod
    def _looks_disconnected(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        return any(part in name or part in message for part in ("disposedexception", "connection refused", "connection reset", "no connection", "remote bridge", "socket"))

    def close(self):
        self._closed = True
        # Removing listeners is owned by the service. Do not terminate or close Writer.
        self._executor.shutdown(wait=True, cancel_futures=True)
