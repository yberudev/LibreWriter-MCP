"""Runtime configuration with conservative defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = int(raw)
    if value <= 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be between 1 and {maximum or 'infinity'}")
    return value


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 2002
    soffice_bin: Path = Path("/usr/bin/soffice")
    connect_timeout: float = 2.5
    operation_timeout: float = 30.0
    max_read_chars: int = 20_000
    max_snapshot_chars: int = 5_000_000
    max_structure_items: int = 2_000
    max_text_chars: int = 100_000
    allowed_save_roots: tuple[Path, ...] = ()
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        host = os.environ.get("YBERU_WRITER_HOST", "127.0.0.1").strip()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("YBERU_WRITER_HOST must resolve to loopback")
        port = _positive_int("YBERU_WRITER_PORT", 2002, maximum=65535)
        raw_roots = os.environ.get("YBERU_WRITER_ALLOWED_SAVE_ROOTS", "")
        roots = tuple(
            Path(item).expanduser().resolve()
            for item in raw_roots.split(os.pathsep)
            if item.strip()
        )
        log_level = os.environ.get("YBERU_WRITER_LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("YBERU_WRITER_LOG_LEVEL is invalid")
        return cls(
            host=host,
            port=port,
            soffice_bin=Path(os.environ.get("YBERU_WRITER_SOFFICE", "/usr/bin/soffice")).expanduser(),
            connect_timeout=float(os.environ.get("YBERU_WRITER_CONNECT_TIMEOUT", "2.5")),
            operation_timeout=float(os.environ.get("YBERU_WRITER_OPERATION_TIMEOUT", "30")),
            max_read_chars=_positive_int("YBERU_WRITER_MAX_READ_CHARS", 20_000, maximum=500_000),
            max_snapshot_chars=_positive_int("YBERU_WRITER_MAX_SNAPSHOT_CHARS", 5_000_000, maximum=50_000_000),
            max_structure_items=_positive_int("YBERU_WRITER_MAX_STRUCTURE_ITEMS", 2_000, maximum=20_000),
            max_text_chars=_positive_int("YBERU_WRITER_MAX_TEXT_CHARS", 100_000, maximum=1_000_000),
            allowed_save_roots=roots,
            log_level=log_level,
        )
