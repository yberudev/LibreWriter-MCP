"""High-level, allowlisted MCP tools; no arbitrary UNO method surface."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..errors import WriterError
from ..session import WriterService

log = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _invoke(fn):
    try:
        return _json(fn())
    except WriterError as exc:
        log.info("MCP operation rejected: %s", exc.code)
        raise ToolError(_json(exc.as_dict())) from exc
    except Exception as exc:
        log.exception("Unexpected MCP tool failure")
        err = WriterError("INTERNAL_ERROR", "The operation failed unexpectedly.", details={"exception": type(exc).__name__})
        raise ToolError(_json(err.as_dict())) from exc


def register_tools(server: MCPServer, service: WriterService) -> MCPServer:
    @server.tool(name="writer_get_state", description="Return Writer connection, active document, version, and supported capabilities.")
    def writer_get_state() -> str:
        """Read the current Writer MCP connection state."""
        return _invoke(service.get_state)

    @server.tool(name="writer_list_documents", description="List open Writer documents with opaque IDs, modified/read-only state, and local revision.")
    def writer_list_documents() -> str:
        """List open Writer documents."""
        return _invoke(service.list_documents)

    @server.tool(name="writer_read_document", description="Read a bounded text slice from an open Writer document. Offsets are Unicode code points.")
    def writer_read_document(document_id: str, offset: int = 0, limit: int = 20000) -> str:
        """Read a text slice with revision and content reference."""
        return _invoke(lambda: service.read_document(document_id, offset, limit))

    @server.tool(name="writer_read_selection", description="Read the current selection, including non-text selection type information.")
    def writer_read_selection(document_id: str | None = None) -> str:
        """Read the current selection of a Writer document."""
        return _invoke(lambda: service.read_selection(document_id))

    @server.tool(name="writer_read_structure", description="Return paragraph and heading structure with opaque anchors, styles, and table references.")
    def writer_read_structure(document_id: str) -> str:
        """Read a bounded structured representation of a Writer document."""
        return _invoke(lambda: service.read_structure(document_id))

    @server.tool(name="writer_insert_text", description="Insert bounded text at an explicit anchored paragraph position, guarded by revision and operation UUID.")
    def writer_insert_text(document_id: str, anchor: str, expected_revision: int, text: str,
                           operation_id: str, position: str = "end", offset: int | None = None) -> str:
        """Insert text before, after, or at a code-point offset inside an anchored paragraph."""
        return _invoke(lambda: service.insert_text(document_id, anchor, expected_revision, text, position, offset, operation_id))

    @server.tool(name="writer_replace_range", description="Safely replace one anchored paragraph after revision and exact text/hash checks. No fuzzy matching is performed.")
    def writer_replace_range(document_id: str, anchor: str, expected_revision: int, new_content: str,
                             operation_id: str, expected_text: str | None = None,
                             expected_hash: str | None = None) -> str:
        """Replace the exact paragraph represented by an anchor."""
        return _invoke(lambda: service.replace_range(document_id, anchor, expected_revision, new_content,
                                                     expected_text, expected_hash, operation_id))

    @server.tool(name="writer_apply_style", description="Apply an existing paragraph style to an anchored paragraph after revision and content checks.")
    def writer_apply_style(document_id: str, anchor: str, expected_revision: int, expected_text: str,
                           style_name: str, operation_id: str) -> str:
        """Apply an existing paragraph style without changing paragraph text."""
        return _invoke(lambda: service.apply_style(document_id, anchor, expected_revision, expected_text, style_name, operation_id))

    @server.tool(name="writer_save_document", description="Save a modified existing local document only when its path is inside configured allowed roots.")
    def writer_save_document(document_id: str, operation_id: str) -> str:
        """Save the existing document in place; Save As is not supported."""
        return _invoke(lambda: service.save_document(document_id, operation_id))

    return server
