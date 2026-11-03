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
                                                 "replace_table_cell", "insert_table", "insert_image",
                                                 "apply_formatting", "format_table_cell", "edit_table_structure",
                                                 "update_image", "save_document", "save_as",
                                                 "undo_last_change"],
                             document_id: str, expected_revision: int,
                             anchor: str | None = None, text: str | None = None,
                             position: str = "end", offset: int | None = None,
                             expected_text: str | None = None, expected_hash: str | None = None,
                             new_content: str | None = None, style_name: str | None = None,
                             table_anchor: str | None = None, cell_name: str | None = None,
                             rows: int | None = None, columns: int | None = None,
                             cell_values: list[list[str]] | None = None,
                             image_path: str | None = None, alt_text: str = "",
                             width_mm: int | None = None, height_mm: int | None = None,
                             target_path: str | None = None, file_format: str = "odt",
                             overwrite: bool = False, bold: bool | None = None,
                             italic: bool | None = None, underline: bool | None = None,
                             font_name: str | None = None, font_size_pt: float | None = None,
                             font_color: str | None = None,
                             alignment: Literal["left", "center", "right", "justify"] | None = None,
                             table_operation: Literal["insert_rows", "delete_rows", "insert_columns", "delete_columns"] | None = None,
                             index: int | None = None, count: int = 1,
                             image_ref: str | None = None, image_alt_text: str | None = None) -> str:
        """Prepare a bounded before/after preview for one supported change."""
        return _invoke(lambda: service.preview_change(
            change_type, document_id, expected_revision, anchor, text, position, offset,
            expected_text, expected_hash, new_content, style_name, table_anchor, cell_name,
            rows, columns, cell_values, image_path, alt_text, width_mm, height_mm,
            target_path, file_format, overwrite,
            {key: value for key, value in {"bold": bold, "italic": italic,
             "underline": underline, "font_name": font_name, "font_size_pt": font_size_pt,
             "font_color": font_color, "alignment": alignment}.items() if value is not None},
            table_operation, index, count, image_ref, alignment, image_alt_text))

    @server.tool(name="writer_preview_create_document", description="Preview creation of a new unsaved Writer document and issue a one-use confirmation token.", annotations=READ_ONLY)
    def writer_preview_create_document(title: str, initial_paragraphs: list[str] | None = None) -> str:
        """Preview a blank or pre-populated new Writer document without creating it."""
        return _invoke(lambda: service.preview_create_document(title, initial_paragraphs))

    @server.tool(name="writer_create_document", description="Create a new unsaved Writer document with optional initial paragraphs after confirmation.", annotations=MUTATING)
    def writer_create_document(title: str, operation_id: str, confirmation_token: str,
                               initial_paragraphs: list[str] | None = None) -> str:
        """Create a new document after its preview token and operation UUID are confirmed."""
        return _invoke(lambda: service.create_document(title, initial_paragraphs, operation_id, confirmation_token))

    @server.tool(name="writer_preview_compose_document", description="Validate and preview a complete new Writer document from paragraph, heading, table, and image blocks.", annotations=READ_ONLY)
    def writer_preview_compose_document(title: str, blocks: list[dict[str, Any]]) -> str:
        """Preview a structured document composition without opening a document."""
        return _invoke(lambda: service.preview_compose_document(title, blocks))

    @server.tool(name="writer_compose_document", description="Create a new unsaved Writer document from a confirmed structure of paragraph, heading, table, and image blocks.", annotations=MUTATING)
    def writer_compose_document(title: str, blocks: list[dict[str, Any]], operation_id: str,
                                confirmation_token: str) -> str:
        """Build a complete new document from a validated, confirmed list of structural blocks."""
        return _invoke(lambda: service.compose_document(title, blocks, operation_id, confirmation_token))

    @server.tool(name="writer_preview_create_from_template", description="Validate an allowlisted local .odt or .ott template and preview a new unsaved copy without opening or changing the template.", annotations=READ_ONLY)
    def writer_preview_create_from_template(template_path: str, title: str | None = None) -> str:
        """Preview creation of a new Writer document based on an authorized template."""
        return _invoke(lambda: service.preview_create_document_from_template(template_path, title))

    @server.tool(name="writer_create_from_template", description="Create a new unsaved Writer document from an allowlisted .odt or .ott template after hash, macro-safety, and confirmation checks.", annotations=MUTATING)
    def writer_create_from_template(template_path: str, expected_template_hash: str, title: str,
                                    operation_id: str, confirmation_token: str) -> str:
        """Create an editable untitled document from a confirmed OpenDocument template."""
        return _invoke(lambda: service.create_document_from_template(
            template_path, expected_template_hash, title, operation_id, confirmation_token))

    @server.tool(name="writer_insert_text", description="Insert bounded text at an explicit anchored paragraph position, guarded by revision, preview confirmation, and operation UUID.", annotations=MUTATING)
    def writer_insert_text(document_id: str, anchor: str, expected_revision: int, text: str,
                           operation_id: str, confirmation_token: str,
                           position: str = "end", offset: int | None = None) -> str:
        """Confirm a previewed insertion before, after, or inside an anchored paragraph."""
        return _invoke(lambda: service.insert_text(document_id, anchor, expected_revision, text,
                                                   position, offset, operation_id, confirmation_token))

    @server.tool(name="writer_insert_table", description="Insert a bounded table after a paragraph anchor, guarded by revision, preview, confirmation, and operation UUID.", annotations=MUTATING)
    def writer_insert_table(document_id: str, anchor: str, expected_revision: int,
                            rows: int, columns: int, operation_id: str, confirmation_token: str,
                            cell_values: list[list[str]] | None = None) -> str:
        """Insert a new table after the anchored paragraph without replacing its text."""
        return _invoke(lambda: service.insert_table(document_id, anchor, expected_revision,
                                                   rows, columns, cell_values, operation_id, confirmation_token))

    @server.tool(name="writer_insert_image", description="Embed a local image after a paragraph anchor. Image paths must be under YBERU_WRITER_ALLOWED_IMAGE_ROOTS; revision, file hash, preview, and confirmation are checked.", annotations=MUTATING)
    def writer_insert_image(document_id: str, anchor: str, expected_revision: int,
                            image_path: str, expected_image_hash: str, operation_id: str,
                            confirmation_token: str, alt_text: str = "",
                            width_mm: int | None = None, height_mm: int | None = None) -> str:
        """Embed a permitted local PNG, JPEG, GIF, or BMP image at an anchored paragraph."""
        return _invoke(lambda: service.insert_image(document_id, anchor, expected_revision,
                                                    image_path, expected_image_hash, alt_text,
                                                    width_mm, height_mm, operation_id, confirmation_token))

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

    @server.tool(name="writer_edit_table_structure", description="Insert or remove table rows or columns after previewing affected cell values and confirming the table fingerprint.", annotations=DESTRUCTIVE)
    def writer_edit_table_structure(document_id: str, table_anchor: str, expected_revision: int,
                                    table_operation: Literal["insert_rows", "delete_rows", "insert_columns", "delete_columns"],
                                    index: int, count: int, expected_table_hash: str,
                                    operation_id: str, confirmation_token: str) -> str:
        """Change table dimensions at a zero-based index after an exact preview."""
        return _invoke(lambda: service.edit_table_structure(
            document_id, table_anchor, expected_revision, table_operation, index,
            count, expected_table_hash, operation_id, confirmation_token))

    @server.tool(name="writer_format_table_cell", description="Apply allowlisted text and paragraph formatting to one exact table cell after preview, revision, and cell-text checks.", annotations=MUTATING)
    def writer_format_table_cell(document_id: str, table_anchor: str, cell_name: str,
                                 expected_revision: int, expected_text: str,
                                 operation_id: str, confirmation_token: str,
                                 bold: bool | None = None, italic: bool | None = None,
                                 underline: bool | None = None, font_name: str | None = None,
                                 font_size_pt: float | None = None, font_color: str | None = None,
                                 alignment: Literal["left", "center", "right", "justify"] | None = None) -> str:
        """Format a table cell without changing its text or other cells."""
        options = {key: value for key, value in {"bold": bold, "italic": italic,
                   "underline": underline, "font_name": font_name,
                   "font_size_pt": font_size_pt, "font_color": font_color,
                   "alignment": alignment}.items() if value is not None}
        return _invoke(lambda: service.format_table_cell(
            document_id, table_anchor, cell_name, expected_revision, expected_text,
            options, operation_id, confirmation_token))

    @server.tool(name="writer_update_image", description="Update image alternative text, dimensions, or paragraph alignment using the current opaque image reference and revision.", annotations=MUTATING)
    def writer_update_image(document_id: str, image_ref: str, expected_revision: int,
                            expected_image_hash: str, operation_id: str, confirmation_token: str,
                            new_alt_text: str | None = None, width_mm: int | None = None,
                            height_mm: int | None = None,
                            alignment: Literal["left", "center", "right"] | None = None) -> str:
        """Update selected image properties while preserving the embedded graphic."""
        return _invoke(lambda: service.update_image(
            document_id, image_ref, expected_revision, expected_image_hash, new_alt_text,
            width_mm, height_mm, alignment, operation_id, confirmation_token))

    @server.tool(name="writer_apply_style", description="Apply an existing paragraph style to an anchored paragraph after preview, revision, and content checks.", annotations=MUTATING)
    def writer_apply_style(document_id: str, anchor: str, expected_revision: int, expected_text: str,
                           style_name: str, operation_id: str, confirmation_token: str) -> str:
        """Confirm application of an existing paragraph style without changing text."""
        return _invoke(lambda: service.apply_style(document_id, anchor, expected_revision, expected_text,
                                                   style_name, operation_id, confirmation_token))

    @server.tool(name="writer_apply_formatting", description="Apply allowlisted character and paragraph formatting to one exact anchored paragraph after preview, revision, and text checks.", annotations=MUTATING)
    def writer_apply_formatting(document_id: str, anchor: str, expected_revision: int,
                                expected_text: str, operation_id: str, confirmation_token: str,
                                bold: bool | None = None, italic: bool | None = None,
                                underline: bool | None = None, font_name: str | None = None,
                                font_size_pt: float | None = None, font_color: str | None = None,
                                alignment: Literal["left", "center", "right", "justify"] | None = None) -> str:
        """Apply selected formatting properties without replacing the paragraph text."""
        options = {key: value for key, value in {"bold": bold, "italic": italic,
                   "underline": underline, "font_name": font_name,
                   "font_size_pt": font_size_pt, "font_color": font_color,
                   "alignment": alignment}.items() if value is not None}
        return _invoke(lambda: service.apply_formatting(
            document_id, anchor, expected_revision, expected_text, options,
            operation_id, confirmation_token))

    @server.tool(name="writer_save_document", description="Confirm saving a modified existing local document under configured allowed roots.", annotations=DESTRUCTIVE)
    def writer_save_document(document_id: str, expected_revision: int, expected_path: str,
                             operation_id: str, confirmation_token: str) -> str:
        """Save the previewed existing document in place."""
        return _invoke(lambda: service.save_document(document_id, expected_revision, expected_path,
                                                     operation_id, confirmation_token))

    @server.tool(name="writer_save_as", description="Save an open Writer document to a new local path under YBERU_WRITER_ALLOWED_SAVE_ROOTS. Preview, revision, destination hash, and explicit overwrite confirmation are required.", annotations=DESTRUCTIVE)
    def writer_save_as(document_id: str, expected_revision: int, target_path: str,
                       file_format: Literal["odt", "docx", "rtf", "txt"], overwrite: bool,
                       expected_target_hash: str | None, operation_id: str,
                       confirmation_token: str) -> str:
        """Save a document to a previewed local destination in a supported format."""
        return _invoke(lambda: service.save_as(document_id, expected_revision, target_path,
                                               file_format, overwrite, expected_target_hash,
                                               operation_id, confirmation_token))

    @server.tool(name="writer_undo_last_change", description="Undo only the latest Yberu MCP change after previewing it; refuses to undo a user action.", annotations=DESTRUCTIVE)
    def writer_undo_last_change(document_id: str, expected_revision: int, target_operation_id: str,
                                operation_id: str, confirmation_token: str) -> str:
        """Undo one identified Yberu MCP operation if it is still the top Writer undo action."""
        return _invoke(lambda: service.undo_last_change(document_id, expected_revision,
                                                        target_operation_id, operation_id, confirmation_token))

    return server
