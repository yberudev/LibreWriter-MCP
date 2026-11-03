"""High-level, allowlisted MCP tools; no arbitrary UNO method surface."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..errors import WriterError
from ..session import WriterService

log = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


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
    @server.tool(name="writer_get_state", description="Return Writer connection, active document, version, and supported capabilities.", annotations=READ_ONLY)
    def writer_get_state() -> str:
        """Read the current Writer MCP connection state."""
        return _invoke(service.get_state)

    @server.tool(name="writer_list_documents", description="List open Writer documents with opaque IDs, modified/read-only state, and local revision.", annotations=READ_ONLY)
    def writer_list_documents() -> str:
        """List open Writer documents."""
        return _invoke(service.list_documents)

    @server.tool(name="writer_read_document", description="Read a bounded text slice from an open Writer document. Offsets are Unicode code points.", annotations=READ_ONLY)
    def writer_read_document(document_id: str, offset: int = 0, limit: int = 20000) -> str:
        """Read a text slice with revision and content reference."""
        return _invoke(lambda: service.read_document(document_id, offset, limit))

    @server.tool(name="writer_read_selection", description="Read the current selection, including non-text selection type information.", annotations=READ_ONLY)
    def writer_read_selection(document_id: str | None = None) -> str:
        """Read the current selection of a Writer document."""
        return _invoke(lambda: service.read_selection(document_id))

    @server.tool(name="writer_read_structure", description="Return paragraph and heading structure with opaque anchors, styles, and table references.", annotations=READ_ONLY)
    def writer_read_structure(document_id: str) -> str:
        """Read a bounded structured representation of a Writer document."""
        return _invoke(lambda: service.read_structure(document_id))

    @server.tool(name="writer_preview_change", description="Preview an exact write and issue a short-lived, one-use confirmation token. This tool never changes the document.", annotations=READ_ONLY)
    def writer_preview_change(change_type: Literal["insert_text", "replace_range", "apply_style",
                                                 "replace_table_cell", "save_document", "undo_last_change"],
                             document_id: str, expected_revision: int,
                             anchor: str | None = None, text: str | None = None,
                             position: str = "end", offset: int | None = None,
                             expected_text: str | None = None, expected_hash: str | None = None,
                             new_content: str | None = None, style_name: str | None = None,
                             table_anchor: str | None = None, cell_name: str | None = None) -> str:
        """Prepare a bounded before/after preview for one supported change."""
        return _invoke(lambda: service.preview_change(
            change_type, document_id, expected_revision, anchor, text, position, offset,
            expected_text, expected_hash, new_content, style_name, table_anchor, cell_name))

    @server.tool(name="writer_insert_text", description="Insert bounded text at an explicit anchored paragraph position, guarded by revision, preview confirmation, and operation UUID.", annotations=MUTATING)
    def writer_insert_text(document_id: str, anchor: str, expected_revision: int, text: str,
                           operation_id: str, confirmation_token: str,
                           position: str = "end", offset: int | None = None) -> str:
        """Confirm a previewed insertion before, after, or inside an anchored paragraph."""
        return _invoke(lambda: service.insert_text(document_id, anchor, expected_revision, text,
                                                   position, offset, operation_id, confirmation_token))

    @server.tool(name="writer_read_text_range", description="Read a text range bounded by current paragraph anchors and Unicode code-point offsets.", annotations=READ_ONLY)
    def writer_read_text_range(document_id: str, start_anchor: str, start_offset: int,
                               end_anchor: str, end_offset: int) -> str:
        """Read an exact range and return a revision-bound range anchor."""
        return _invoke(lambda: service.read_text_range(document_id, start_anchor, start_offset,
                                                       end_anchor, end_offset))

    @server.tool(name="writer_replace_range", description="Safely replace an exact anchored paragraph or text range after preview, revision, and expected text/hash checks. No fuzzy matching is performed.", annotations=DESTRUCTIVE)
    def writer_replace_range(document_id: str, anchor: str, expected_revision: int, new_content: str,
                             operation_id: str, confirmation_token: str,
                             expected_text: str | None = None,
                             expected_hash: str | None = None) -> str:
        """Confirm and replace the exact paragraph/range represented by an anchor."""
        return _invoke(lambda: service.replace_range(document_id, anchor, expected_revision, new_content,
                                                     operation_id, confirmation_token, expected_text, expected_hash))

    @server.tool(name="writer_read_table", description="Read cells from a table identified by a current structure reference.", annotations=READ_ONLY)
    def writer_read_table(document_id: str, table_anchor: str, expected_revision: int) -> str:
        """Read a bounded table matrix with per-cell hashes."""
        return _invoke(lambda: service.read_table(document_id, table_anchor, expected_revision))

    @server.tool(name="writer_replace_table_cell", description="Replace one exact table cell after preview, revision, and expected text/hash checks.", annotations=DESTRUCTIVE)
    def writer_replace_table_cell(document_id: str, table_anchor: str, cell_name: str,
                                  expected_revision: int, new_content: str, operation_id: str,
                                  confirmation_token: str,
                                  expected_text: str | None = None,
                                  expected_hash: str | None = None) -> str:
        """Confirm and replace one table cell without modifying other cells or document content."""
        return _invoke(lambda: service.replace_table_cell(document_id, table_anchor, cell_name,
                                                          expected_revision, new_content, operation_id, confirmation_token,
                                                          expected_text, expected_hash))

    @server.tool(name="writer_apply_style", description="Apply an existing paragraph style to an anchored paragraph after preview, revision, and content checks.", annotations=MUTATING)
    def writer_apply_style(document_id: str, anchor: str, expected_revision: int, expected_text: str,
                           style_name: str, operation_id: str, confirmation_token: str) -> str:
        """Confirm application of an existing paragraph style without changing text."""
        return _invoke(lambda: service.apply_style(document_id, anchor, expected_revision, expected_text,
                                                   style_name, operation_id, confirmation_token))

    @server.tool(name="writer_save_document", description="Confirm saving a modified existing local document under configured allowed roots.", annotations=DESTRUCTIVE)
    def writer_save_document(document_id: str, expected_revision: int, expected_path: str,
                             operation_id: str, confirmation_token: str) -> str:
        """Save the previewed existing document in place; Save As is not supported."""
        return _invoke(lambda: service.save_document(document_id, expected_revision, expected_path,
                                                     operation_id, confirmation_token))

    @server.tool(name="writer_undo_last_change", description="Undo only the latest Yberu MCP change after previewing it; refuses to undo a user action.", annotations=DESTRUCTIVE)
    def writer_undo_last_change(document_id: str, expected_revision: int, target_operation_id: str,
                                operation_id: str, confirmation_token: str) -> str:
        """Undo one identified Yberu MCP operation if it is still the top Writer undo action."""
        return _invoke(lambda: service.undo_last_change(document_id, expected_revision,
                                                        target_operation_id, operation_id, confirmation_token))

    return server
